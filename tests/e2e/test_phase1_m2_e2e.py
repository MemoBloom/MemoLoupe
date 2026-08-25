"""Phase 1 M2 端到端测试：真实检测链路 + mock/降级服务接入（docs/05）。

场景：
1. ``--mock-services`` 全服务完整 Phase 1，strict 校验 0 error；
2. 模型“无”端到端：HTML 单元格 absent-claimed 且 raw 保留原文；
3. unified checkpoint 恢复：首批失败 → partial；重跑只请求未完成镜头 → complete；
4. 无配置（无 mock）回归：ASR/Unified 显式降级，strict 校验 0 error；
5. 音画同步切视频：audio-cuts 检出 synchronizedCut。

Apple Vision 在本机有 swiftc 时 camera-motion 真实 complete，否则
unavailable——两种都接受，但严格校验必须过。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from memoloupe.analysis.shot_pipeline import ShotAnalysisPipeline, ShotAnalysisRequest
from memoloupe.cli.shot_analysis import EXIT_OK, run_shot_analysis
from memoloupe.core.config import load_config
from memoloupe.services.mock import GROUP_OWNED_SECTIONS, MockUnifiedMediaService
from memoloupe.validate.cross_artifact import validate_output_dir
from memoloupe.validate.html_contract import validate_html

from conftest import E2E_SHOTS_ENV, synthesize_hardcut_video  # noqa: F401


def _read(out_dir: Path, name: str) -> dict:
    return json.loads((out_dir / "raw" / f"{name}.json").read_text(encoding="utf-8"))


def _strict_errors(out_dir: Path) -> list:
    issues = list(validate_output_dir(out_dir, strict=True))
    html = out_dir / "shot-analysis.html"
    if html.is_file():
        issues.extend(validate_html(html, root=out_dir, strict=True))
    return [i for i in issues if i.severity == "error"]


def _group_payload(group_name: str, shot_ids: list[str]) -> str:
    """按 GROUP_OWNED_SECTIONS 生成该组 owns 的合法响应（与 default mock 同构）。"""
    shots = []
    for sid in shot_ids:
        shot: dict = {"shotID": sid}
        for section, fields in GROUP_OWNED_SECTIONS[group_name].items():
            if section == "confidence":
                shot["confidence"] = {name: "medium" for name in fields}
            elif section == "components":
                shot["components"] = {"texts": [], "compositingEvents": "无"}
            else:
                shot[section] = {name: "无" for name in fields}
        shots.append(shot)
    return json.dumps({"shots": shots}, ensure_ascii=False)


@pytest.fixture(scope="module")
def mock_run_out(hardcut_video, tmp_path_factory) -> Path:
    """场景 1/2 共用：--mock-services 完整跑一遍 3 段硬切视频。"""
    out_dir = tmp_path_factory.mktemp("e2e-mock") / "out"
    with pytest.MonkeyPatch.context() as mp:
        for key, value in E2E_SHOTS_ENV.items():
            mp.setenv(key, value)
        code = run_shot_analysis(
            [
                str(hardcut_video),
                "--output-dir", str(out_dir),
                "--start-ms", "0",
                "--end-ms", "3000",
                "--mock-services",
            ]
        )
    assert code == EXIT_OK
    return out_dir


class TestMockFullRun:
    """场景 1：mock 全服务完整 Phase 1。"""

    def test_all_capabilities_real_or_explicitly_degraded(self, mock_run_out):
        out_dir = mock_run_out
        shots = _read(out_dir, "shots")
        shot_ids = [s["shotID"] for s in shots["shots"]]
        assert len(shot_ids) == 3

        asr = _read(out_dir, "asr")
        assert asr["status"] == "complete"
        assert asr["service"] == "asr"

        audio_cuts = _read(out_dir, "audio-cuts")
        assert audio_cuts["status"] == "complete"

        music = _read(out_dir, "music-flags")
        assert music["status"] == "complete"
        assert music["stateTally"]["unknown"] + music["stateTally"]["music"] + music[
            "stateTally"
        ]["silent"] == 3

        unified = _read(out_dir, "unified-media")
        assert unified["status"] == "complete"
        assert unified["shotStatuses"] == {sid: "succeeded" for sid in shot_ids}
        assert unified["terminal"] is True

        camera = _read(out_dir, "camera-motion")
        assert camera["analysis"]["capabilityStatus"] in ("complete", "unavailable")

        assert _strict_errors(out_dir) == []

    def test_model_absence_claim_end_to_end(self, mock_run_out):
        """场景 2：mock 的 visual.subjects=“无” → HTML absent-claimed，raw 保留原文。"""
        out_dir = mock_run_out
        unified = _read(out_dir, "unified-media")
        # raw 保留模型原文“无”
        found = False
        for batch in unified["batches"]:
            for shot in batch["response"]["shots"]:
                assert shot["visual"]["subjects"] == "无"
                found = True
        assert found

        html = (out_dir / "shot-analysis.html").read_text(encoding="utf-8")
        cells = re.findall(
            r'<td data-field="visual\.subjects" data-shot-id="(SH\d+)" '
            r'data-value-state="([^"]+)"',
            html,
        )
        assert len(cells) == 3
        assert all(state == "absent-claimed" for _, state in cells)


class TestCheckpointResume:
    """场景 3：unified 编排中断 → 首次 partial；同配置重跑只补未完成镜头。

    中断方式：脚本 mock 在最后一组（editing_function）的第二批抛非服务类
    异常（RuntimeError 不在编排器重试/回退范围内，原样传播——docs/03 §2.12
    “每次成功请求后立即 checkpoint”，已完成镜头因此保留在 checkpoints/）。
    """

    def test_resume_only_requests_incomplete_shots(self, hardcut_video, tmp_path):
        out_dir = tmp_path / "out"
        config = load_config(
            {"unifiedModel": {"batchSize": 2, "maxRetries": 0}},
            env=dict(E2E_SHOTS_ENV),
        )
        pipeline = ShotAnalysisPipeline()

        script: dict = {}
        for group in ("visual", "audio"):
            script[(group, ("SH0001", "SH0002"))] = _group_payload(
                group, ["SH0001", "SH0002"]
            )
            script[(group, ("SH0003",))] = _group_payload(group, ["SH0003"])
        script[("editing_function", ("SH0001", "SH0002"))] = _group_payload(
            "editing_function", ["SH0001", "SH0002"]
        )
        script[("editing_function", ("SH0003",))] = RuntimeError("编排中断")
        first_mock = MockUnifiedMediaService(script)
        first = pipeline.run(
            ShotAnalysisRequest(
                source=hardcut_video,
                output_dir=out_dir,
                config=config,
                unified_service=first_mock,
            )
        )
        # 编排异常 → unified 步骤失败、无终态产物，整体 partial（非致命）
        assert first.status == "partial"
        assert not (out_dir / "raw" / "unified-media.json").exists()
        # 已成功请求的批次已写入 checkpoint
        checkpoints = sorted((out_dir / "checkpoints").glob("unified-media-*.json"))
        assert len(checkpoints) == 3
        saved = {
            cp.name: set(json.loads(cp.read_text(encoding="utf-8"))["completedShotIDs"])
            for cp in checkpoints
        }
        assert saved["unified-media-visual.json"] == {"SH0001", "SH0002", "SH0003"}
        assert saved["unified-media-audio.json"] == {"SH0001", "SH0002", "SH0003"}
        assert saved["unified-media-editing_function.json"] == {"SH0001", "SH0002"}

        # 重跑（同 config，新 mock 全部成功）：只应请求未完成镜头
        def succeed(clips, group, call_index):
            return _group_payload(group.name, [c.shot_id for c in clips])

        second_mock = MockUnifiedMediaService(succeed)
        second = pipeline.run(
            ShotAnalysisRequest(
                source=hardcut_video,
                output_dir=out_dir,
                config=config,
                unified_service=second_mock,
            )
        )
        assert second.status == "complete"
        assert second_mock.calls, "checkpoint 恢复应发起补跑请求"
        for call in second_mock.calls:
            assert call["shot_ids"] == ("SH0003",), call["shot_ids"]
        unified = _read(out_dir, "unified-media")
        assert unified["status"] == "complete"
        assert all(v == "succeeded" for v in unified["shotStatuses"].values())
        assert _strict_errors(out_dir) == []


class TestNoConfigRegression:
    """场景 4：无配置无 mock —— 服务能力显式降级，确定性链路真实运行。"""

    def test_explicit_degradation_and_strict_validation(
        self, hardcut_video, tmp_path, shot_env
    ):
        out_dir = tmp_path / "out"
        code = run_shot_analysis(
            [
                str(hardcut_video),
                "--output-dir", str(out_dir),
                "--start-ms", "0",
                "--end-ms", "3000",
            ]
        )
        assert code == EXIT_OK
        assert _read(out_dir, "asr")["status"] == "skipped"
        assert _read(out_dir, "unified-media")["status"] == "skipped"
        camera = _read(out_dir, "camera-motion")
        assert camera["analysis"]["capabilityStatus"] in ("complete", "unavailable")
        assert _strict_errors(out_dir) == []


@pytest.fixture(scope="module")
def sync_cut_video(tmp_path_factory) -> Path:
    """0-2s 红+440Hz，2-4s 蓝+880Hz：画面硬切与音色突变都在 2s。"""
    out = tmp_path_factory.mktemp("e2e-sync") / "sync-cut.mp4"
    argv = [
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=red:size=320x240:rate=30:duration=2",
        "-f", "lavfi", "-i", "color=blue:size=320x240:rate=30:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=2",
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1[v];[2:a][3:a]concat=n=2:v=0:a=1[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "mpeg4", "-c:a", "aac",
        str(out),
    ]
    proc = subprocess.run(argv, capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    return out


class TestSynchronizedCut:
    """场景 5：音画同步切 → audio-cuts 分类 synchronizedCut。"""

    def test_synchronized_cut_detected(self, sync_cut_video, tmp_path, shot_env):
        out_dir = tmp_path / "out"
        code = run_shot_analysis(
            [str(sync_cut_video), "--output-dir", str(out_dir)]
        )
        assert code == EXIT_OK
        audio_cuts = _read(out_dir, "audio-cuts")
        assert audio_cuts["status"] == "complete"
        classifications = [
            side["classification"]
            for shot in audio_cuts["shots"]
            for side in (shot["boundaryIn"], shot["boundaryOut"])
        ]
        assert "synchronizedCut" in classifications
        assert _strict_errors(out_dir) == []
