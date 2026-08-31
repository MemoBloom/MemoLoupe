"""analysis.story_prompts — story 文本模型 prompt 构造（docs/03 §3.1/§3.3、roadmap 03-03）。

铁律：prompt 只含文本摘要与受控词表，**不发送视频、帧、Data URI、
clip/代理路径或源文件路径**（编排层测试断言 payload 不含这些形态）。

受控词表常量镜像 docs/07 story-blocks 受控集合，同时服务于 prompt 下发与
响应归一化（story_pipeline 复用本模块常量，单一事实源）。
"""

from __future__ import annotations

STORY_PROMPT_VERSION = "story-prompt.v1"

#: docs/07 story-blocks 受控集合（unknown 为 scaffold/归一化占位，不下发给模型
#: 作为可选值——模型应尽力给出具体值，无法判断时才用 unknown）。
DIVISION_AXES = ("主题/话题", "行动/任务", "场景/时空", "情绪/语气", "人物/主体")
PRIMARY_ROLES = (
    "hook", "context", "promise", "problem", "development",
    "proof", "turn", "payoff", "resolution", "custom",
)
INFORMATION_ROLES = (
    "建立背景", "推进新信息", "解释原因", "演示步骤",
    "得出结论", "重复强调", "转移话题",
)
NARRATIVE_DENSITIES = ("高", "中", "低")
AUDIENCE_REACTIONS = (
    "好奇/想看下去", "共鸣/代入", "意外/反转", "娱乐/好笑",
    "获得信息/学到东西", "建立信任/认同", "无强烈反应（信息过场）",
)
VISUAL_INDEPENDENCES = ("静音也能看懂", "需要声音辅助", "没有声音完全看不懂")
SLOT_TYPES = (
    "开场引入", "背景铺垫", "行动展开", "冲突转折",
    "深度剖析", "高潮兑现", "总结升华", "结尾收束", "custom",
)

BLOCK_TITLE_MAX = 12
SLOT_TITLE_MAX = 15


def _format_summary(summary: dict) -> str:
    visual = summary.get("visual", {})
    editing = summary.get("editing", {})
    texts = "、".join(summary.get("texts", [])) or "无"
    lines = [
        f"  - {summary['shotID']} [{summary['startMs']}, {summary['endMs']}ms)",
        f"    画面: {visual.get('contentSummary') or '未知'}",
        f"    主体/动作/场景: {visual.get('subjects') or '未知'} / "
        f"{visual.get('actions') or '未知'} / {visual.get('setting') or '未知'}",
        f"    语音: {summary.get('speech') or '无'}",
        f"    叠字: {texts}",
        f"    入镜转场(确定性): {editing.get('transition') or '片头/未知'}",
        f"    运镜(确定性): {summary.get('cameraMovement') or 'unknown'}",
    ]
    return "\n".join(lines)


def build_story_prompt(
    summaries: list[dict], blocks: list[dict], *, gap_ms: int
) -> str:
    """渲染 story 叙事字段填充 prompt（纯文本，无媒体内容）。

    ``summaries`` 为 :func:`build_shot_summaries` 的输出（已白名单过滤）；
    ``blocks`` 为 scaffold 的确定性候选块（shotIDs/边界只读展示给模型，
    模型不得修改）。
    """
    summaries_by_id = {s["shotID"]: s for s in summaries}
    block_sections: list[str] = []
    for block in blocks:
        shot_lines = "\n".join(
            _format_summary(summaries_by_id[sid])
            for sid in block["shotIDs"]
            if sid in summaries_by_id
        )
        block_sections.append(
            f"## {block['storyBlockID']} [{block['startMs']}, {block['endMs']}ms)\n"
            f"镜头: {', '.join(block['shotIDs'])}\n{shot_lines}"
        )
    blocks_text = "\n\n".join(block_sections)
    return f"""你是视频叙事结构分析师。以下是参考片的确定性故事块划分（由 ASR 停顿
聚块得出，gapMs={gap_ms}）与每镜头的文本摘要。请为每个故事块填充叙事字段，
并把块聚合为故事插槽（slot）。

硬性约束：
- 不得新增、删除、重排或重新分配镜头；不得修改任何块边界与 ID。
- 只输出一个 JSON 对象，不要输出其他文字；不要用 Markdown 围栏之外的解释。
- 无法判断的枚举字段填 "unknown"，不要编造。
- blockTitle 不超过 {BLOCK_TITLE_MAX} 字，slotTitle 不超过 {SLOT_TITLE_MAX} 字。

受控词表：
- divisionAxis: {"、".join(DIVISION_AXES)}
- primaryRole: {"、".join(PRIMARY_ROLES)}
- informationRole（可多选，顿号分隔）: {"、".join(INFORMATION_ROLES)}
- narrativeDensity: {"、".join(NARRATIVE_DENSITIES)}
- audienceReaction: {"、".join(AUDIENCE_REACTIONS)}
- visualIndependence: {"、".join(VISUAL_INDEPENDENCES)}
- slotType（可多选，顿号分隔）: {"、".join(SLOT_TYPES)}

输出 JSON 形状：
{{
  "blocks": [{{
    "storyBlockID": "B0001",
    "blockTitle": "…", "divisionAxis": "…", "divisionRationale": "…",
    "primaryRole": "…", "coreContent": "…", "informationRole": "…",
    "narrativeDensity": "…", "audienceReaction": "…",
    "visualIndependence": "…", "blockRelation": "…", "relationReason": "…"
  }}],
  "slots": [{{
    "slotID": "S001", "slotType": "…", "slotTitle": "…",
    "blockIDs": ["B0001"], "slotRationale": "…"
  }}]
}}

确定性故事块与镜头摘要：

{blocks_text}
"""
