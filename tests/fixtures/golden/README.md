# 黄金视频校准（roadmap 05-03）

## 用途

用真实视频 + 人工标注收敛 `docs/06` 中 A-001~A-007 的待校准参数。
**没有黄金视频前，不得凭感觉改默认阈值。**

## 目录约定

```text
tests/fixtures/golden/
├── README.md
└── videos/            # 真实短视频（不入库，本地放置；每个视频一个标注 JSON）
    └── golden-001.mp4
└── golden-001.json    # 标注文件（可入库，须脱敏）
```

标注 JSON 与视频同名（`golden-001.json` ↔ `videos/golden-001.mp4`）。
测试框架（`tests/e2e/test_golden_calibration.py`）扫描 `tests/fixtures/golden/*.json`，
无标注文件时全部 skip；有标注但无对应视频时也 skip。

## 标注格式（schemaVersion=1）

```json
{
  "schemaVersion": 1,
  "video": "golden-001.mp4",
  "notes": "红/白/蓝三色硬切 3s，440Hz 正弦音轨（可选人工说明）",
  "annotations": {
    "shotBoundariesMs": [0, 1000, 2000, 3000],
    "audioCutPointsMs": [1000, 2000],
    "bgmIntervalsMs": [[0, 3000]],
    "qualityFlags": { "SH0001": ["画面模糊"] },
    "cameraMovement": { "SH0001": "static", "SH0002": "pan_right" },
    "storyBlocks": [
      { "startMs": 0, "endMs": 3000, "title": "全片单块" }
    ]
  },
  "tolerances": {
    "boundaryMs": 100,
    "minRecall": 0.8,
    "minPrecision": 0.6,
    "enumAccuracy": 0.8
  }
}
```

### 字段语义

| 字段 | 类型 | 用途（校准项） |
|---|---|---|
| shotBoundariesMs | number[] | A-001 视觉切镜：含首尾的边界毫秒 |
| audioCutPointsMs | number[] | A-002 音频切点 |
| bgmIntervalsMs | [number, number][] | A-003 BGM 区间 |
| qualityFlags | dict | A-004 质量 |
| cameraMovement | dict | A-005 Apple Vision 运镜（枚举匹配） |
| storyBlocks | {startMs,endMs,title}[] | A-006 story 聚块 |
| tolerances | dict | 误差容忍：边界毫秒、最低召回/精确率、枚举准确率 |

### 校准纪律（docs/08 05-03）

每项校准必须：

1. 保存脱敏黄金标注或期望范围（本目录）；
2. 先新增失败测试（`test_golden_calibration.py` 中按标注断言）；
3. 更新配置默认值和算法版本；
4. 更新 fingerprint，确保旧缓存失效；
5. 在 `docs/06_DECISIONS_AND_ASSUMPTIONS.md` 记录实证、局限和适用范围。

未达标项必须留有误差解释（missed/falsePositives 明细），不假装 complete。
