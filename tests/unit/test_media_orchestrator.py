"""analysis.media_orchestrator 单元测试（全程 mock，无真实网络）。

覆盖：fence 剥离、非法 JSON 重试、shotID 集合不一致（漏/重/未知）回退单镜头、
429 指数退避、单镜头永久失败、乱序响应按 ID 对齐、"无" 原值透传、
checkpoint 崩溃续跑、skipped 降级产物。
"""

from __future__ import annotations

import json
import threading

import pytest

from memoloupe.analysis.media_orchestrator import (
    build_skipped_unified_media,
    run_unified_media_analysis,
)
from memoloupe.analysis.vocabulary import load_vocabulary
from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.artifacts.store import ArtifactStore
from memoloupe.core.config import load_config
from memoloupe.services.base import PermanentServiceError, TransientServiceError
from memoloupe.services.mock import MockUnifiedMediaService, _default_shot_fields

NO_SLEEP = lambda _sec: None  # noqa: E731


def _clips_info(n: int) -> list[dict]:
    items = []
    for i in range(1, n + 1):
        sid = f"SH{i:04d}"
        items.append(
            {
                "shotID": sid,
                "startMs": (i - 1) * 1000,
                "endMs": i * 1000,
                "durationMs": 1000,
                "file": f"clips/{sid}.mp4",
                "modelFile": f"clips/model-proxy/{sid}-abcd.mp4",
                "modelDurationMs": 1000,
                "modelNormalization": None,
            }
        )
    return items


def _config(**unified_overrides) -> dict:
    config = load_config(env={})
    config["unifiedModel"].update(unified_overrides)
    return config


def _run(store, clips, service, config, sleep=NO_SLEEP):
    return run_unified_media_analysis(
        store,
        clips,
        service,
        config=config,
        vocab=load_vocabulary(),
        source_revision="a1b2c3d4e5f6",
        sleep=sleep,
    )


def _payload(group_name: str, shot_ids, *, content_prefix: str | None = None) -> str:
    shots = []
    for sid in shot_ids:
        shot = _default_shot_fields(group_name, sid)
        if content_prefix and "visual" in shot:
            shot["visual"]["content"] = f"{content_prefix}-{sid}"
        shots.append(shot)
    return json.dumps({"shots": shots}, ensure_ascii=False)


def _good_service(shot_ids) -> MockUnifiedMediaService:
    def script(clips, group, call_index):
        return _payload(group.name, [c.shot_id for c in clips])

    return MockUnifiedMediaService(script)


def _validate(doc: dict) -> None:
    validate_artifact(ArtifactName.UNIFIED_MEDIA, doc)


class TestHappyPath:
    def test_complete_run_passes_schema(self, tmp_path):
        clips = _clips_info(5)
        shot_ids = [c["shotID"] for c in clips]
        service = _good_service(shot_ids)
        service.model = "mock-unified-1"
        doc = _run(ArtifactStore(tmp_path), clips, service, _config(batchSize=2))

        _validate(doc)
        assert doc["status"] == "complete"
        assert doc["terminal"] is True
        assert doc["completedShots"] == 5
        assert doc["failedShots"] == 0
        assert doc["pendingShots"] == 0
        assert doc["permanentFailureShots"] == 0
        assert doc["shotStatuses"] == {sid: "succeeded" for sid in shot_ids}

        request = doc["request"]
        assert request["model"] == "mock-unified-1"
        assert request["clipTransport"] == "videoDataURI"
        assert request["externalFrameExtraction"] is False
        assert request["batchSize"] == 2
        assert request["sourceRevisionID"] == "a1b2c3d4e5f6"
        assert request["shortClipPolicy"] == {
            "minimumDurationMs": 800,
            "recoveryMinimumDurationMs": 2000,
            "recoveryWidth": 720,
        }
        assert doc["retryPolicy"] == {
            "maxRetries": 3,
            "fallbackFromBatchToSingleShot": True,
            "checkpointAfterEachRequest": True,
        }
        assert doc["service"] == "unifiedAudioVideo"
        assert len(doc["schemaFingerprint"]) == 16

        # 批次确定性：batchID 连续编号，shotIDs 与 response.shots 集合一致
        assert [b["batchID"] for b in doc["batches"]] == ["B0001", "B0002", "B0003"]
        assert [b["shotIDs"] for b in doc["batches"]] == [
            ["SH0001", "SH0002"],
            ["SH0003", "SH0004"],
            ["SH0005"],
        ]
        for batch in doc["batches"]:
            assert batch["status"] == "complete"
            assert {s["shotID"] for s in batch["response"]["shots"]} == set(
                batch["shotIDs"]
            )
        # 合并后的 modelShot 六段齐全且字段完整
        shot = doc["batches"][0]["response"]["shots"][0]
        for section in ("visual", "function", "audio", "components", "editing", "confidence"):
            assert section in shot
        assert len(shot["visual"]) == 21
        assert set(shot["confidence"]) == {"visual", "audio", "editing", "overall"}

    def test_model_defaults_to_mock(self, tmp_path):
        clips = _clips_info(1)
        doc = _run(
            ArtifactStore(tmp_path), clips, _good_service(["SH0001"]), _config()
        )
        assert doc["request"]["model"] == "mock"
        assert doc["request"]["fallbackModel"] is None

    def test_raw_wu_value_passthrough(self, tmp_path):
        # 模型输出的 "无" 原值保留进 raw；归一化是 Observation 层的事
        clips = _clips_info(1)
        doc = _run(
            ArtifactStore(tmp_path), clips, _good_service(["SH0001"]), _config()
        )
        shot = doc["batches"][0]["response"]["shots"][0]
        assert shot["components"]["compositingEvents"] == "无"
        assert shot["visual"]["subjects"] == "无"

    def test_out_of_order_response_aligned_by_id(self, tmp_path):
        # 响应 shots 数组乱序且带每镜头特征值：合并必须按 shotID 对齐
        clips = _clips_info(3)

        def script(clips_arg, group, call_index):
            ids = [c.shot_id for c in clips_arg]
            return _payload(group.name, list(reversed(ids)), content_prefix="content")

        doc = _run(ArtifactStore(tmp_path), clips, MockUnifiedMediaService(script), _config())
        shots = {
            s["shotID"]: s
            for b in doc["batches"]
            for s in b["response"]["shots"]
        }
        for sid in ("SH0001", "SH0002", "SH0003"):
            assert shots[sid]["visual"]["content"] == f"content-{sid}"


class TestResponseParsing:
    def test_markdown_fence_stripped(self, tmp_path):
        clips = _clips_info(1)

        def script(clips_arg, group, call_index):
            body = _payload(group.name, ["SH0001"])
            return f"```json\n{body}\n```"

        doc = _run(ArtifactStore(tmp_path), clips, MockUnifiedMediaService(script), _config())
        assert doc["status"] == "complete"

    def test_invalid_json_retried_then_succeeds(self, tmp_path):
        clips = _clips_info(1)
        calls = {"n": 0}
        sleeps: list[float] = []

        def script(clips_arg, group, call_index):
            if group.name != "visual":
                return _payload(group.name, ["SH0001"])
            calls["n"] += 1
            if calls["n"] <= 2:
                return "这不是 JSON"
            return _payload("visual", ["SH0001"])

        doc = _run(
            ArtifactStore(tmp_path),
            clips,
            MockUnifiedMediaService(script),
            _config(),
            sleep=sleeps.append,
        )
        assert doc["status"] == "complete"
        assert calls["n"] == 3
        assert sleeps == [1.0, 2.0]  # 指数退避：基数 1s
        visual_batch = doc["batches"][0]
        assert visual_batch["attempts"] == 3 + 1 + 1  # visual 3 次 + 另两组各 1 次

    def test_transient_error_retried_then_succeeds(self, tmp_path):
        clips = _clips_info(1)
        calls = {"n": 0}

        def script(clips_arg, group, call_index):
            if group.name != "audio":
                return _payload(group.name, ["SH0001"])
            calls["n"] += 1
            if calls["n"] <= 3:
                raise TransientServiceError("HTTP 429: rate limited")
            return _payload("audio", ["SH0001"])

        doc = _run(
            ArtifactStore(tmp_path),
            clips,
            MockUnifiedMediaService(script),
            _config(),
        )
        assert doc["status"] == "complete"
        assert calls["n"] == 4  # 429 三次后第四次成功


class TestShotIdMismatchFallback:
    """漏 shot / 重复 shot / 未知 shot → 批次失败，回退单镜头逐个请求。"""

    def _service(self, mode: str) -> MockUnifiedMediaService:
        def script(clips_arg, group, call_index):
            ids = [c.shot_id for c in clips_arg]
            if len(ids) == 1:
                return _payload(group.name, ids)
            if mode == "missing":
                return _payload(group.name, ids[:1])
            if mode == "duplicate":
                shots = [_default_shot_fields(group.name, ids[0])] * 2
                return json.dumps({"shots": shots}, ensure_ascii=False)
            assert mode == "unknown"
            return _payload(group.name, [ids[0], "SH9999"])

        return MockUnifiedMediaService(script)

    @pytest.mark.parametrize("mode", ["missing", "duplicate", "unknown"])
    def test_mismatch_falls_back_to_single_shot(self, tmp_path, mode):
        clips = _clips_info(2)
        service = self._service(mode)
        doc = _run(
            ArtifactStore(tmp_path), clips, service, _config(batchSize=4)
        )
        assert doc["status"] == "complete"
        assert doc["shotStatuses"] == {"SH0001": "succeeded", "SH0002": "succeeded"}
        pair_calls = [
            c
            for c in service.calls
            if c["shot_ids"] == ("SH0001", "SH0002") and c["group"] == "visual"
        ]
        single_calls = [
            c for c in service.calls if len(c["shot_ids"]) == 1 and c["group"] == "visual"
        ]
        assert len(pair_calls) == 4  # 1 次首发 + 3 次重试，然后回退
        assert len(single_calls) == 2  # 每个镜头一次单发
        _validate(doc)

    def test_permanent_batch_error_falls_back_without_retry(self, tmp_path):
        def script(clips_arg, group, call_index):
            if len(clips_arg) > 1:
                raise PermanentServiceError("HTTP 400: bad request")
            return _payload(group.name, [c.shot_id for c in clips_arg])

        clips = _clips_info(2)
        service = MockUnifiedMediaService(script)
        doc = _run(
            ArtifactStore(tmp_path), clips, service, _config(batchSize=4)
        )
        assert doc["status"] == "complete"
        pair_calls = [c for c in service.calls if len(c["shot_ids"]) == 2]
        assert len(pair_calls) == 3  # 每组 1 次，PermanentServiceError 不重试


class TestPermanentFailure:
    def test_single_shot_permanent_failure_partial(self, tmp_path):
        clips = _clips_info(2)

        def script(clips_arg, group, call_index):
            ids = [c.shot_id for c in clips_arg]
            if "SH0001" in ids:
                raise PermanentServiceError("clip unreadable: shotID=SH0001")
            return _payload(group.name, ids)

        service = MockUnifiedMediaService(script)
        service.fallback_model = "mock-fallback"
        doc = _run(ArtifactStore(tmp_path), clips, service, _config(batchSize=4))

        _validate(doc)
        assert doc["status"] == "partial"
        assert doc["terminal"] is True
        assert doc["completedShots"] == 1
        assert doc["permanentFailureShots"] == 1
        assert doc["shotStatuses"] == {
            "SH0001": "permanent_failure",
            "SH0002": "succeeded",
        }
        # 部分失败分区拆成单镜头记录；失败批次无 response，成功镜头数据不丢
        by_id = {b["shotIDs"][0]: b for b in doc["batches"]}
        assert by_id["SH0001"]["status"] == "failed"
        assert "response" not in by_id["SH0001"]
        assert by_id["SH0001"]["fallbackAttempted"] is True
        assert by_id["SH0002"]["status"] == "complete"
        assert by_id["SH0002"]["response"]["shots"][0]["shotID"] == "SH0002"

    def test_all_shots_failed_status_failed(self, tmp_path):
        clips = _clips_info(2)

        def script(clips_arg, group, call_index):
            raise PermanentServiceError("model unavailable")

        doc = _run(
            ArtifactStore(tmp_path),
            clips,
            MockUnifiedMediaService(script),
            _config(batchSize=4),
        )
        _validate(doc)
        assert doc["status"] == "failed"
        assert doc["completedShots"] == 0
        assert doc["permanentFailureShots"] == 2
        assert all(b["status"] == "failed" for b in doc["batches"])

    def test_transient_exhausted_is_permanent_failure(self, tmp_path):
        clips = _clips_info(1)

        def script(clips_arg, group, call_index):
            raise TransientServiceError("HTTP 503: overloaded")

        doc = _run(
            ArtifactStore(tmp_path),
            clips,
            MockUnifiedMediaService(script),
            _config(maxRetries=2),
        )
        _validate(doc)
        assert doc["status"] == "failed"
        assert doc["shotStatuses"] == {"SH0001": "permanent_failure"}
        # 批次 3 次（1+2 重试）+ 单镜头 3 次，每组如此
        assert doc["batches"][0]["attempts"] == (3 + 3) * 3


class TestCheckpoint:
    def test_crash_resume_only_requests_unfinished_shots(self, tmp_path):
        clips = _clips_info(5)
        all_ids = [c["shotID"] for c in clips]
        store = ArtifactStore(tmp_path)
        config = _config(batchSize=4, concurrency=1)

        def crash_on_second_partition(clips_arg, group, call_index):
            ids = [c.shot_id for c in clips_arg]
            if ids == ["SH0005"]:
                raise RuntimeError("模拟进程崩溃")
            return _payload(group.name, ids)

        with pytest.raises(RuntimeError):
            _run(store, clips, MockUnifiedMediaService(crash_on_second_partition), config)

        checkpoint = json.loads(
            (tmp_path / "checkpoints" / "unified-media-visual.json").read_text(
                encoding="utf-8"
            )
        )
        assert checkpoint["completedShotIDs"] == all_ids[:4]
        assert checkpoint["fingerprint"]

        # 重跑：visual 组只请求 SH0005；audio/editing 组从未运行，全部重请
        service = _good_service(all_ids)
        doc = _run(store, clips, service, config)
        _validate(doc)
        assert doc["status"] == "complete"
        assert doc["shotStatuses"] == {sid: "succeeded" for sid in all_ids}

        visual_calls = [c["shot_ids"] for c in service.calls if c["group"] == "visual"]
        assert visual_calls == [("SH0005",)]
        audio_ids = {
            sid for c in service.calls if c["group"] == "audio" for sid in c["shot_ids"]
        }
        assert audio_ids == set(all_ids)

    def test_checkpoint_rejected_on_fingerprint_mismatch(self, tmp_path):
        clips = _clips_info(1)
        store = ArtifactStore(tmp_path)
        # 写入一个指纹不匹配的 checkpoint
        (tmp_path / "checkpoints").mkdir()
        (tmp_path / "checkpoints" / "unified-media-visual.json").write_text(
            json.dumps(
                {
                    "version": "unified.v1",
                    "group": "visual",
                    "fingerprint": "stale",
                    "batchSize": 4,
                    "completedShotIDs": ["SH0001"],
                    "results": {"SH0001": _default_shot_fields("visual", "SH0001")},
                }
            ),
            encoding="utf-8",
        )
        service = _good_service(["SH0001"])
        doc = _run(store, clips, service, _config())
        assert doc["status"] == "complete"
        assert service.calls, "指纹不匹配的 checkpoint 不得被复用"

    def test_checkpoint_rejects_invalid_entries(self, tmp_path):
        clips = _clips_info(2)
        store = ArtifactStore(tmp_path)
        config = _config()
        # 先跑一次拿到真实指纹
        service = _good_service(["SH0001", "SH0002"])
        _run(store, clips, service, config)
        path = tmp_path / "checkpoints" / "unified-media-visual.json"
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        # 污染一条成功项（缺 section），另一条保持合法
        checkpoint["results"]["SH0001"] = {"shotID": "SH0001"}
        path.write_text(json.dumps(checkpoint), encoding="utf-8")

        service2 = _good_service(["SH0001", "SH0002"])
        doc = _run(store, clips, service2, config)
        assert doc["status"] == "complete"
        visual_calls = [c["shot_ids"] for c in service2.calls if c["group"] == "visual"]
        assert visual_calls == [("SH0001",)], "只有未通过校验的 SH0001 需要重请"


class TestSkipped:
    def test_build_skipped_unified_media_passes_schema(self, tmp_path):
        clips = _clips_info(3)
        doc = build_skipped_unified_media(clips, _config(), "a1b2c3d4e5f6")
        _validate(doc)
        assert doc["status"] == "skipped"
        assert doc["terminal"] is False
        assert doc["batches"] == []
        assert doc["shotStatuses"] == {
            "SH0001": "pending",
            "SH0002": "pending",
            "SH0003": "pending",
        }
        assert doc["pendingShots"] == 3
        assert doc["request"]["sourceRevisionID"] == "a1b2c3d4e5f6"


class TestConcurrencyDeterminism:
    def test_concurrent_batches_merge_deterministically(self, tmp_path):
        # 并发 + 响应延迟乱序，合并结果必须按 shotID 对齐且确定
        clips = _clips_info(8)
        lock = threading.Lock()
        started: list[str] = []

        def script(clips_arg, group, call_index):
            ids = [c.shot_id for c in clips_arg]
            with lock:
                started.append(f"{group.name}:{','.join(ids)}")
            return _payload(group.name, list(reversed(ids)), content_prefix="c")

        config = _config(batchSize=1, concurrency=4)
        doc = _run(ArtifactStore(tmp_path), clips, MockUnifiedMediaService(script), config)
        _validate(doc)
        assert doc["status"] == "complete"
        shots = [s for b in doc["batches"] for s in b["response"]["shots"]]
        assert [s["shotID"] for s in shots] == [c["shotID"] for c in clips]
        for shot in shots:
            assert shot["visual"]["content"] == f"c-{shot['shotID']}"
