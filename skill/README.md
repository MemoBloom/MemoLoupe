# memoloupe-skill

[MemoLoupe](https://github.com/MemoBloom/MemoLoupe) 拉片分析 CLI 的
Agent Skill：让 AI 编码代理（pi / Claude Code 等）正确驱动
`memoloupe` 命令完成参考视频的结构化分析。

Skill 内容包含：CLI 全命令速查、产物契约表、强制数据规则
（整数毫秒 / `[startMs, endMs)` / 五态语义 / evidenceRefs / strict 校验）、
典型工作流与故障排查。

## 安装

```bash
# 方式一：npx 直装（默认 ~/.agents/skills/memoloupe）
npx memoloupe-skill

# 方式二：指定目标目录（如 Claude Code）
npx memoloupe-skill --target ~/.claude/skills/memoloupe
```

## 前置：安装 memoloupe CLI

```bash
brew install memobloom/memoloupe/memoloupe
memoloupe connect add mimo    # 配置模型服务（mimo / qwen）
memoloupe connect switch mimo
```

## 发布（维护者）

```bash
cd skill
npm publish
```

版本对齐：与 MemoLoupe `pyproject.toml` 的 version 同步 bump。
