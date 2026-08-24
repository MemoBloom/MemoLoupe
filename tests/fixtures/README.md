# 契约测试夹具

两个夹具都按完整 output-dir 布局组织（`raw/*.json` + 根目录
`style-profile.json`），便于直接喂给
`memoloupe.validate.cross_artifact.validate_output_dir`：

- `output_full/`：3 镜头（SH0001 [0,3203)、SH0002 [3203,6400)、
  SH0003 [6400,9800)）的完整合法产物，内容以
  `docs/07_SOURCE_DATA_CONTRACT.md` 各文件"完整示例"为底扩展，
  所有跨文件引用（revisionID、shotID、slotID、blockID、
  evidenceRefs、帧文件）真实闭合。`evidence/frames/` 下的
  JPEG 为占位文件，仅供文件存在性校验。
- `minimal/`：单镜头（SH0001 [0,1000)）、每个文件只含 schema
  required 字段的最小合法产物，用于验证 schema 没有把可选字段
  误标为必填。frame-evidence 含一条 MAIN 帧（及其占位 JPEG），
  使 strict 覆盖检查也能通过。

JSON 序列化与 `memoloupe.core.atomic_io` 一致：UTF-8、
`ensure_ascii=False`、2 空格缩进、`sort_keys`、末尾换行。
