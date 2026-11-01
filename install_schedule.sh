#!/bin/zsh
# 安装 / 卸载 macOS launchd 定时任务
#   ./install_schedule.sh           安装（默认每天 08:30 和 21:30 运行）
#   ./install_schedule.sh uninstall 卸载
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.minsky.monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3)"

if [[ "$1" == "uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "已卸载定时任务 $LABEL"
  exit 0
fi

mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/monitor.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>30</integer></dict>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    <!-- 如使用 Bark/Server酱/Telegram/邮件，可在此添加密钥，例如：
    <key>BARK_KEY</key><string>你的key</string>
    -->
  </dict>
  <key>StandardOutPath</key><string>$DIR/logs/monitor.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/monitor.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "已安装定时任务：每天 08:30 / 21:30 运行"
echo "  plist: $PLIST"
echo "  日志:  $DIR/logs/monitor.log"
echo "立即手动触发一次： launchctl kickstart gui/$(id -u)/$LABEL"
