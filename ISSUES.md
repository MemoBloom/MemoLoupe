# Known Issues

本文件记录 MemoLoupe 当前已确认、尚未解决的问题。每条包含现象、根因、影响与候选方向。
问题关闭后从本文档移除（历史决策见 `docs/06_DECISIONS_AND_ASSUMPTIONS.md`）。

---

## ISSUE-001: chat ASR（mimo-v2.5-asr / qwen3-asr-flash）无词级/句级时间戳，story 聚块退化为 30s 窗口粒度

**状态**: Open（2026-09-03 记录；同日确认 qwen-chat 通路同样受限）
**严重级别**: Medium — 功能可用，但 story 分块精度受限

### 现象

使用 `asr.provider=mimo-chat`（mimo-v2.5-asr）或 `asr.provider=qwen-chat`
（qwen3-asr-flash）时，ASR 返回纯文本、**不携带任何时间戳**。客户端只能按
固定窗口切片（默认 `asr.windowSec = 30s`，见 `src/memoloupe/services/asr.py`
的 `WindowedChatASR`），每个窗口产生一个 segment，其起止时间就是窗口边界
（`raw_extras.provider.windowed = True`）。

### 影响

- 下游 story 聚块依赖 segment 间的停顿时长（`story.gapMs`）判断叙事边界。
  在 mimo-chat 通路下，**窗口内部没有任何停顿信息**，30s 以内的内容永远聚成一块。
- 实测：disney.MP4（2m18s 演唱类内容，歌词密集）在 mimo ASR 通路下 story
  只能产出 1 个故事块；而 whisper 通路（有真实句级时间戳）能按演唱停顿分出多块。
- 对歌词/旁白密集的视频，story 分块基本失效；对对白稀疏、句子天然跨窗口的内容影响较小。

### 根因

mimo-v2.5-asr 与 qwen3-asr-flash 的 chat completions 接口（`input_audio`
data URL）响应只有纯文本，无 `segments` / `words` 时间戳字段（2026-09-03
两家均实测确认）。这是服务端能力限制，不是客户端解析遗漏。

补充：Qwen 侧存在带时间戳的替代路径——`qwen3-asr-flash-filetrans`
（DashScope 异步任务接口）支持句级/词级时间戳（`enable_words`），但要求
音频为**公网可访问 URL**，无法直接用于本地文件，暂不可行。

### 候选方向

1. **本地 VAD 预切分（推荐）**：接入本地人声检测（如 FireRedVAD），先按人声段
   切音频，再把每个语音段单独送 mimo-v2.5-asr。segment 时间 = VAD 段边界，
   恢复句级粒度和段间停顿信息，story 聚块可与 whisper 通路对齐。
   代价：新增本地模型依赖。
2. **等待服务端支持**：若 MiMo ASR 后续返回时间戳，客户端直接消费，
   移除 windowed 降级逻辑（检测 `windowed` 标记即可平滑切换）。
3. **窗口内强制细分**：把 windowSec 调小（如 10s）缓解粒度问题，但会切断
   跨窗口的句子、增加请求数，治标不治本，不建议作为主方案。

### 相关代码 / 文档

- `src/memoloupe/services/asr.py` — `WindowedChatASR`（窗口切片与 `windowed`
  标记，`MiMoChatASR` / `QwenChatASR` 共用）
- 配置：`asr.provider = "mimo-chat" | "qwen-chat"`、`asr.windowSec`
- 决策记录：`docs/06_DECISIONS_AND_ASSUMPTIONS.md` D-057、D-058
- 验证产物：`output/disney-mimo-e2e-20260903/`（mimo 通路 e2e，story = 1 块）
