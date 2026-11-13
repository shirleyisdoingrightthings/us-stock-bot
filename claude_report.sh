#!/bin/bash
# claude_report.sh — 供定时任务调用的薄封装（委托 shared/report_base.sh）
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.shirley.us-stock-bot.plist"
SCRIPT="stock_report.py"
USAGE_MSG="用法: claude_report.sh {fetch|send}"
source "$DIR/../shared/report_base.sh"
