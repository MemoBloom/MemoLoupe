"""analysis.media_groups — unified-media v2 三组字段分组与严格响应契约。

三组字段所有权的单一事实源是 ``services.mock.GROUP_OWNED_SECTIONS``
（mock 响应生成器与本模块共用同一张表，保证 mock 输出必然过组 schema）：

- ``visual``：视觉原子语义 + 画面图层事件 + confidence.visual；
- ``audio``：BGM 风格/声音事件 + confidence.audio（speech 归 ASR）；
- ``function``：素材形态/人物情绪/镜头语气 + confidence.function。

不变量：两组不得拥有同一字段；:func:`build_groups` 启动时自检，重叠直接 raise
（docs/03 §2.12：字段重叠视为 schema 编程错误）。
"""

from __future__ import annotations

import json

from memoloupe.analysis.vocabulary import Vocabulary
from memoloupe.core.errors import ConfigError
from memoloupe.core.hashing import fingerprint
from memoloupe.services.mock import GROUP_OWNED_SECTIONS
from memoloupe.services.unified_media import AnalysisGroup

#: 响应解析器实现版本，进入组 fingerprint（docs/03 §5 失效矩阵）。
PARSER_VERSION = "groups.v3"

#: 组的固定执行/合并顺序。
GROUP_ORDER: tuple[str, ...] = ("visual", "audio", "function")

_CONFIDENCE_ENUM = ["high", "medium", "low", "unknown"]

_TEXT_ITEM_FIELDS = ("textContent", "textType", "textStyle", "textAnimation")

#: 字段中文说明（注入 prompt；键为 ``<section>.<field>`` 点路径）。
_FIELD_LABELS: dict[str, str] = {
    "visual.subjects": "画面主体",
    "visual.actions": "主体动作",
    "visual.setting": "场景环境",
    "visual.props": "道具",
    "visual.framing": "景别",
    "visual.cameraAngle": "摄影机角度",
    "visual.composition": "构图",
    "visual.viewpoint": "叙事观看关系/视角",
    "visual.perceivedLensFeel": "感知到的镜头透视感（非焦段事实）",
    "visual.cameraMovement": "运镜现象",
    "visual.brightness": "亮度",
    "visual.contrast": "对比度",
    "visual.lightingSource": "光源来源",
    "visual.perceivedColorTemperature": "感知色温",
    "visual.dominantColor": "主色",
    "visual.saturation": "饱和度",
    "visual.depthOfField": "景深",
    "visual.imageTexture": "成像质感",
    "components.texts": "画面文字/后期文字",
    "components.nonTextOverlayEvents": "非文字后期图层/合成事件",
    "audio.bgmStyle": "BGM 风格（只描述风格，不判断有无）",
    "audio.soundEvents": "可听声音事件",
    "function.sourceMedium": "素材形态",
    "function.subjectEmotion": "人物情绪",
    "function.shotTone": "镜头语气",
    "confidence.visual": "视觉字段自评置信度",
    "confidence.audio": "声音字段自评置信度",
    "confidence.function": "功能语义字段自评置信度",
}


def flatten_fields(sections: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """把组 owns 的 section 表展平为 ``<section>.<field>`` 点路径元组。"""
    return tuple(
        f"{section}.{field}"
        for section, fields in sections.items()
        for field in fields
    )


def _check_no_overlap(ownership: dict[str, dict[str, tuple[str, ...]]]) -> None:
    """字段所有权自检：任一路径被两组拥有即视为 schema 编程错误。"""
    seen: dict[str, str] = {}
    for group_name, sections in ownership.items():
        for path in flatten_fields(sections):
            owner = seen.get(path)
            if owner is not None:
                raise ConfigError(
                    f"组字段重叠：{path} 同时属于 {owner} 与 {group_name}（schema 编程错误）"
                )
            seen[path] = group_name


def _text_item_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(_TEXT_ITEM_FIELDS),
        "properties": {name: {"type": "string"} for name in _TEXT_ITEM_FIELDS},
    }


def _section_schema(group_name: str, section: str) -> dict:
    fields = GROUP_OWNED_SECTIONS[group_name][section]
    if section == "components":
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["texts", "nonTextOverlayEvents"],
            "properties": {
                "texts": {"type": "array", "items": _text_item_schema()},
                "nonTextOverlayEvents": {"type": "string"},
            },
        }
    if section == "confidence":
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(fields),
            "properties": {
                name: {"type": "string", "enum": _CONFIDENCE_ENUM} for name in fields
            },
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(fields),
        "properties": {name: {"type": "string"} for name in fields},
    }


def group_schema(group_name: str) -> dict:
    """组响应 schema：{"shots": [{shotID, <本组 owns 的 section...>}...]}。

    shot 项与 section 均 ``additionalProperties: false``：模型多返回字段
    （含其他组的字段）会导致批次校验失败，而不是被静默吞掉。
    """
    sections = GROUP_OWNED_SECTIONS[group_name]
    shot_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["shotID", *sections.keys()],
        "properties": {
            "shotID": {"type": "string", "pattern": "^SH\\d{4}$"},
            **{section: _section_schema(group_name, section) for section in sections},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["shots"],
        "properties": {"shots": {"type": "array", "items": shot_item}},
    }


def shot_item_schema(group: AnalysisGroup) -> dict:
    """组 schema 中单个 shot 项的 schema（checkpoint 复用项校验用）。"""
    return group.schema["properties"]["shots"]["items"]


def _response_template(group_name: str) -> str:
    """生成嵌套 section 示例，避免模型把点路径误当作 JSON key。"""
    shot: dict = {"shotID": "SH0001"}
    for section, fields in GROUP_OWNED_SECTIONS[group_name].items():
        if section == "components":
            shot[section] = {"texts": [], "nonTextOverlayEvents": "unknown"}
        else:
            shot[section] = {field: "unknown" for field in fields}
    return json.dumps({"shots": [shot]}, ensure_ascii=False, indent=2)


def _prompt(group_name: str, vocab: Vocabulary, config: dict) -> str:
    model_cfg = config.get("unifiedModel", {}) if isinstance(config, dict) else {}
    video_fps = model_cfg.get("videoFPS", 10.0)
    resolution = model_cfg.get("mediaResolution", "default")
    sections = GROUP_OWNED_SECTIONS[group_name]

    lines = [
        "你是视频拉片分析助手。输入是若干镜头 clip"
        f"（video Data URI，fps={video_fps}，分辨率 {resolution}）。",
        f"本请求只分析【{group_name}】组的以下字段，逐字段给出结果：",
    ]
    for path in flatten_fields(sections):
        label = _FIELD_LABELS.get(path, path)
        lines.append(f"- {path}：{label}。{vocab.prompt_fragment(path)}")
    if "components" in sections:
        lines.append(
            "- components.texts 是数组，每项为 "
            '{"textContent", "textType", "textStyle", "textAnimation"}；'
            "画面无文字时输出空数组 []。"
        )
        lines.append(f"  textType：{vocab.prompt_fragment('components.texts.textType')}")
        lines.append(
            f"  textAnimation：{vocab.prompt_fragment('components.texts.textAnimation')}"
        )
    lines += [
        "输出要求：",
        "- 严格使用下面的嵌套 JSON 结构模板；点号只表示字段路径，绝对不能"
        "把 audio.bgmStyle、function.sourceMedium 等点路径当作 JSON key。",
        f"JSON 结构模板：\n{_response_template(group_name)}",
        "- 只输出一个 JSON 对象；禁止 Markdown 代码围栏和任何额外文字。",
        "- shotID 必须原样返回；每个输入镜头在 shots 中恰好出现一次，"
        "不得遗漏，不得返回未请求的镜头。",
        '- 没有的内容写 "无"；无法判断的写 "unknown"；'
        "confidence 只能取 high/medium/low/unknown。",
    ]
    return "\n".join(lines)


def build_groups(vocab: Vocabulary, config: dict) -> list[AnalysisGroup]:
    """构造三组分析任务（prompt 已注入词表，fingerprint 含解析器版本）。

    启动时自检字段所有权，两组拥有同一字段直接 raise ConfigError。
    """
    ownership = GROUP_OWNED_SECTIONS
    _check_no_overlap(ownership)
    groups: list[AnalysisGroup] = []
    for name in GROUP_ORDER:
        schema = group_schema(name)
        prompt = _prompt(name, vocab, config)
        fp = fingerprint(
            {
                "prompt": prompt,
                "schema": schema,
                "vocabVersion": vocab.version,
                "parser": PARSER_VERSION,
            }
        )
        groups.append(
            AnalysisGroup(
                name=name,
                fields=flatten_fields(ownership[name]),
                prompt=prompt,
                schema=schema,
                fingerprint=fp,
            )
        )
    return groups
