"""可编程 mock 服务：供单元/集成测试与 CLI ``--mock-services`` 使用。

两个 mock 都满足对应 Protocol，并记录每次调用参数供断言。
:func:`default_mock_unified` 按 ``schemas/unified-media.json`` 的 modelShot
结构生成合法的三组建模响应，每组只生成自己 owns 的字段。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Sequence

from memoloupe.services.asr import ASRRequest, ASRResult
from memoloupe.services.base import SERVICE_PROTOCOL_VERSION
from memoloupe.services.text_model import TextModelRequest
from memoloupe.services.unified_media import AnalysisGroup, ModelClip

__all__ = [
    "SERVICE_PROTOCOL_VERSION",
    "GROUP_OWNED_SECTIONS",
    "MockASRService",
    "MockTextModelService",
    "MockUnifiedMediaService",
    "default_mock_unified",
]

# 三组字段所有权（docs/03 §2.12：两组不得拥有同一字段）。
# visual 组含"文字和合成"（components）；editing_function 组含素材形态/
# 情绪/语气（function）与转场/连续性（editing）；confidence 按子键拆分。
GROUP_OWNED_SECTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "visual": {
        "visual": (
            "content", "subjects", "actions", "setting", "props", "framing",
            "subjectCoverage", "cameraAngle", "composition", "perspective",
            "lensFeel", "cameraMovement", "movementIntensity", "brightness",
            "contrast", "lightingType", "colorTemperature", "dominantColor",
            "saturation", "depthOfField", "texture",
        ),
        "components": ("texts", "compositingEvents"),
        "confidence": ("visual",),
    },
    "audio": {
        "audio": ("speech", "bgmStyle", "soundEffects"),
        "confidence": ("audio",),
    },
    "editing_function": {
        "function": ("sourceMedium", "subjectEmotion", "shotTone"),
        "editing": ("transition", "continuity"),
        "confidence": ("editing", "overall"),
    },
}


class MockASRService:
    """可编程 ASR mock：返回固定 segments 或抛出预设异常。"""

    def __init__(
        self,
        segments: list[dict] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._segments = tuple(segments or [])
        self._error = error
        self.calls: list[tuple[Path, ASRRequest]] = []

    def transcribe(self, media_path: Path, request: ASRRequest) -> ASRResult:
        self.calls.append((media_path, request))
        if self._error is not None:
            raise self._error
        return ASRResult(segments=self._segments, raw_extras={"mock": True})


# script 的值类型：返回文本、抛异常，或按调用动态生成。
_ScriptOutcome = str | Exception
_ScriptKey = tuple[str, tuple[str, ...]] | int
MockScript = (
    dict[_ScriptKey, _ScriptOutcome]
    | Callable[[Sequence[ModelClip], AnalysisGroup, int], str]
)


class MockUnifiedMediaService:
    """可编程 UnifiedMLLM mock。

    ``script`` 支持两种编排键（可混合）：

    - ``(group.name, tuple(shot_ids))``：精确匹配某次批次；
    - ``int``：按调用序号（从 0 开始）编排。

    值为 ``str`` 时原样返回（成功 JSON / fence 包裹 / 非法 JSON / 漏 shot /
    重复 shot / 未知 shot / "无" 值都由此构造）；值为 ``Exception`` 时抛出
    （如 TransientServiceError 的 429/500/timeout、PermanentServiceError）。
    传入 callable 时签名为 ``(clips, group, call_index) -> str``。
    """

    def __init__(
        self,
        script: MockScript,
        *,
        model: str = "mock-model",
        fallback_model: str | None = None,
    ) -> None:
        self._script = script
        self._model = model
        self._fallback_model = fallback_model
        self.calls: list[dict] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def fallback_model(self) -> str | None:
        return self._fallback_model

    def with_model(self, model: str) -> "MockUnifiedMediaService":
        """返回共享 script 的新 mock（05-01B：编排器 fallback 重发）。

        ``calls`` 独立记录，便于断言两次请求使用的模型不同。
        """
        return MockUnifiedMediaService(
            self._script, model=model, fallback_model=self._fallback_model
        )

    def analyze_batch(
        self, clips: Sequence[ModelClip], group: AnalysisGroup
    ) -> str:
        call_index = len(self.calls)
        shot_ids = tuple(c.shot_id for c in clips)
        self.calls.append(
            {
                "group": group.name,
                "shot_ids": shot_ids,
                "clips": tuple(clips),
                "analysis_group": group,
                "model": self._model,
            }
        )
        if callable(self._script):
            return self._script(clips, group, call_index)
        key = (group.name, shot_ids)
        if key in self._script:
            outcome = self._script[key]
        elif call_index in self._script:
            outcome = self._script[call_index]
        else:
            raise KeyError(
                f"mock 未编排的调用: group={group.name} shots={shot_ids} "
                f"call_index={call_index}"
            )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class MockTextModelService:
    """可编程文本模型 mock（story/profile 编排测试用）。

    ``script`` 键为调用序号（从 0 开始）；值为 ``str`` 时原样返回（成功 JSON /
    fence 包裹 / 非法 JSON / 漏 block / 未知 block 都由此构造），值为
    ``Exception`` 时抛出（TransientServiceError / PermanentServiceError）。
    传入 callable 时签名为 ``(request: TextModelRequest) -> str``，可读取
    prompt 动态构造响应（如 CLI ``--mock-text-model`` 需要按块 ID 回填）。
    """

    def __init__(self, script: dict[int, str | Exception] | Callable[[TextModelRequest], str]) -> None:
        self._script = script
        self.calls: list[TextModelRequest] = []

    def generate(self, request: TextModelRequest) -> str:
        call_index = len(self.calls)
        self.calls.append(request)
        if callable(self._script):
            return self._script(request)
        if call_index not in self._script:
            raise KeyError(f"mock 未编排的调用: call_index={call_index}")
        outcome = self._script[call_index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _default_shot_fields(group_name: str, shot_id: str) -> dict:
    """为指定组生成该组 owns 的合法字段值。"""
    owned = GROUP_OWNED_SECTIONS[group_name]
    shot: dict = {"shotID": shot_id}
    for section, fields in owned.items():
        if section == "confidence":
            shot["confidence"] = {name: "medium" for name in fields}
        elif section == "components":
            shot["components"] = {
                "texts": [],
                "compositingEvents": "无",
            }
        else:
            shot[section] = {name: "无" for name in fields}
    return shot


def default_mock_unified(shot_ids: Sequence[str]) -> MockUnifiedMediaService:
    """生成覆盖全部 modelShot 字段组的合法三组建模响应。

    返回文本形状为 ``{"shots": [{"shotID", ...该组 owns 的 section...}]}``，
    可直接进入编排器的 fence 剥离 / schema 校验 / shotID 对齐流程。
    """

    def script(
        clips: Sequence[ModelClip], group: AnalysisGroup, call_index: int
    ) -> str:
        known = [c.shot_id for c in clips if c.shot_id in shot_ids]
        payload = {
            "shots": [_default_shot_fields(group.name, sid) for sid in known]
        }
        return json.dumps(payload, ensure_ascii=False)

    return MockUnifiedMediaService(script)
