// Reads the QR codes in a PNG with macOS's own two barcode readers, so a square check-in drew itself is checked
// against something that didn't draw it. Core Image's reader is the one an iPhone's camera uses.
//
// Usage: swift read_qr.swift <file.png>   Prints one "reader: text" line per code found, or "none".

import CoreImage
import Foundation
import Vision

guard CommandLine.arguments.count > 1,
      let image = CIImage(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])) else {
    print("usage: swift read_qr.swift <file.png>")
    exit(2)
}

var found = 0

let options = [CIDetectorAccuracy: CIDetectorAccuracyHigh]
if let detector = CIDetector(ofType: CIDetectorTypeQRCode, context: nil, options: options) {
    for case let code as CIQRCodeFeature in detector.features(in: image) {
        print("CoreImage: \(code.messageString ?? "")")
        found += 1
    }
}

let request = VNDetectBarcodesRequest()
request.symbologies = [.qr]
try? VNImageRequestHandler(ciImage: image).perform([request])
for code in request.results ?? [] {
    print("Vision: \(code.payloadStringValue ?? "")")
    found += 1
}

if found == 0 {
    print("none")
    exit(1)
}
