// 앱 아이콘을 그려 PNG 로 굽는다. 외부 도구 없이 CoreGraphics 만 쓴다.
//
// 모티브: 코드 리뷰 — 말풍선(리뷰 코멘트) 안에 체크(승인). 리뷰 봇이 하는 일이
// "읽고 → 판단해서 → 코멘트를 남긴다" 라서 그 셋을 한 도형에 담는다.
import AppKit

let size = 1024.0
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()
guard let ctx = NSGraphicsContext.current?.cgContext else { exit(1) }

// 배경: macOS 아이콘 관례대로 둥근 사각형. 위에서 아래로 옅어지는 남색.
let inset = size * 0.085
let rect = CGRect(x: inset, y: inset, width: size - inset * 2, height: size - inset * 2)
let squircle = CGPath(roundedRect: rect,
                      cornerWidth: rect.width * 0.235, cornerHeight: rect.height * 0.235,
                      transform: nil)
ctx.saveGState()
ctx.addPath(squircle)
ctx.clip()
let space = CGColorSpaceCreateDeviceRGB()
let top = CGColor(colorSpace: space, components: [0.30, 0.44, 0.86, 1])!
let bottom = CGColor(colorSpace: space, components: [0.16, 0.24, 0.58, 1])!
if let grad = CGGradient(colorsSpace: space, colors: [top, bottom] as CFArray,
                         locations: [0, 1]) {
    ctx.drawLinearGradient(grad, start: CGPoint(x: 0, y: rect.maxY),
                           end: CGPoint(x: 0, y: rect.minY), options: [])
}
ctx.restoreGState()

// 말풍선
let bw = rect.width * 0.62, bh = rect.height * 0.46
let bx = rect.midX - bw / 2, by = rect.midY - bh / 2 + rect.height * 0.06
let bubble = CGMutablePath()
bubble.addRoundedRect(in: CGRect(x: bx, y: by, width: bw, height: bh),
                      cornerWidth: bh * 0.28, cornerHeight: bh * 0.28)
// 꼬리
let tailX = bx + bw * 0.28, tailY = by
bubble.move(to: CGPoint(x: tailX, y: tailY + 2))
bubble.addLine(to: CGPoint(x: tailX + bw * 0.10, y: tailY - bh * 0.30))
bubble.addLine(to: CGPoint(x: tailX + bw * 0.26, y: tailY + 2))
bubble.closeSubpath()
ctx.addPath(bubble)
ctx.setFillColor(CGColor(colorSpace: space, components: [1, 1, 1, 0.97])!)
ctx.fillPath()

// 체크
ctx.setStrokeColor(CGColor(colorSpace: space, components: [0.18, 0.27, 0.62, 1])!)
ctx.setLineWidth(bh * 0.135)
ctx.setLineCap(.round)
ctx.setLineJoin(.round)
ctx.move(to: CGPoint(x: bx + bw * 0.26, y: by + bh * 0.52))
ctx.addLine(to: CGPoint(x: bx + bw * 0.44, y: by + bh * 0.33))
ctx.addLine(to: CGPoint(x: bx + bw * 0.75, y: by + bh * 0.70))
ctx.strokePath()

img.unlockFocus()

guard let tiff = img.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff),
      let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"
try! png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
