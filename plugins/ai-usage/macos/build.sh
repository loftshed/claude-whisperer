#!/bin/sh
# Builds "AI Usage.app", a menu bar front end that runs `ai-usage json`.
# Usage: macos/build.sh [app path]   (default: ~/Applications/AI Usage.app)
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
app="${1:-$HOME/Applications/AI Usage.app}"
command_path="${AI_USAGE_COMMAND:-$HOME/.local/bin/ai-usage}"

mkdir -p "$app/Contents/MacOS"
xcrun swiftc -O -swift-version 5 -framework AppKit -o "$app/Contents/MacOS/AIUsageBar" "$here/main.swift"

cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>AI Usage</string>
  <key>CFBundleDisplayName</key><string>AI Usage</string>
  <key>CFBundleIdentifier</key><string>local.ai-usage.bar</string>
  <key>CFBundleExecutable</key><string>AIUsageBar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>AIUsageCommand</key><string>$command_path</string>
  <key>AIUsageBuildHash</key><string>${AI_USAGE_BUILD_HASH:-}</string>
</dict>
</plist>
EOF

codesign --force --sign - "$app" >/dev/null 2>&1 || echo "warning: ad-hoc codesign failed; the app still runs locally" >&2
echo "built $app"
