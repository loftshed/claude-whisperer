#!/bin/sh
# Builds "Monitor.app", a menu bar front end that runs `monitor json`.
# Usage: macos/build.sh [app path]   (default: ~/Applications/Monitor.app)
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
app="${1:-$HOME/Applications/Monitor.app}"
command_path="${MONITOR_COMMAND:-$HOME/.local/bin/monitor}"

mkdir -p "$app/Contents/MacOS"
xcrun swiftc -O -swift-version 5 -framework AppKit -o "$app/Contents/MacOS/MonitorBar" "$here/main.swift"

cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Monitor</string>
  <key>CFBundleDisplayName</key><string>Monitor</string>
  <key>CFBundleIdentifier</key><string>local.monitor.bar</string>
  <key>CFBundleExecutable</key><string>MonitorBar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>MonitorCommand</key><string>$command_path</string>
  <key>MonitorBuildHash</key><string>${MONITOR_BUILD_HASH:-}</string>
</dict>
</plist>
EOF

codesign --force --sign - "$app" >/dev/null 2>&1 || echo "warning: ad-hoc codesign failed; the app still runs locally" >&2
echo "built $app"
