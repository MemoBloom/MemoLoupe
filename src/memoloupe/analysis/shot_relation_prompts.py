"""切点语义受控字段：词表、prompt 渲染与响应解析（Phase 06-04）。

红线（docs 计划 §3）：

- 模型只输出受控枚举与摘要，不得触碰镜头边界、pair 集合或指标数值；
- 模型声称“没有连续性问题”只能落 ``absent-claimed``，不得落 ``absent``；
- 非法 JSON / 枚举越界 / 静帧不足时该字段落 ``unknown``，不得伪造结论。
"""

from __future__ import annotations

import json
import re

SEMANTIC_PROMPT_VERSION = "shot-relation-prompt.v1"

#: 受控词表（后续迁入 rules/vocabulary.json；CALIBRATION）。
SEMANTIC_FIELDS: dict[str, dict[str, object]] = {
    "actionContinuity": {
        "enum": ["动作承接", "动作跳跃", "无法判断"],
        "question": "左右镜头的动作是否连贯承接？静帧与摘要不足以判断时必须选“无法判断”。",
    },
    "eyelineContinuity": {
        "enum": ["视线承接", "视线冲突", "不适用", "无法判断"],
        "question": "若画面中人物有明确视线方向，切点前后视线是否匹配？",
    },
    "screenDirection": {
        "enum": ["方向连续", "方向反转", "不适用", "无法判断"],
        "question": "若存在明确运动方向或人物朝向，跨越切点后是否保持同一屏幕方向？",
    },
    "spatialTemporalRelation": {
        "enum": [
            "同空间连续",
            "跨空间",
            "时间跳跃",
            "蒙太奇并置",
            "无法判断",
        ],
        "question": "两个镜头在空间与时间上最可能是什么关系？",
    },
}
#: editMotivations 允许的多选值。
EDIT_MOTIVATIONS: tuple[str, ...] = (
    "动作",
    "对白",
    "声音",
    "节拍",
    "视觉匹配",
    "信息揭示",
    "对比",
    "时空推进",
)


def render_pair_prompt(payload: dict) -> str:
    """渲染单个 pair 的语义分析 prompt。

    ``payload`` 只允许包含白名单输入（docs 计划 §4.3）：
    shotID、时间范围、时长、确定性指标、边界帧存在的标记、ASR 摘要。
    """
    allowed = SEMANTIC_FIELDS
    enum_lines = "\n".join(
        f'- "{name}"：{" / ".join(spec["enum"])}。{spec["question"]}'  # type: ignore[arg-type]
        for name, spec in allowed.items()
    )
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"""你是专业剪辑拉片助手。分析一组相邻镜头切点（pair）的剪辑语义。

输入数据（白名单：结构化摘要、确定性指标、语音文本；不含完整视频）：
{payload_json}

请仅依据上述输入判断，逐项输出以下受控字段：
{enum_lines}
- "editMotivations"：从 {"、".join(EDIT_MOTIVATIONS)} 中选择 0-3 个，按可能性排序；
- "relationSummary"：不超过 60 字的中文摘要，说明这个切点"发生了什么"，不做好坏评价。

严格约束：
1. 静帧/文本证据不足以判断时，必须输出"无法判断"，不得猜测；
2. 只输出一个 JSON 对象，不要任何解释、markdown 代码块标记或多余文本；
3. 输出格式：
{{"actionContinuity": "...", "eyelineContinuity": "...", "screenDirection": "...", "spatialTemporalRelation": "...", "editMotivations": ["..."], "relationSummary": "..."}}
"""


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n|\n```$")


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE_RE.sub("", stripped)
    return stripped.strip()


class PairSemanticParseError(ValueError):
    """模型响应无法解析为合法语义字段。"""


def parse_pair_semantics(text: str, *, ref_base: str) -> dict:
    """解析模型响应为 Observation 形状的语义字段 dict。

    - 非法 JSON → 抛 :class:`PairSemanticParseError`（整个 pair 语义落 failed）；
    - 枚举越界 → 该字段落 ``unknown``（不污染其他字段，reason 记录原文值）；
    - 字段缺失 → 该字段落 ``unknown``。

    ``ref_base`` 为该 pair 的证据引用前缀，形如
    ``raw/shot-relations.json#relations[3]``。
    返回 ``{"fields": {...}, "raw": <原文本>}``。
    """
    try:
        data = json.loads(_strip_fence(text))
    except (json.JSONDecodeError, ValueError) as exc:
        raise PairSemanticParseError(f"模型响应不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise PairSemanticParseError("模型响应顶层不是 JSON 对象")

    fields: dict[str, dict] = {}

    def observation(value: object, confidence: str, note: str | None = None) -> dict:
        obs: dict = {
            "state": "value",
            "value": value,
            "confidence": confidence,
            "source": "textModel",
            "evidenceRefs": [f"{ref_base}.semantic.raw"],
            "verified": False,
        }
        if note:
            obs["note"] = note
        return obs

    def unknown_observation(reason: str) -> dict:
        return {
            "state": "unknown",
            "value": None,
            "confidence": "unknown",
            "source": "textModel",
            "evidenceRefs": [f"{ref_base}.semantic.raw"],
            "verified": False,
            "note": reason,
        }

    issues: list[str] = []
    for name, spec in SEMANTIC_FIELDS.items():
        raw_value = data.get(name)
        enum: tuple[str, ...] = tuple(spec["enum"])  # type: ignore[arg-type]
        if raw_value in enum:
            fields[name] = observation(raw_value, "medium")
        else:
            issues.append(f"{name}={raw_value!r} 不在受控枚举内")
            fields[name] = unknown_observation(f"枚举越界或缺失：{raw_value!r}")

    raw_motivations = data.get("editMotivations")
    if isinstance(raw_motivations, list):
        picked = [m for m in raw_motivations if m in EDIT_MOTIVATIONS]
        rejected = [m for m in raw_motivations if m not in EDIT_MOTIVATIONS]
        if rejected:
            issues.append(f"editMotivations 越界值被丢弃：{rejected!r}")
        if picked:
            fields["editMotivations"] = observation(
                picked, "low", note=None if not rejected else f"丢弃越界值 {rejected!r}"
            )
        else:
            fields["editMotivations"] = {
                "state": "absent-claimed",
                "value": [],
                "confidence": "low",
                "source": "textModel",
                "evidenceRefs": [
                    f"{ref_base}.semantic.raw"
                ],
                "verified": False,
            }
    else:
        issues.append(f"editMotivations={raw_motivations!r} 不是数组")
        fields["editMotivations"] = unknown_observation("缺失或不是数组")

    raw_summary = data.get("relationSummary")
    if isinstance(raw_summary, str) and raw_summary.strip():
        fields["relationSummary"] = observation(raw_summary.strip(), "low")
    else:
        issues.append(f"relationSummary={raw_summary!r} 非非空字符串")
        fields["relationSummary"] = unknown_observation("缺失或为空")

    result = {"fields": fields, "raw": text}
    if issues:
        result["issues"] = issues
    return result
