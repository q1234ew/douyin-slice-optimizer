import AppKit
import Foundation
import Vision

struct OCRItem: Codable {
    let path: String
    let lines: [String]
    let error: String?
}

struct OCRPayload: Codable {
    let status: String
    let engine: String
    let items: [OCRItem]
}

func recognize(path: String) -> OCRItem {
    guard let image = NSImage(contentsOfFile: path),
          let data = image.tiffRepresentation,
          let bitmap = NSBitmapImageRep(data: data),
          let cgImage = bitmap.cgImage else {
        return OCRItem(path: path, lines: [], error: "image_decode_failed")
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([request])
        let lines = (request.results ?? []).compactMap { observation in
            observation.topCandidates(1).first?.string.trimmingCharacters(in: .whitespacesAndNewlines)
        }.filter { !$0.isEmpty }
        return OCRItem(path: path, lines: lines, error: nil)
    } catch {
        return OCRItem(path: path, lines: [], error: String(describing: error))
    }
}

let items = CommandLine.arguments.dropFirst().map { recognize(path: String($0)) }
let payload = OCRPayload(status: items.isEmpty ? "empty" : "ready", engine: "macos_vision", items: items)
let encoder = JSONEncoder()
encoder.outputFormatting = [.withoutEscapingSlashes]
let data = try encoder.encode(payload)
print(String(data: data, encoding: .utf8) ?? "{}")
