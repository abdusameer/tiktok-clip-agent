import Vision
import AppKit
import Foundation
// Usage: ocr <image>...   Prints [{path, lines:[text], boxes:[{text, h, w, x, y}]}] (box sizes are fractions of the image).
var out: [[String: Any]] = []
for path in CommandLine.arguments.dropFirst() {
    guard let img = NSImage(contentsOfFile: path), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { out.append(["path": path, "lines": [], "boxes": []]); continue }
    let req = VNRecognizeTextRequest(); req.recognitionLevel = .accurate; req.minimumTextHeight = 0.004
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
    var lines: [String] = []; var boxes: [[String: Any]] = []
    for o in (req.results ?? []) {
        guard let c = o.topCandidates(1).first else { continue }
        lines.append(c.string)
        boxes.append(["text": c.string, "h": o.boundingBox.height, "w": o.boundingBox.width, "x": o.boundingBox.minX, "y": o.boundingBox.minY])
    }
    out.append(["path": path, "lines": lines, "boxes": boxes])
}
print(String(data: try! JSONSerialization.data(withJSONObject: out), encoding: .utf8)!)
