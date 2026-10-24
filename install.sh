#!/bin/bash
set -euo pipefail
umask 022
ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="/Applications/SN VPN.app"
RUNTIME="/Library/Application Support/Local VPN"

if [[ "$EUID" -eq 0 ]]; then
  echo '请以普通用户运行 bash install.sh；安装阶段会请求管理员授权。' >&2
  exit 1
fi
if [[ -e "$APP" || -L "$APP" || -e "$RUNTIME" || -L "$RUNTIME" || -e /Library/LaunchDaemons/local.vpn.autoconnect.plist || -L /Library/LaunchDaemons/local.vpn.autoconnect.plist || -e /Library/LaunchDaemons/local.snvpn.helper.plist || -L /Library/LaunchDaemons/local.snvpn.helper.plist ]]; then
  echo '检测到已有 App、运行环境或启动项；未覆盖。请按 README 的升级步骤处理。' >&2
  exit 1
fi
if [[ "$(uname -m)" != arm64 ]]; then
  echo '当前安装脚本仅支持 Apple Silicon Mac。' >&2
  exit 1
fi
/usr/bin/python3 -c 'import platform, sys; sys.exit(0 if int(platform.mac_ver()[0].split(".")[0]) >= 27 else "需要 macOS 27 或以上。")'
bash "$ROOT/prepare-runtime.sh"
bash "$ROOT/app/build.sh"

sudo -v
STEP='初始化运行环境'
trap 'echo "安装在${STEP}阶段中止；已完成的文件保留。请按 README 的维护入口检查并完成安装。" >&2' ERR
sudo /usr/bin/python3 -I "$ROOT/install.py" --initialize
STEP='复制 App'
sudo /usr/bin/ditto "$ROOT/app/dist/SN VPN.app" "$APP"
STEP='安装受限助手'
sudo /usr/bin/python3 -I "$ROOT/install.py" --install-helper "$APP" "$(id -u)"
trap - ERR
/usr/bin/open "$APP"
echo '安装完成。请在 App 中导入或新建连接；保存配置不会立即连接 VPN。'
