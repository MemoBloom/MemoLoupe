"""analysis.media_orchestrator — UnifiedMLLM 三组分析编排（docs/03 §2.12、docs/02 §4.7）。

执行模型：

- 三组（visual / audio / function）顺序执行；每组内按
  ``unifiedModel.batchSize``（默认 4）分批，批次用线程池并发，另有一把
  独立的模型信号量（``unifiedModel.concurrency``，默认 10）限制在途请求。
- 响应解析严格按 docs/03 §2.12 顺序：模型文本 → 去单层 Markdown fence →
  ``json.loads`` → 组 schema 校验 → 请求/响应 shotID 集合完全一致 →
  按 shotID 提取。禁止用正则从长文本猜多个 JSON 对象后拼接。
- 重试：TransientServiceError 与非法/不合法响应按指数退避重试
  （默认最多 3 次重试，基数 1s）；PermanentServiceError 不重试。
  批次最终失败回退单镜头逐个请求；单镜头仍失败（PermanentServiceError
  或重试耗尽）记 ``permanent_failure``。
- checkpoint：``checkpoints/unified-media-<group>.json``，每次成功请求后
  立即原子写入（fingerprint + 已完成 shotID 集合 + 已校验结果）；
  重跑只请求未完成镜头，且只复用通过组 schema 校验的成功项。
- 合并：三组结果按 shotID + 字段路径合并为完整 modelShot（confidence
  子字段按组填入）；缺失字段不得用其他 shot 补齐；未完成镜头不进
  response，只通过 shotStatuses 表达。
- 归一化不在本层：模型原始值（含 "无"）原样保留进 raw，
  absent-claimed 等语义归 Observation 层（docs/02 §4.7）。

fallback（05-01B）：服务暴露 ``fallback_model`` 且支持 ``with_model`` 时，
编排器构造 fallback 适配器并在主模型失败后按 ``fallbackModel`` 重发——
先批次级重发，再（若仍失败）单镜头级先主后备。fallback 构造失败按无
fallback 显式降级（不静默换服务）。产物记录：成功批次 ``fallbackUsed``，
失败镜头 ``fallbackAttempted``/``fallbackFailed``。协议
``analyze_batch(clips, group)`` 保持冻结不变。
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import jsonschema

from memoloupe.analysis.media_groups import GROUP_ORDER, build_groups, shot_item_schema
from memoloupe.analysis.vocabulary import Vocabulary
from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.artifacts.store import ArtifactStore
from memoloupe.core.atomic_io import read_json, write_json_atomic
from memoloupe.core.errors import MemoLoupeError
from memoloupe.core.hashing import fingerprint
from memoloupe.core.logging import get_logger
from memoloupe.media.clips import PROXY_WIDTH, SHORT_CLIP_MS
from memoloupe.services.base import PermanentServiceError, TransientServiceError
from memoloupe.services.unified_media import AnalysisGroup, ModelClip, UnifiedMediaService

UNIFIED_MEDIA_VERSION = "unified.v3"

#: 指数退避基数（秒）：第 n 次重试前睡 base * 2**n（n 从 0 起）。
_RETRY_BASE_SEC = 1.0

_logger = get_logger("memoloupe.analysis.media_orchestrator", phase="shot", step="unified")


class _BatchError(Exception):
    """可重试的响应错误：非法 JSON、组 schema 校验失败、shotID 集合不一致。"""


class _BatchExhausted(Exception):
    """暂时性错误重试耗尽；由分区逻辑回退到单镜头。"""


# ---------------------------------------------------------------------------
# 响应解析（docs/03 §2.12 顺序的第 3–6 步；第 1–2 步在 services 层完成）
# ---------------------------------------------------------------------------


def _strip_single_fence(text: str) -> str:
    """移除单层 Markdown 代码围栏（```json ... ```）；无围栏或不闭合时原样返回。

    只剥一层，不递归；禁止用正则从任意长文本中猜多个 JSON 对象。
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3:
        return stripped
    info = lines[0][3:].strip()
    if info and not info.isalpha():
        return stripped
    if lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _schema_validator(schema: dict) -> jsonschema.Validator:
    return jsonschema.validators.validator_for(schema)(schema)


def _parse_group_response(
    text: str,
    group: AnalysisGroup,
    expected_ids: Sequence[str],
    validator: jsonschema.Validator,
) -> dict[str, dict]:
    """解析并校验一批次响应，返回 {shotID: shot payload}（按 ID 索引）。

    任何一步失败抛 :class:`_BatchError`（可重试）。
    """
    stripped = _strip_single_fence(text)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise _BatchError(f"非法 JSON: {exc.msg}") from None
    try:
        validator.validate(data)
    except jsonschema.ValidationError as exc:
        raise _BatchError(f"组 schema 校验失败: {exc.message}") from None
    shots = data["shots"]
    response_ids = [shot["shotID"] for shot in shots]
    # Counter 比较同时捕获漏项、重复与未知 ID。
    if Counter(response_ids) != Counter(expected_ids):
        raise _BatchError(
            f"shotID 集合不一致: 请求 {sorted(expected_ids)} 响应 {sorted(response_ids)}"
        )
    return {shot["shotID"]: shot for shot in shots}


# ---------------------------------------------------------------------------
# 请求与重试
# ---------------------------------------------------------------------------


def _request_validated(
    service: UnifiedMediaService,
    fallback_service: UnifiedMediaService | None,
    clips: list[ModelClip],
    group: AnalysisGroup,
    max_retries: int,
    sleep: Callable[[float], None],
    validator: jsonschema.Validator,
    counter: list[int],
) -> tuple[dict[str, dict], bool]:
    """带重试的已校验请求：返回 ``({shotID: payload}, used_fallback)``。

    - 主模型按 max_retries 指数退避重试（PermanentServiceError 立即上抛）；
    - 主模型耗尽（Transient/_BatchError）或 Permanent 失败后，若提供
      ``fallback_service`` 则以 fallback 模型重发一轮（同样按 max_retries
      重试），成功返回 ``(shots, True)``；
    - fallback 也失败时原样上抛（保持现有 partial/failed 降级语义）。
    - 其他异常（如 mock 注入的崩溃）不在此捕获，原样传播。
    """
    expected_ids = [clip.shot_id for clip in clips]

    def attempt(provider: UnifiedMediaService) -> dict[str, dict]:
        last: Exception | None = None
        for attempt_index in range(max_retries + 1):
            counter[0] += 1
            try:
                text = provider.analyze_batch(clips, group)
                return _parse_group_response(text, group, expected_ids, validator)
            except PermanentServiceError:
                raise
            except (TransientServiceError, _BatchError) as exc:
                last = exc
                if attempt_index < max_retries:
                    sleep(_RETRY_BASE_SEC * (2**attempt_index))
        raise _BatchExhausted(str(last)) from last

    try:
        return attempt(service), False
    except (PermanentServiceError, _BatchExhausted):
        if fallback_service is None:
            raise
        # 主模型失败：按 fallbackModel 重发（不递归 fallback）。
        return attempt(fallback_service), True


def _analyze_partition(
    clips: list[ModelClip],
    group: AnalysisGroup,
    service: UnifiedMediaService,
    fallback_service: UnifiedMediaService | None,
    max_retries: int,
    sleep: Callable[[float], None],
    validator: jsonschema.Validator,
    semaphore: threading.Semaphore,
    on_success: Callable[[dict[str, dict]], None],
    counter: list[int],
) -> tuple[list[str], dict[str, bool]]:
    """分析一个批次；批次失败先按 fallbackModel 重发，再回退单镜头
    （单镜头同样先主模型后 fallback）。

    返回 ``(永久失败 shotID 列表, {"attempted": bool, "used": bool})``。
    """
    stats = {"attempted": False, "used": False}

    def run(
        provider: UnifiedMediaService | None, shot_set: list[ModelClip]
    ) -> tuple[list[str], bool]:
        """尝试一次请求；成功返回 ([], used_fallback)，失败返回 (shotIDs, False)。"""
        if provider is None:
            return [c.shot_id for c in shot_set], False
        try:
            with semaphore:
                shots, used_fallback = _request_validated(
                    provider, None, shot_set, group,
                    max_retries, sleep, validator, counter,
                )
            on_success(shots)
            return [], used_fallback
        except (PermanentServiceError, _BatchExhausted):
            return [c.shot_id for c in shot_set], False

    failed, used = run(service, clips)
    stats["used"] = used
    if failed:
        stats["attempted"] = fallback_service is not None
        if fallback_service is not None:
            fb_failed, fb_used = run(fallback_service, clips)
            # fallback 批次成功（fb_failed 为空）即视为 fallback 生效。
            stats["used"] = stats["used"] or fb_used or not fb_failed
            if not fb_failed:
                return [], stats
            # 批次 fallback 也失败：单镜头逐个（主 → fallback）。
            failed = []
            for clip in clips:
                single_failed, _ = run(service, [clip])
                if not single_failed:
                    continue
                fb_single_failed, fb_single_used = run(fallback_service, [clip])
                stats["used"] = stats["used"] or fb_single_used or not fb_single_failed
                if fb_single_failed:
                    failed.append(clip.shot_id)
        else:
            # 无 fallback：批次失败后单镜头主模型重试（原行为）。
            failed = []
            for clip in clips:
                single_failed, _ = run(service, [clip])
                if single_failed:
                    failed.append(clip.shot_id)
    return failed, stats


# ---------------------------------------------------------------------------
# checkpoint（与最终 artifact 分离；只复用通过组 schema 校验的成功项）
# ---------------------------------------------------------------------------


def _checkpoint_path(store: ArtifactStore, group: AnalysisGroup) -> Path:
    return store.root / "checkpoints" / f"unified-media-{group.name}.json"


def _write_checkpoint(
    store: ArtifactStore,
    group: AnalysisGroup,
    batch_size: int,
    results: dict[str, dict],
) -> None:
    payload = {
        "version": UNIFIED_MEDIA_VERSION,
        "group": group.name,
        "fingerprint": group.fingerprint,
        "batchSize": batch_size,
        "completedShotIDs": sorted(results),
        "results": results,
    }
    write_json_atomic(_checkpoint_path(store, group), payload)


def _load_checkpoint(
    store: ArtifactStore,
    group: AnalysisGroup,
    batch_size: int,
    valid_ids: set[str],
) -> dict[str, dict]:
    """加载组 checkpoint；指纹/版本/批次参数不匹配或文件损坏时视为无 checkpoint。"""
    try:
        data = read_json(_checkpoint_path(store, group))
    except (MemoLoupeError, OSError):
        return {}
    if (
        data.get("version") != UNIFIED_MEDIA_VERSION
        or data.get("fingerprint") != group.fingerprint
        or data.get("batchSize") != batch_size
    ):
        return {}
    raw_results = data.get("results")
    if not isinstance(raw_results, dict):
        return {}
    validator = _schema_validator(shot_item_schema(group))
    results: dict[str, dict] = {}
    for shot_id, payload in raw_results.items():
        if (
            isinstance(shot_id, str)
            and shot_id in valid_ids
            and validator.is_valid(payload)
        ):
            results[shot_id] = payload
    return results


# ---------------------------------------------------------------------------
# 组执行与合并
# ---------------------------------------------------------------------------


def _run_group(
    store: ArtifactStore,
    group: AnalysisGroup,
    partitions: list[list[str]],
    clip_index: dict[str, ModelClip],
    service: UnifiedMediaService,
    fallback_service: UnifiedMediaService | None,
    *,
    batch_size: int,
    concurrency: int,
    max_retries: int,
    sleep: Callable[[float], None],
) -> tuple[dict[str, dict], dict[int, int], list[str], dict[int, dict[str, bool]]]:
    """执行一个组：返回 ({shotID: payload}, {分区号: 请求次数},
    永久失败 shotIDs, {分区号: fallback 统计})。"""
    valid_ids = {sid for part in partitions for sid in part}
    results = _load_checkpoint(store, group, batch_size, valid_ids)
    attempts: dict[int, int] = {}
    fallback_stats: dict[int, dict[str, bool]] = {}
    failed: list[str] = []
    todo = [
        (p, [sid for sid in part if sid not in results])
        for p, part in enumerate(partitions)
    ]
    todo = [(p, sids) for p, sids in todo if sids]
    if not todo:
        return results, attempts, failed, fallback_stats

    validator = _schema_validator(group.schema)
    semaphore = threading.Semaphore(concurrency)
    lock = threading.Lock()

    def on_success(shots: dict[str, dict]) -> None:
        # 每次成功请求后立即 checkpoint（原子写）。
        with lock:
            results.update(shots)
            _write_checkpoint(store, group, batch_size, results)

    def work(partition_index: int, shot_ids: list[str]) -> tuple[list[str], dict[str, bool]]:
        counter = [0]
        clips = [clip_index[sid] for sid in shot_ids]
        failed_ids, stats = _analyze_partition(
            clips,
            group,
            service,
            fallback_service,
            max_retries,
            sleep,
            validator,
            semaphore,
            on_success,
            counter,
        )
        with lock:
            attempts[partition_index] = counter[0]
            fallback_stats[partition_index] = stats
        return failed_ids

    workers = max(1, min(concurrency, len(todo)))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix=f"unified-{group.name}"
    ) as pool:
        # 按提交顺序取结果；输出语义只依赖 shotID 合并，与完成顺序无关。
        futures = [pool.submit(work, p, sids) for p, sids in todo]
        for future in futures:
            failed.extend(future.result())
    return results, attempts, failed, fallback_stats


def _merge_model_shot(group_results: dict[str, dict[str, dict]], shot_id: str) -> dict | None:
    """按 shotID + 字段路径合并三组结果为完整 modelShot；任一组缺失返回 None。"""
    merged: dict = {"shotID": shot_id}
    for group_name in GROUP_ORDER:
        payload = group_results[group_name].get(shot_id)
        if payload is None:
            return None
        for key, value in payload.items():
            if key == "shotID":
                continue
            if key == "confidence":
                # confidence 子字段按组 ownership 填入，互不覆盖。
                merged.setdefault("confidence", {}).update(value)
            else:
                merged[key] = value
    return merged


def _service_model_name(service: UnifiedMediaService) -> str:
    """模型名从 service 属性取（mock 可提供 .model，OpenAI 适配器为 ._model）。"""
    name = getattr(service, "model", None) or getattr(service, "_model", None)
    return str(name) if name else "mock"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run_unified_media_analysis(
    store: ArtifactStore,
    clips_info: list[dict],
    service: UnifiedMediaService,
    *,
    config: dict,
    vocab: Vocabulary,
    source_revision: str,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """执行三组分析并返回 unified-media.json 内容（不自行写 artifact）。

    服务不可用（service=None / CapabilityUnavailableError）由调用方决定降级，
    本函数不处理；调用方可用 :func:`build_skipped_unified_media` 生成显式
    skipped 产物。除 PermanentServiceError/TransientServiceError 外的异常
    （如实现 bug 或注入的崩溃）原样传播，已完成的请求已写入 checkpoint，
    重跑只会请求未完成镜头。
    """
    sleep_fn = sleep if sleep is not None else time.sleep
    model_cfg = config.get("unifiedModel", {})
    batch_size = max(1, int(model_cfg.get("batchSize", 4)))
    concurrency = max(1, int(model_cfg.get("concurrency", 10)))
    max_retries = max(0, int(model_cfg.get("maxRetries", 3)))

    groups = build_groups(vocab, config)
    shot_ids = [str(clip["shotID"]) for clip in clips_info]
    clip_index = {
        sid: ModelClip(
            shot_id=sid,
            proxy_path=store.root / str(clip["modelFile"]),
            duration_ms=int(clip.get("modelDurationMs", clip.get("durationMs", 0))),
        )
        for sid, clip in zip(shot_ids, clips_info)
    }
    partitions = [
        shot_ids[i : i + batch_size] for i in range(0, len(shot_ids), batch_size)
    ]

    group_results: dict[str, dict[str, dict]] = {}
    group_attempts: dict[str, dict[int, int]] = {}
    group_fallback: dict[str, dict[int, dict[str, bool]]] = {}
    # fallback 服务：服务暴露 fallback_model 且支持 with_model 时构造
    # （显式配置，不静默换服务；构造失败按无 fallback 降级）。
    fallback_service: UnifiedMediaService | None = None
    fb_model = getattr(service, "fallback_model", None)
    with_model = getattr(service, "with_model", None)
    if fb_model and callable(with_model):
        try:
            fallback_service = with_model(fb_model)
        except Exception as exc:  # noqa: BLE001 —— fallback 构造失败显式降级
            _logger.debug(f"fallback service 构造失败，按无 fallback 处理：{exc}")
    for group in groups:
        results, attempts, failed, fallback_stats = _run_group(
            store,
            group,
            partitions,
            clip_index,
            service,
            fallback_service,
            batch_size=batch_size,
            concurrency=concurrency,
            max_retries=max_retries,
            sleep=sleep_fn,
        )
        group_results[group.name] = results
        group_attempts[group.name] = attempts
        group_fallback[group.name] = fallback_stats
        if failed:
            _logger.debug(
                f"group={group.name} permanent failures: {','.join(sorted(failed))}"
            )

    # 合并：shot 只有在三组都成功时才算 succeeded（modelShot 必须完整）。
    succeeded = {
        sid
        for sid in shot_ids
        if all(sid in group_results[group.name] for group in groups)
    }
    merged = {sid: _merge_model_shot(group_results, sid) for sid in shot_ids}

    fallback_model = getattr(service, "fallback_model", None)
    batch_records: list[dict] = []
    for p, part in enumerate(partitions):
        total_attempts = sum(group_attempts[g.name].get(p, 0) for g in groups)
        # 分区级 fallback 统计：三组取或。
        fb_attempted = any(
            group_fallback[g.name].get(p, {}).get("attempted", False) for g in groups
        )
        fb_used = any(
            group_fallback[g.name].get(p, {}).get("used", False) for g in groups
        )
        if part and all(sid in succeeded for sid in part):
            record: dict = {
                "shotIDs": list(part),
                "status": "complete",
                "response": {"shots": [merged[sid] for sid in part]},
            }
            if total_attempts:
                record["attempts"] = total_attempts
            if fb_used:
                # 该分区至少一次请求经 fallback 模型成功。
                record["fallbackUsed"] = True
            batch_records.append(record)
            continue
        # 部分失败的分区拆成单镜头记录：失败批次不得伪造 response，
        # 成功镜头的 modelShot 也不因同批失败而丢失。
        for sid in part:
            if sid in succeeded:
                record = {
                    "shotIDs": [sid],
                    "status": "complete",
                    "response": {"shots": [merged[sid]]},
                }
                if fb_used:
                    record["fallbackUsed"] = True
            else:
                record = {"shotIDs": [sid], "status": "failed"}
                if fb_attempted:
                    # 尝试过 fallback 仍失败：记录 fallback 证据（05-01B）。
                    record["fallbackAttempted"] = True
                    record["fallbackFailed"] = True
            if total_attempts:
                record["attempts"] = total_attempts
            batch_records.append(record)
    for index, record in enumerate(batch_records, 1):
        record["batchID"] = f"B{index:04d}"

    shot_statuses = {
        sid: ("succeeded" if sid in succeeded else "permanent_failure")
        for sid in shot_ids
    }
    completed = len(succeeded)
    permanent_failures = len(shot_ids) - completed
    if permanent_failures == 0:
        status = "complete"
    elif completed == 0:
        status = "failed"
    else:
        status = "partial"

    document = {
        "schemaVersion": 3,
        "service": "unifiedAudioVideo",
        "schemaFingerprint": fingerprint(
            {"groupFingerprints": {group.name: group.fingerprint for group in groups}}
        ),
        "request": {
            "model": _service_model_name(service),
            "fallbackModel": fallback_model,
            "clipTransport": "mediaDataURI",
            "batchSize": batch_size,
            "concurrency": concurrency,
            "externalFrameExtraction": False,
            "videoFPS": float(model_cfg.get("videoFPS", 10.0)),
            "mediaResolution": str(model_cfg.get("mediaResolution", "default")),
            "sourceRevisionID": source_revision,
            "shortClipPolicy": {
                "minimumDurationMs": SHORT_CLIP_MS,
                "imageProxyWidth": PROXY_WIDTH,
            },
        },
        "retryPolicy": {
            "maxRetries": max_retries,
            "fallbackFromBatchToSingleShot": True,
            "checkpointAfterEachRequest": True,
        },
        "clips": clips_info,
        "batches": batch_records,
        "shotStatuses": shot_statuses,
        "completedShots": completed,
        # 终态输出中失败即永久失败（暂时性失败已在重试中耗尽）；
        # failedShots 保留给非终态中断场景，本函数恒为 0。
        "failedShots": 0,
        "pendingShots": 0,
        "permanentFailureShots": permanent_failures,
        "terminal": True,
        "status": status,
    }
    validate_artifact(ArtifactName.UNIFIED_MEDIA, document)
    return document


def build_skipped_unified_media(
    clips_info: list[dict], config: dict, source_revision: str
) -> dict:
    """服务不可用时的显式降级产物（M1 stub 语义：skipped、全 pending、terminal=false）。"""
    from memoloupe.analysis.shot_pipeline import build_unified_media_stub

    return build_unified_media_stub(
        clips_info, {"source": {"revisionID": source_revision}}, config
    )
