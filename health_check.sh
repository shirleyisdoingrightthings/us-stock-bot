#!/bin/bash
# health_check.sh — US Stock Bot
# 薄封装：设好本 bot 的差异点后，交给同级 shared/health_check_base.sh 执行。
# 完整流程与踩坑记录见那个文件，别把逻辑抄回来。

DIR="$(cd "$(dirname "$0")" && pwd)"

BOT_NAME="US Stock Bot"

# 终态 WARN 的匹配模式。⚠️ 必须与 stock_report.py 里 write_log("WARN", ...) 的实际措辞
# 一致——对不上就会把"正常的没东西可播"误判成缺跑，白派一次无头补跑。
NO_NEWS_PATTERN="无有效新闻"
NO_NEWS_MSG="今天无有效新闻，未出稿（非故障）"

STALE_KEY="rss_stale_sources"
ZERO_KEY="rss_zero_sources"

# 第 4 节：标普点位缺失 + 新闻条数偏低
content_check() {
    local spx kept
    spx=$(jsonl_field spx 未知)
    if [ "$spx" = "未知" ]; then
        notify WARN "标普点位缺失，请检查 CNBC 行情接口"
        echo "[health_check] WARN: 标普点位缺失"
    fi
    kept=$(jsonl_field rss_kept 99)
    if [ -n "$kept" ] && [ "$kept" -lt 3 ] 2>/dev/null; then
        notify WARN "本次仅保留 ${kept} 条新闻，请检查 RSS 源"
        echo "[health_check] WARN: rss_kept=${kept}，新闻数量异常偏低"
    fi
}

source "$DIR/../shared/health_check_base.sh"
