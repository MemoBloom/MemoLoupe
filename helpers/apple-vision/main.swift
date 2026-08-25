// MemoLoupe Apple Vision 运镜 helper（docs/01 §7.4、docs/03 §2.11）。
//
// 协议：stdin 读入单个 JSON 请求，stdout 只输出 JSON 结果，日志写 stderr。
// 请求：{"source": path, "shots": [{"shotID","startMs","endMs"}],
//        "sampleFps": 2.0, "maximumFramesPerShot": 12, "maximumImageDimension": 960}
// 结果：{"shots": [{"shotID", "frames": [{"frameIndex","timeMs","shiftX","shiftY",
//        "scale","rotationDegrees","ok"]}]}]}
//
// 实现：AVAssetImageGenerator 按 sampleFps 在每镜头 [startMs, endMs) 内均匀抽帧
//（中心采样，绝不在 endMs 精确抽帧；每镜头至多 maximumFramesPerShot 帧，缩放到
// 最大边 maximumImageDimension，应用 preferredTransform）。对相邻帧跑有状态的
// VNTrackHomographicImageRegistrationRequest（macOS 14+，经 VNSequenceRequestHandler
// 按帧序喂入，每对帧产出一个 warp）；单帧失败后重建请求并以当前帧重新播种，
// 该对记 ok:false，其余对不受影响。
//
// 方向约定：追踪请求返回的 warp 把当前帧配准回上一帧（cur → prev）。
// 输出时取其逆矩阵分解，使 shift/shiftY 表示「上一帧 → 当前帧」的画面内容位移：
// shiftX > 0 表示内容右移（对应摄影机左移）。注意这是图像位移，不是摄影机
// 运动（D-005）。经验上 Vision 对大幅逐对位移的幅值有低估，方向与单调性可靠。
//
// 编译目标：macOS 14+。编译：swiftc -O main.swift -o apple-vision

import AVFoundation
import Foundation
import Vision
import simd

// MARK: - 协议模型

struct ShotRequest: Decodable {
    let shotID: String
    let startMs: Int
    let endMs: Int
}

struct HelperRequest: Decodable {
    let source: String
    let shots: [ShotRequest]
    let sampleFps: Double
    let maximumFramesPerShot: Int
    let maximumImageDimension: Int
}

struct FrameResult: Encodable {
    let frameIndex: Int
    let timeMs: Int
    let shiftX: Double
    let shiftY: Double
    let scale: Double
    let rotationDegrees: Double
    let ok: Bool
}

struct ShotResult: Encodable {
    let shotID: String
    let frames: [FrameResult]
}

struct HelperResponse: Encodable {
    let shots: [ShotResult]
}

/// 采样/注册失败时的占位帧（identity，ok:false）。
func failedFrame(_ frameIndex: Int, _ timeMs: Int) -> FrameResult {
    FrameResult(
        frameIndex: frameIndex, timeMs: timeMs,
        shiftX: 0, shiftY: 0, scale: 1, rotationDegrees: 0, ok: false
    )
}

func log(_ message: String) {
    FileHandle.standardError.write(Data("apple-vision: \(message)\n".utf8))
}

// MARK: - 采样时间

/// 在 [startMs, endMs) 内按 sampleFps 均匀中心采样，至多 maxFrames 帧。
func sampleTimesMs(startMs: Int, endMs: Int, sampleFps: Double, maxFrames: Int) -> [Int] {
    let durationMs = endMs - startMs
    guard durationMs > 0, sampleFps > 0, maxFrames > 0 else { return [] }
    var count = Int((Double(durationMs) / 1000.0 * sampleFps).rounded(.down))
    count = max(1, min(maxFrames, count))
    return (0 ..< count).map { k in
        startMs + Int((Double(k) + 0.5) * Double(durationMs) / Double(count))
    }
}

// MARK: - warp 分解

/// 把 homography 近似分解为 similarity（translation / scale / rotation）。
/// 传入的是 cur → prev 的配准 warp；先取逆得到 prev → cur 的内容位移再分解。
/// simd_float3x3 为列主序：m[c][r] 表示第 r 行第 c 列。
/// 返回 nil 表示矩阵退化（不可信）。
func decompose(_ warp: simd_float3x3) -> (shiftX: Double, shiftY: Double, scale: Double, rotationDegrees: Double)? {
    let determinant = warp.determinant
    guard determinant.isFinite, abs(determinant) > 1e-9 else { return nil }
    let inverted = warp.inverse
    let w = inverted[2][2]
    guard w.isFinite, abs(w) > 1e-9 else { return nil }
    let m = simd_float3x3(columns: (inverted[0] / w, inverted[1] / w, inverted[2] / w))
    let a = Double(m[0][0])  // M00
    let b = Double(m[0][1])  // M10
    let tx = Double(m[2][0]) // M02
    let ty = Double(m[2][1]) // M12
    let scale = (a * a + b * b).squareRoot()
    let rotationDegrees = atan2(b, a) * 180.0 / .pi
    guard tx.isFinite, ty.isFinite, scale.isFinite, rotationDegrees.isFinite else { return nil }
    return (tx, ty, scale, rotationDegrees)
}

// MARK: - registration

/// 有状态追踪器：按帧序喂入，第 i（>=1）次 perform 产出 (i-1, i) 对的 warp。
/// 失败后由调用方重建并以当前帧重新播种。
final class PairTracker {
    private var handler = VNSequenceRequestHandler()
    private var request = VNTrackHomographicImageRegistrationRequest()

    /// 以 frame 播种（不产生观测）。
    func seed(_ frame: CGImage) {
        handler = VNSequenceRequestHandler()
        request = VNTrackHomographicImageRegistrationRequest()
        try? handler.perform([request], on: frame)
    }

    /// 追踪到当前帧，返回该对 warp；失败返回 nil（调用方需重新 seed）。
    func track(_ frame: CGImage) -> simd_float3x3? {
        do {
            try handler.perform([request], on: frame)
            return request.results?.first?.warpTransform
        } catch {
            return nil
        }
    }
}

// MARK: - 镜头分析

func analyzeShot(
    _ shot: ShotRequest,
    generator: AVAssetImageGenerator,
    request: HelperRequest
) -> ShotResult {
    let times = sampleTimesMs(
        startMs: shot.startMs, endMs: shot.endMs,
        sampleFps: request.sampleFps, maxFrames: request.maximumFramesPerShot
    )

    // 抽帧；失败样本记 nil，对应帧 ok:false。
    var images: [CGImage?] = []
    var frames: [FrameResult] = []
    frames.reserveCapacity(times.count)
    for (index, timeMs) in times.enumerated() {
        let cmTime = CMTime(seconds: Double(timeMs) / 1000.0, preferredTimescale: 600)
        do {
            images.append(try generator.copyCGImage(at: cmTime, actualTime: nil))
        } catch {
            log("shot \(shot.shotID) frame \(index) 抽帧失败: \(error.localizedDescription)")
            images.append(nil)
        }
    }

    // 首帧：无前一帧可配对，写 identity；抽帧失败则 ok:false。
    if !times.isEmpty {
        frames.append(
            FrameResult(
                frameIndex: 0, timeMs: times[0],
                shiftX: 0, shiftY: 0, scale: 1, rotationDegrees: 0,
                ok: images[0] != nil
            )
        )
    }

    // 相邻帧有状态追踪；单帧失败后重建 tracker 并以当前帧重新播种，
    // 该对记 ok:false，不影响后续对。
    let tracker = PairTracker()
    if let first = images.first ?? nil {
        tracker.seed(first)
    }
    for index in 1 ..< times.count {
        let timeMs = times[index]
        guard let curr = images[index] else {
            frames.append(failedFrame(index, timeMs))
            continue
        }
        var pair: (shiftX: Double, shiftY: Double, scale: Double, rotationDegrees: Double)?
        if images[index - 1] != nil, let warp = tracker.track(curr) {
            pair = decompose(warp)
        }
        if let pair {
            frames.append(
                FrameResult(
                    frameIndex: index, timeMs: timeMs,
                    shiftX: pair.shiftX, shiftY: pair.shiftY,
                    scale: pair.scale, rotationDegrees: pair.rotationDegrees, ok: true
                )
            )
        } else {
            log("shot \(shot.shotID) frame \(index) 配准失败，重新播种")
            frames.append(failedFrame(index, timeMs))
            tracker.seed(curr)
        }
    }
    return ShotResult(shotID: shot.shotID, frames: frames)
}

// MARK: - main

let input = FileHandle.standardInput.readDataToEndOfFile()
guard let request = try? JSONDecoder().decode(HelperRequest.self, from: input) else {
    log("请求 JSON 非法")
    exit(2)
}

let asset = AVURLAsset(url: URL(fileURLWithPath: request.source))
let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.requestedTimeToleranceBefore = .zero
generator.requestedTimeToleranceAfter = .zero
let maxDimension = CGFloat(request.maximumImageDimension)
generator.maximumSize = CGSize(width: maxDimension, height: maxDimension)

var results: [ShotResult] = []
for shot in request.shots {
    autoreleasepool {
        results.append(analyzeShot(shot, generator: generator, request: request))
    }
}

let encoder = JSONEncoder()
do {
    let data = try encoder.encode(HelperResponse(shots: results))
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
    log("结果编码失败: \(error.localizedDescription)")
    exit(3)
}
