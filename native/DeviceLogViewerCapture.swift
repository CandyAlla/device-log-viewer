import CoreGraphics
import CoreMedia
import CoreVideo
import Foundation
import AppKit
import ScreenCaptureKit

private let quickTimeBundleID = "com.apple.QuickTimePlayerX"
private let pixelFormatNV12 = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange

private enum CaptureError: LocalizedError {
    case permissionRequired
    case invalidArguments(String)
    case windowNotFound(UInt32)
    case unsupportedPixelBuffer

    var errorDescription: String? {
        switch self {
        case .permissionRequired:
            return "SCREEN_CAPTURE_PERMISSION_REQUIRED"
        case let .invalidArguments(message):
            return message
        case let .windowNotFound(windowID):
            return "QuickTime 预览窗口不存在：\(windowID)"
        case .unsupportedPixelBuffer:
            return "ScreenCaptureKit 返回了不支持的画面格式。"
        }
    }
}

private struct WindowDescription: Codable {
    let id: UInt32
    let title: String
    let frameWidth: Double
    let frameHeight: Double
    let pointPixelScale: Double
    let nativeWidth: Int
    let nativeHeight: Int
    let onScreen: Bool
    let active: Bool
}

private struct WindowList: Codable {
    let authorized: Bool
    let windows: [WindowDescription]
}

private func writeJSON<T: Encodable>(_ value: T) throws {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    let data = try encoder.encode(value)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0a]))
}

private func requestScreenCapturePermission() -> Bool {
    if CGPreflightScreenCaptureAccess() {
        return true
    }
    return CGRequestScreenCaptureAccess()
}

private func quickTimeWindows() async throws -> [(SCWindow, SCContentFilter)] {
    guard requestScreenCapturePermission() else {
        throw CaptureError.permissionRequired
    }
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    return content.windows.compactMap { window in
        guard window.owningApplication?.bundleIdentifier == quickTimeBundleID,
              window.frame.width >= 160,
              window.frame.height >= 240
        else {
            return nil
        }
        return (window, SCContentFilter(desktopIndependentWindow: window))
    }
}

private func listWindows() async throws {
    let windows = try await quickTimeWindows().map { window, filter in
        let scale = max(1.0, Double(filter.pointPixelScale))
        let nativeWidth = max(2, Int((window.frame.width * scale).rounded()) & ~1)
        let nativeHeight = max(2, Int((window.frame.height * scale).rounded()) & ~1)
        return WindowDescription(
            id: window.windowID,
            title: window.title ?? "",
            frameWidth: window.frame.width,
            frameHeight: window.frame.height,
            pointPixelScale: scale,
            nativeWidth: nativeWidth,
            nativeHeight: nativeHeight,
            onScreen: window.isOnScreen,
            active: window.isActive
        )
    }
    try writeJSON(WindowList(authorized: true, windows: windows))
}

private final class RawFrameOutput: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private let expectedWidth: Int
    private let expectedHeight: Int
    private let output = FileHandle.standardOutput
    private let lock = NSLock()
    private var failed = false

    init(width: Int, height: Int) {
        expectedWidth = width
        expectedHeight = height
    }

    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        FileHandle.standardError.write(Data("SCREEN_CAPTURE_STOPPED: \(error)\n".utf8))
        exit(5)
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of outputType: SCStreamOutputType
    ) {
        guard outputType == .screen,
              sampleBuffer.isValid,
              CMSampleBufferDataIsReady(sampleBuffer),
              let pixelBuffer = sampleBuffer.imageBuffer
        else {
            return
        }
        if let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false)
            as? [[SCStreamFrameInfo: Any]],
           let statusValue = attachments.first?[.status] as? Int,
           statusValue != SCFrameStatus.complete.rawValue
        {
            return
        }

        lock.lock()
        defer { lock.unlock() }
        if failed { return }

        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }
        guard CVPixelBufferGetPixelFormatType(pixelBuffer) == pixelFormatNV12,
              CVPixelBufferIsPlanar(pixelBuffer),
              CVPixelBufferGetPlaneCount(pixelBuffer) == 2,
              CVPixelBufferGetWidth(pixelBuffer) == expectedWidth,
              CVPixelBufferGetHeight(pixelBuffer) == expectedHeight
        else {
            failed = true
            FileHandle.standardError.write(Data("UNSUPPORTED_PIXEL_BUFFER\n".utf8))
            exit(6)
        }

        writePlane(pixelBuffer, plane: 0, rowBytes: expectedWidth, rows: expectedHeight)
        writePlane(pixelBuffer, plane: 1, rowBytes: expectedWidth, rows: expectedHeight / 2)
    }

    private func writePlane(_ pixelBuffer: CVPixelBuffer, plane: Int, rowBytes: Int, rows: Int) {
        guard let base = CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, plane) else {
            failed = true
            return
        }
        let sourceStride = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, plane)
        if sourceStride == rowBytes {
            output.write(Data(bytesNoCopy: base, count: rowBytes * rows, deallocator: .none))
            return
        }
        for row in 0..<rows {
            output.write(Data(bytes: base.advanced(by: row * sourceStride), count: rowBytes))
        }
    }
}

private func argumentValue(_ name: String, in arguments: [String]) -> String? {
    guard let index = arguments.firstIndex(of: name), arguments.indices.contains(index + 1) else {
        return nil
    }
    return arguments[index + 1]
}

private func requiredInteger(_ name: String, in arguments: [String], range: ClosedRange<Int>) throws -> Int {
    guard let raw = argumentValue(name, in: arguments), let value = Int(raw), range.contains(value) else {
        throw CaptureError.invalidArguments("参数 \(name) 无效。")
    }
    return value
}

private func streamWindow(arguments: [String]) async throws {
    let windowIDValue = try requiredInteger("--window-id", in: arguments, range: 1...Int(UInt32.max))
    let width = try requiredInteger("--width", in: arguments, range: 2...8192)
    let height = try requiredInteger("--height", in: arguments, range: 2...8192)
    let fps = try requiredInteger("--fps", in: arguments, range: 1...120)
    guard width.isMultiple(of: 2), height.isMultiple(of: 2) else {
        throw CaptureError.invalidArguments("画面宽高必须是偶数。")
    }
    let windowID = UInt32(windowIDValue)
    let matches = try await quickTimeWindows()
    guard let (window, filter) = matches.first(where: { $0.0.windowID == windowID }) else {
        throw CaptureError.windowNotFound(windowID)
    }

    let configuration = SCStreamConfiguration()
    configuration.width = width
    configuration.height = height
    configuration.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(fps))
    configuration.pixelFormat = pixelFormatNV12
    configuration.scalesToFit = true
    configuration.showsCursor = false
    configuration.capturesAudio = false
    configuration.queueDepth = 6
    configuration.ignoreShadowsSingleWindow = true
    configuration.captureResolution = .best

    let output = RawFrameOutput(width: width, height: height)
    let stream = SCStream(filter: filter, configuration: configuration, delegate: output)
    let queue = DispatchQueue(label: "com.candyalla.devicelogviewer.screen-capture", qos: .userInteractive)
    try stream.addStreamOutput(output, type: .screen, sampleHandlerQueue: queue)
    try await stream.startCapture()

    FileHandle.standardError.write(
        Data("STREAM_READY window=\(window.windowID) size=\(width)x\(height) fps=\(fps)\n".utf8)
    )
    while true {
        try await Task.sleep(nanoseconds: 60_000_000_000)
    }
}

@main
private struct DeviceLogViewerCapture {
    static func main() async {
        do {
            let application = NSApplication.shared
            application.setActivationPolicy(.accessory)
            let arguments = Array(CommandLine.arguments.dropFirst())
            guard let command = arguments.first else {
                throw CaptureError.invalidArguments("用法：DeviceLogViewerCapture list | stream [参数]")
            }
            switch command {
            case "list":
                try await listWindows()
            case "stream":
                try await streamWindow(arguments: arguments)
            default:
                throw CaptureError.invalidArguments("未知命令：\(command)")
            }
        } catch {
            FileHandle.standardError.write(Data("\(error.localizedDescription)\n".utf8))
            exit(error is CaptureError ? 3 : 4)
        }
    }
}
