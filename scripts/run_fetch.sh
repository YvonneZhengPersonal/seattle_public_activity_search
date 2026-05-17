#!/usr/bin/env bash
# 在项目根目录执行抓取，写入 web/data.json。
# 本机每月定时示例（每月 1 号 09:00）：
#   crontab -e
#   0 9 1 * * /path/to/seattle_activity_search/scripts/run_fetch.sh >> /path/to/logs/fetch.log 2>&1
#
# macOS launchd：用绝对路径写一个 plist，ProgramArguments 指向本脚本，
# StartCalendarInterval 设 Month=1 Day=1 Hour=9 Minute=0。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec python3 fetch_seattle_activities.py "$@"
