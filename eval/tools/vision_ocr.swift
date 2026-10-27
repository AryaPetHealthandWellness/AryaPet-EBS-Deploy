// The iOS app's image OCR (AryaPet-MVP ios/Runner/AppDelegate.swift, channel
// "aryapet/image_ocr"), run on macOS so photos can be scored without a phone.
// Same request settings, same output: a JSON list of fragments with top-down,
// normalised boxes.
//
//   swiftc -O eval/tools/vision_ocr.swift -o eval/tools/vision_ocr
//   eval/tools/vision_ocr photo.jpg > blocks.json
import Foundation
import ImageIO
import Vision

let path = CommandLine.arguments[1]
guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
  print("[]"); exit(0)
}
let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
let raw = (props?[kCGImagePropertyOrientation] as? UInt32) ?? 1
let orientation = CGImagePropertyOrientation(rawValue: raw) ?? .up

var blocks: [[String: Any]] = []
let request = VNRecognizeTextRequest { req, error in
  guard error == nil, let observations = req.results as? [VNRecognizedTextObservation] else { return }
  for o in observations {
    guard let c = o.topCandidates(1).first else { continue }
    let b = o.boundingBox
    blocks.append(["text": c.string, "x": b.minX, "y": 1.0 - b.maxY, "width": b.width, "height": b.height])
  }
}
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
// This Mac may be a VM with no Neural Engine; the CPU path gives the same text.
request.usesCPUOnly = true
try? VNImageRequestHandler(cgImage: image, orientation: orientation, options: [:]).perform([request])
let data = try! JSONSerialization.data(withJSONObject: blocks)
print(String(data: data, encoding: .utf8)!)
