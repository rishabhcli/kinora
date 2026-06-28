#!/usr/bin/env bash
# Assemble a real `KinoraGlass.app` bundle from the SwiftPM executable.
#
# This is the CLT/Xcode-26-SDK packaging path the Makefile `app-native-bundle` target
# drives. It:
#   1. release-builds the `KinoraGlass` executable against the macOS 26+ SDK (so the OS
#      turns on real Liquid Glass);
#   2. lays out a standard `.app` bundle (MacOS/ + Resources/ + Info.plist);
#   3. writes an Info.plist that declares the `kinora://` URL scheme, the PDF/EPUB
#      document types (Open-With / file-drop), and notification + media usage;
#   4. optionally embeds a bundled renderer build (apps/desktop/dist) so the shipped app
#      works with no dev server — set KINORA_EMBED_RENDERER=1 to copy it in;
#   5. ad-hoc code-signs so notifications + Keychain access work locally.
#
# Usage (from repo root, via Makefile):  make app-native-bundle
# Direct:                                bash apps/desktop-native/build-app.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

APP_NAME="KinoraGlass"
BUNDLE_ID="local.kinora.glass"
VERSION="1.0.0"
APP_DIR="$HERE/$APP_NAME.app"
CONTENTS="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RES_DIR="$CONTENTS/Resources"

# Point at the installed Xcode (26+ SDK). The Makefile also sets DEVELOPER_DIR, but set a
# sensible default here so the script is runnable standalone.
if [[ -z "${DEVELOPER_DIR:-}" ]]; then
  XCODE_APP="$(ls -d /Applications/Xcode*.app 2>/dev/null | head -1 || true)"
  if [[ -n "$XCODE_APP" ]]; then export DEVELOPER_DIR="$XCODE_APP/Contents/Developer"; fi
fi
SWIFT="${DEVELOPER_DIR:+$DEVELOPER_DIR/Toolchains/XcodeDefault.xctoolchain/usr/bin/swift}"
SWIFT="${SWIFT:-swift}"

echo "==> Building $APP_NAME (release) with $SWIFT"
"$SWIFT" build -c release --product "$APP_NAME"

BIN_PATH="$("$SWIFT" build -c release --product "$APP_NAME" --show-bin-path)/$APP_NAME"
if [[ ! -x "$BIN_PATH" ]]; then
  echo "!! built binary not found at $BIN_PATH" >&2
  exit 1
fi

echo "==> Assembling bundle at $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR" "$RES_DIR"
cp "$BIN_PATH" "$MACOS_DIR/$APP_NAME"

# Copy the SwiftPM resource bundle (bundled demo films) next to the binary so
# Bundle.module resolves inside the .app.
BIN_DIR="$(dirname "$BIN_PATH")"
if compgen -G "$BIN_DIR/${APP_NAME}_${APP_NAME}.bundle" > /dev/null; then
  cp -R "$BIN_DIR/${APP_NAME}_${APP_NAME}.bundle" "$MACOS_DIR/"
fi
# Newer SwiftPM names it KinoraGlass_KinoraGlass.bundle; copy any *.bundle to be safe.
for b in "$BIN_DIR"/*.bundle; do
  [[ -e "$b" ]] && cp -R "$b" "$MACOS_DIR/" || true
done

# Optionally embed a built renderer (apps/desktop/dist) so the shell needs no dev server.
if [[ "${KINORA_EMBED_RENDERER:-0}" == "1" ]]; then
  DIST="$HERE/../desktop/dist"
  if [[ -d "$DIST" ]]; then
    echo "==> Embedding renderer build from $DIST"
    mkdir -p "$RES_DIR/dist"
    cp -R "$DIST/." "$RES_DIR/dist/"
  else
    echo "!! KINORA_EMBED_RENDERER=1 but $DIST not found (run: make app-desktop-build)" >&2
  fi
fi

echo "==> Writing Info.plist"
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>Kinora</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>26.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>NSSupportsAutomaticTermination</key><true/>
  <key>NSSupportsSuddenTermination</key><false/>
  <!-- Real Liquid Glass + window vibrancy -->
  <key>NSRequiresAquaSystemAppearance</key><false/>
  <!-- Custom URL scheme for deep links: kinora://book/<id> etc. -->
  <key>CFBundleURLTypes</key>
  <array>
    <dict>
      <key>CFBundleURLName</key><string>$BUNDLE_ID.url</string>
      <key>CFBundleURLSchemes</key><array><string>kinora</string></array>
    </dict>
  </array>
  <!-- Open-With / file-drop document types -->
  <key>CFBundleDocumentTypes</key>
  <array>
    <dict>
      <key>CFBundleTypeName</key><string>PDF Document</string>
      <key>CFBundleTypeRole</key><string>Viewer</string>
      <key>LSItemContentTypes</key><array><string>com.adobe.pdf</string></array>
    </dict>
    <dict>
      <key>CFBundleTypeName</key><string>EPUB Document</string>
      <key>CFBundleTypeRole</key><string>Viewer</string>
      <key>LSItemContentTypes</key><array><string>org.idpf.epub-container</string></array>
    </dict>
  </array>
  <!-- Usage descriptions -->
  <key>NSUserNotificationsUsageDescription</key>
  <string>Kinora notifies you when a film clip finishes generating.</string>
</dict>
</plist>
PLIST

echo "==> Ad-hoc code-signing"
codesign --force --deep --sign - "$APP_DIR" 2>/dev/null || \
  echo "!! codesign failed (notifications/keychain may be limited) — continuing"

echo "==> Built $APP_DIR"
