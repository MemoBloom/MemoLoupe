"""analysis.profile_prompts — style-profile 模型蒸馏 prompt（docs/03 §4.2、roadmap 04-02）。

铁律：蒸馏请求只包含结构化 story/profile aggregate 与块叙事摘要的文本，
**不发送视频、帧、Data URI、clip/代理路径或源文件路径**（编排层测试断言）。

prompt 强调：

- 抽象参考片的功能（affordance），不要求具体地点/对象一一复刻；
- L1 是叙事结构、L2 是承载模式、L3 是原片证据；
- 模型只补充主观字段，不得修改确定性统计/ID/时长/分布；
- 不生成 Story Spine 或剪辑方案。
"""

from __future__ import annotations

from typing import Any

PROFILE_PROMPT_VERSION = "profile-prompt.v2"

NARRATIVE_FUNCTIONS = (
    "setup", "progression", "complication", "resolution", "reflection",
)

#: 模型无权覆盖的确定性字段（白名单保护，docs/08 04-02）。
DETERMINISTIC_SLOT_FIELDS = frozenset(
    {"types", "durationShare", "rangeSeconds", "minBlocks", "shotIds", "shotCount", "avgShotSeconds"}
)


def _slot_block_summary(slot: dict, block_ids: list[str], blocks_by_id: dict[str, Any]) -> str:
    lines = [
        f"- {slot['slotId']} [{slot['L1']['rangeSeconds'][0]}, "
        f"{slot['L1']['rangeSeconds'][1]}]s 占 {slot['L1']['durationShare']}"
        f" types={slot['L1']['types']} minBlocks={slot['L1']['minBlocks']}"
        f" 镜头={slot['L3']['shotIds']}（均长 {slot['L3']['avgShotSeconds']}s）"
    ]
    for bid in block_ids:
        block = blocks_by_id.get(bid)
        if block is None:
            continue
        title = block.get("blockTitle") or block["storyBlockID"]
        lines.append(
            f"    {bid}「{title}」primaryRole={block.get('primaryRole')} "
            f"density={block.get('narrativeDensity')} "
            f"reaction={block.get('audienceReaction')} 关系={block.get('blockRelation')} "
            f"shots={sorted(block.get('shotIDs') or [])}"
            f" 内容={block.get('coreContent')}"
        )
    return "\n".join(lines)


def build_profile_distill_prompt(
    aggregate: dict, story: dict
) -> str:
    """渲染 style-profile 蒸馏 prompt（纯文本，无媒体内容）。

    ``aggregate`` 为 :func:`~memoloupe.analysis.profile_aggregate.build_profile_aggregate`
    的确定性输出；``story`` 为 story-blocks.json 全文（仅用于取 slot→block
    归属与块叙事字段的白名单文本，不接触 clip/路径）。
    """
    blocks_by_id = {
        str(b["storyBlockID"]): b
        for b in (story.get("blocks") if isinstance(story, dict) else []) or []
        if isinstance(b, dict) and isinstance(b.get("storyBlockID"), str)
    }
    slots_by_id: dict[str, list[str]] = {}
    for slot in (story.get("slots") if isinstance(story, dict) else []) or []:
        if not isinstance(slot, dict) or not isinstance(slot.get("slotID"), str):
            continue
        slots_by_id[slot["slotID"]] = [
            str(bid) for bid in slot.get("blockIDs", []) if isinstance(bid, str)
        ]
    slot_sections: list[str] = []
    for slot in aggregate["structure"]["slots"]:
        slot_sections.append(
            _slot_block_summary(slot, slots_by_id.get(slot["slotId"], []), blocks_by_id)
        )
    slots_text = "\n".join(slot_sections)
    pacing = aggregate["pacing"]
    style = aggregate["style"]
    return f"""你是参考片风格档案分析师。以下是确定性聚合出的 style-profile 骨架
（L1/L2 主观字段为空、hook/payoff 为 null、蒸馏字段为空）与每插槽的故事块摘要。
请为模型专属字段补充内容，输出一个 JSON 对象。

铁律：
- 只补充主观字段；不得复述或修改确定性统计、计数、时长、分布与 ID 清单
  （返回这些受保护确定性字段将被整体拒绝）。
- 例外：hook/payoff 的 L3.shotIds 是“原片证据引用”，允许且必须从下方各
  block 行列出的真实镜头（shots=）中挑选；空数组永远不合法；若对某个
  hook/payoff 没有把握，把整个 hook/payoff 置 null，绝不返回空 shotIds。
- 抽象参考片的功能（affordance），不要求具体地点、人物或对象一一复刻；
  L1 是叙事结构，L2 是承载模式，L3 是原片证据。
- 不生成 Story Spine、剪辑方案或用户素材匹配建议。
- 无法判断的字段填 null，不要编造。
- 只输出一个 JSON 对象，不要输出其他文字；不要用 Markdown 围栏之外的解释。

每个 slot 补充：
- L1.functionalTitle（抽象功能命名，不含具体内容）
- L1.narrativeFunction（受控词表：{"、".join(NARRATIVE_FUNCTIONS)}）
- L1.intendedReaction（目标观众反应）
- L2.carriage / L2.pattern / L2.referenceContent（参考片具体内容摘要）

可选：hook/payoff（layeredRole：L1 定位 atSeconds/slotId/blockId，L2
form/referenceContent，L3 shotIds 从该 blockId 行的 shots= 中挑选真实存在
的 ID 且必须非空；判断不了就整个置 null）；structureRequirements（用户素材硬前提，
每项 slotId/requirementType/description/minEvidence）；adoptionHints
（strengths/cautions/suggestedDefault）；discussionItems（复刻前澄清问题，
每项 id/layer/category/question/options 含 id 与 label 的选项/impactLevel/
defaultIfUnanswered）。

输出 JSON 形状：
{{
  "slots": [{{"slotId": "S001",
             "L1": {{"functionalTitle": "…", "narrativeFunction": "setup",
                     "intendedReaction": "…"}},
             "L2": {{"carriage": "…", "pattern": "…", "referenceContent": "…"}}}}],
  "hook": {{"L1": {{"atSeconds": 0.0, "slotId": "S001", "blockId": "B0001"}},
            "L2": {{"form": "…", "referenceContent": "…"}},
            "L3": {{"shotIds": ["SH0001"]}}}} | null,
  "payoff": {{…}} | null,
  "structureRequirements": [{{"slotId": "S001", "requirementType": "evidence",
      "description": "…", "minEvidence": "…"}}],
  "adoptionHints": {{"strengths": ["…"], "cautions": ["…"], "suggestedDefault": "L1+L2"}} | null,
  "discussionItems": [{{"id": "q-1", "layer": "L2", "category": "applicability",
      "question": "…", "options": [{{"id": "a", "label": "…"}}],
      "impactLevel": "preference", "defaultIfUnanswered": "…"}}]
}}

确定性聚合骨架：

结构（slots 与期望链）：
{slots_text}

节奏：shotDuration={pacing.get('shotDuration')} densityCurve={pacing.get('densityCurve')}
音画对齐={pacing.get('audioBoundaryBySlot')} musicAlignment={pacing.get('musicAlignment')}
风格分布：transitions={style.get('transitions')} framing={style.get('framing')}
cameraMovement={style.get('cameraMovement')} textOverlay={style.get('textOverlay')}
bgm={style.get('bgm')} voiceMix={style.get('voiceMix')}
hostedCoverage={style.get('hostedCoverage')}
"""


def distill_prompt_has_no_media(prompt: str) -> bool:
    """测试辅助：断言 prompt 不携带媒体形态（视频/帧/Data URI/路径）。"""
    for forbidden in ("data:", ".mp4", "clips/", "evidence/frames/", "base64", "/Users/", "/tmp/"):
        if forbidden in prompt:
            return False
    return True
