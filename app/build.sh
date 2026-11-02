#!/bin/bash
# ReviewBot.app 을 만든다. Xcode 프로젝트 없이 SwiftPM 산출물을 번들로 조립한다.
set -euo pipefail
cd "$(dirname "$0")"

CONFIG="${1:-release}"
APP="ReviewBot.app"
# Swift 6.4's default swiftbuild engine stamped SDK 14.0 (the minimum OS)
# into this app's LC_BUILD_VERSION. Keep the native build engine and explicitly
# select the installed SDK so rebuilding does not change SDK-linked UI behavior.
SDK_PATH="$(xcrun --sdk macosx --show-sdk-path)"
SDK_VERSION="$(xcrun --sdk macosx --show-sdk-version)"
BUILD_ARGS=(--build-system native --sdk "$SDK_PATH" -c "$CONFIG")

echo "▸ 빌드 ($CONFIG, native, SDK $SDK_VERSION)"
if [ "${REVIEW_BOT_UNIVERSAL:-0}" = "1" ]; then
    # --arch switches recent SwiftPM back to swiftbuild, which stamps SDK 14.0.
    # Explicit triples keep the native driver for both slices before lipo merges them.
    SLICES=()
    for ARCH in arm64 x86_64; do
        TRIPLE="$ARCH-apple-macosx14.0"
        xcrun swift build "${BUILD_ARGS[@]}" --triple "$TRIPLE"
        SLICES+=("$(xcrun swift build "${BUILD_ARGS[@]}" --triple "$TRIPLE" --show-bin-path)/ReviewBotApp")
    done
    mkdir -p .build/review-bot-universal
    BIN="$(pwd)/.build/review-bot-universal/ReviewBotApp"
    xcrun lipo -create "${SLICES[@]}" -output "$BIN"
else
    BIN="$(xcrun swift build "${BUILD_ARGS[@]}" --show-bin-path)/ReviewBotApp"
    xcrun swift build "${BUILD_ARGS[@]}"
fi

# Check the linked artifact before replacing either the bundle or installed app.
BUILT_SDK="$(xcrun otool -l "$BIN" | awk '/LC_BUILD_VERSION/ { found=1 } found && $1 == "sdk" { print $2; found=0 }')"
python3 - "$SDK_VERSION" "$BUILT_SDK" <<'PYSDK'
import sys

def version(value):
    parts = [int(part) for part in value.split(".")]
    return tuple((parts + [0, 0])[:3])

try:
    slices = sys.argv[2].split()
    matches = bool(slices) and all(version(sys.argv[1]) == version(sdk) for sdk in slices)
except ValueError:
    matches = False
if not matches:
    sys.exit("빌드 SDK 불일치: 선택 %s / 실행 파일 %s — 기존 앱을 유지합니다."
             % (sys.argv[1], sys.argv[2] or "확인 불가"))
PYSDK
echo "▸ 실행 파일 SDK 확인: $BUILT_SDK"

echo "▸ 번들 조립"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 아이콘. 그리는 코드는 icon/make-icon.swift 에 있다 (외부 도구 없이 CoreGraphics).
if [ -f icon/ReviewBot.icns ]; then
  cp icon/ReviewBot.icns "$APP/Contents/Resources/ReviewBot.icns"
fi
cp "$BIN" "$APP/Contents/MacOS/ReviewBot"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>ReviewBot</string>
  <key>CFBundleDisplayName</key>       <string>review-bot</string>
  <key>CFBundleIdentifier</key>        <string>com.review-bot.app</string>
  <key>CFBundleExecutable</key>        <string>ReviewBot</string>
  <key>CFBundleIconFile</key>          <string>ReviewBot</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key>           <string>1</string>
  <key>LSMinimumSystemVersion</key>    <string>14.0</string>
  <!-- 일반 앱으로 Dock·앱 전환기에 표시한다. 메뉴바 아이콘은 MenuBarExtra로 유지한다. -->
  <key>LSUIElement</key>               <false/>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>NSAppleEventsUsageDescription</key><string>선택한 PR의 분석 내용을 iTerm에서 Codex 또는 Claude로 이어서 작업합니다.</string>
</dict>
</plist>
PLIST

python3 - "$APP/Contents/Info.plist" ../VERSION <<'PYVERSION'
import plistlib, sys
from pathlib import Path
target = Path(sys.argv[1])
data = plistlib.loads(target.read_bytes())
version = Path(sys.argv[2]).read_text().strip()
data['CFBundleShortVersionString'] = version
data['CFBundleVersion'] = version
target.write_bytes(plistlib.dumps(data))
PYVERSION

# 서명 신원은 좋은 것부터 고른다. Apple 이 발급한 인증서가 있으면 알림 센터까지 열리고,
# 자체 서명이면 신원만 안정된다(빌드마다 권한이 초기화되지 않는다). 둘 다 없으면 ad-hoc.
pick_identity() {
    local list; list="$(security find-identity -v -p codesigning 2>/dev/null || true)"
    for want in "Apple Development" "Developer ID Application" "review-bot-local"; do
        if echo "$list" | grep -q "$want"; then echo "$want"; return; fi
    done
    echo "-"
}
IDENTITY="${REVIEW_BOT_SIGN_IDENTITY:-$(pick_identity)}"
if [ "$IDENTITY" = "-" ]; then
    echo "▸ 서명: ad-hoc (신원 없음 — ./setup-signing.sh 로 만들 수 있습니다)"
else
    echo "▸ 서명: $IDENTITY"
fi
SIGN_ARGS=(--force --deep --sign "$IDENTITY")
case "$IDENTITY" in
    "Developer ID Application"*) SIGN_ARGS+=(--options runtime --timestamp) ;;
esac
codesign "${SIGN_ARGS[@]}" "$APP"

# /Applications 에 이미 설치돼 있으면 거기까지 갱신한다. 빌드본과 설치본이 갈라져서
# "고쳤는데 그대로다" 가 되는 것을 막는다. --no-install 로 끌 수 있다.
if [ "${2:-}" != "--no-install" ] && [ -d "/Applications/$APP" ]; then
    echo "▸ /Applications 갱신"
    osascript -e 'tell application "ReviewBot" to quit' >/dev/null 2>&1 || true
    sleep 1
    rm -rf "/Applications/$APP"
    cp -R "$APP" /Applications/
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
        -f "/Applications/$APP" >/dev/null 2>&1 || true
fi

echo "▸ 완료: $(pwd)/$APP"
echo "  실행:  open -a ReviewBot"
echo "  설치:  cp -R $APP /Applications/"
