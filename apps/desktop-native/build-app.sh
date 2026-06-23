#!/usr/bin/env bash
# Assemble the SwiftPM executable into a real macOS .app bundle so it has a
# bundle identity (persistent WKWebView storage, Dock presence) and an Info.plist
# that opts into the macOS 26+ Liquid Glass design system.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${1:-debug}"

swift build --package-path "$ROOT" $([ "$CONFIG" = release ] && echo "-c release")

APP="$ROOT/KinoraGlass.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$ROOT/.build/$CONFIG/KinoraGlass" "$APP/Contents/MacOS/KinoraGlass"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Kinora</string>
  <key>CFBundleDisplayName</key><string>Kinora</string>
  <key>CFBundleIdentifier</key><string>com.kinora.desktop</string>
  <key>CFBundleExecutable</key><string>KinoraGlass</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>26.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.entertainment</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "built $APP"
