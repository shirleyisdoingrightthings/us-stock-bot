#!/bin/bash
# auto_repair.sh — US Stock Bot
# 薄包装：设置参数后委托共享的两级自愈逻辑
# 修复逻辑统一维护在 ../shared/auto_repair_base.sh

DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="US Stock Bot"
SCRIPT="$DIR/stock_report.py"
ERROR="${1:-未知错误}"
DRAFTS="logs/report_market.txt logs/report_news.txt"

source "$DIR/../shared/auto_repair_base.sh"
