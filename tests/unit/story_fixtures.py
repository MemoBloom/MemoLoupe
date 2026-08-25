"""story 相关测试的共享夹具构造器（tests/unit 内复用）。

pytest 在无 ``__init__.py`` 时把测试文件所在目录加入 sys.path，
因此本模块以顶层模块 ``story_fixtures`` 被测试文件导入。
"""

from __future__ import annotations

import json
from pathlib import Path


def media_doc(duration_ms: int = 6000) -> dict:
    return {
        "source": {
            "assetID": "story-test",
            "sourcePath": "/tmp/story-test.mp4",
            "revisionID": "a1b2c3d4e5f6",
            "durationMs": duration_ms,
            "durationSec": duration_ms / 1000,
            "frameRate": 29.97,
            "resolution": {"width": 1920, "height": 1080},
            "aspectRatio": 1.777778,
            "audioTracks": [
                {
                    "trackID": "1",
                    "channels": 2,
                    "sampleRate": 48000,
                    "language": "unknown",
                    "hasSpeech": "unknown",
                    "hasMusic": "unknown",
                    "hasEffects": "unknown",
                }
            ],
            "analyzedRange": {"startMs": 0, "endMs": duration_ms},
            "analysisCoverage": [{"capability": "mediaMetadata", "status": "complete"}],
        }
    }


def shot(index: int, start_ms: int, end_ms: int) -> dict:
    return {
        "shotID": f"SH{index:04d}",
        "sequenceIndex": index,
        "detectedStartMs": start_ms,
        "detectedEndMs": end_ms,
        "finalStartMs": start_ms,
        "finalEndMs": end_ms,
        "durationMs": end_ms - start_ms,
        "boundaryIn": {"type": "hardCutCandidate", "confidence": "high", "metric": None},
        "boundaryOut": {"type": "hardCutCandidate", "confidence": "high", "metric": None},
        "needsReview": False,
    }


def shots_doc(ranges: list[tuple[int, int]]) -> dict:
    duration_ms = max(end for _, end in ranges)
    return {
        "analysis": {
            "method": "memoClipHardCutCandidateCuts",
            "fps": 29.97,
            "sourceFps": 29.97,
            "durationMs": duration_ms,
            "selectedBoundaryCount": len(ranges) - 1,
        },
        "boundaries": [],
        "shots": [
            shot(i + 1, start, end) for i, (start, end) in enumerate(ranges)
        ],
    }


def asr_doc(segments: list[dict], status: str = "complete") -> dict:
    return {
        "service": "asr",
        "status": status,
        "transcript": {"segments": segments},
    }


def segment(start_ms: int, end_ms: int, text: str = "台词") -> dict:
    return {
        "startMs": start_ms,
        "endMs": end_ms,
        "text": text,
        "speaker": "SPEAKER_0",
        "confidence": 0.9,
    }


def write_out_dir(
    root: Path,
    *,
    shot_ranges: list[tuple[int, int]],
    asr: dict | None,
    unified: dict | None = None,
) -> Path:
    raw = root / "raw"
    raw.mkdir(parents=True)
    (raw / "media.json").write_text(
        json.dumps(media_doc(), ensure_ascii=False), encoding="utf-8"
    )
    (raw / "shots.json").write_text(
        json.dumps(shots_doc(shot_ranges), ensure_ascii=False), encoding="utf-8"
    )
    if asr is not None:
        (raw / "asr.json").write_text(
            json.dumps(asr, ensure_ascii=False), encoding="utf-8"
        )
    if unified is not None:
        (raw / "unified-media.json").write_text(
            json.dumps(unified, ensure_ascii=False), encoding="utf-8"
        )
    return root


def read_blocks(root: Path) -> dict:
    return json.loads((root / "raw" / "story-blocks.json").read_text(encoding="utf-8"))
