#!/usr/bin/python3
"""
美股收盘日报
数据源：
  · CNBC quote API: 指数 / 波动率 / 美债 / 美元 / 11 个 GICS 板块 / 重点个股（免费、无需 key）
  · RSS × 5: CNBC 财经头条 / CNBC 盈利 / Yahoo Finance / MarketWatch 市场 / Fortune
由 Claude 写稿：① 收盘仪表盘（指数+板块+异动）② 新闻播报列表，推送 2 条飞书富文本消息。
本脚本只负责抓取与推送，不含写稿用的第三方大模型 API。

两种运行模式（--mode，均零 API 成本）：
- fetch：抓取行情+新闻（含抓正文）→ context 打到 stdout + 写 logs/fetch_meta.json
- send ：读取 Claude 写好的两份稿子 → 依次推送飞书 + 写日志

关于"当天"的定义：本 bot 播报的是**最近一个已收盘的美股交易日**。北京时间
10:06 推送时，美东时间是前一日 22:06，所以周二～周六推送的是前一交易日收盘。
周日与周一（北京时间）没有新的收盘场次，脚本会输出 SKIP_NO_NEW_SESSION 并
跳过，避免把上周五的行情当成新内容重复播报。

写稿规范存放于 prompt_market.md（消息①）与 prompt_news.md（消息②）。
"""

import os
import sys
import time
import json
import argparse
import traceback
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 共享工具库
# 从脚本自身位置推导共享层（bot 目录的同级 shared/），
# 这样整个 bots 文件夹搬到任何位置都不用改路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from bot_utils import (sanitize_html, with_retry, fetch_rss, parse_entry_date,
                       already_ran_today, fetch_article_text,
                       url_key, load_sent_urls, record_sent_urls, extract_hrefs,
                       send_feishu, update_zero_streak, is_market_relevant,
                       make_logger, make_pending_saver, proxy_ok,
                       emit_fetch_output)

LOG_FILE   = Path(__file__).parent / "logs" / "run.log"
JSONL_FILE = Path(__file__).parent / "logs" / "run.jsonl"
LOG_FILE.parent.mkdir(exist_ok=True)
CACHE_FILE = Path(__file__).parent / "pending_messages.json"

# Claude routine 把写好的两份稿子分别存到这里，再用 --mode send 发送
DRAFT_MARKET = Path(__file__).parent / "logs" / "report_market.txt"  # 消息①收盘仪表盘
DRAFT_NEWS   = Path(__file__).parent / "logs" / "report_news.txt"    # 消息②新闻播报
# fetch 模式写出、send 模式读回的边车：承载 OK 日志摘要与 health_check 所需 metrics
FETCH_META   = Path(__file__).parent / "logs" / "fetch_meta.json"
# fetch 抓来的完整 stdout（marker + context）落盘一份：调用方截断、进程中断、
# 或写稿失败要重来时，不必再打一遍外部 API。每次 fetch 覆盖写，只留最近一次。
LAST_CONTEXT = Path(__file__).parent / "logs" / "last_context.txt"
# 跨天去重档案：send 成功后记录稿件里实际用到的链接，fetch 时据此排除
SENT_URLS    = Path(__file__).parent / "logs" / "sent_urls.json"
# RSS 源连续零产计数（fetch 阶段唯一写入，health_check 只读）
ZERO_STREAK  = Path(__file__).parent / "logs" / ".zero_streak.json"
ZERO_STREAK_THRESHOLD = 3
# 已播报过的交易日（避免周日/周一重复播上周五的收盘）
LAST_SESSION = Path(__file__).parent / "logs" / ".last_session"

# ===== P0: 显式代理配置 =====
_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
SESSION = requests.Session()
SESSION.proxies = {"http": _PROXY, "https": _PROXY}
# feedparser 内部使用 urllib，通过环境变量注入代理
if _PROXY:
    os.environ.setdefault("HTTP_PROXY",  _PROXY)
    os.environ.setdefault("HTTPS_PROXY", _PROXY)


# ===== P1: 结构化日志（实现见 shared/bot_utils.make_logger）=====
write_log = make_logger(LOG_FILE, JSONL_FILE)



# ===== 配置 =====
# 飞书自定义机器人：webhook 地址在群「设置 → 群机器人 → 添加机器人 → 自定义机器人」
# 里取得。若在那里勾了「签名校验」，把密钥一并放进 FEISHU_SECRET；没勾就留空。
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")
FEISHU_SECRET  = os.getenv("FEISHU_SECRET",  "")

# CNBC 免费 quote 接口，一次调用可取全部标的，无需 API key
CNBC_QUOTE_URL = (
    "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
    "?symbols={syms}&requestMethod=itv&noform=1&partnerId=2&fund=1"
    "&exthrs=1&output=json"
)

INDICES = [(".SPX", "标普500"), (".IXIC", "纳斯达克"),
           (".DJI", "道琼斯"), (".RUT", "罗素2000")]
MACRO   = [(".VIX", "VIX恐慌指数"), ("US10Y", "10年期美债"), (".DXY", "美元指数")]
# 11 个 GICS 板块，用 SPDR 行业 ETF 代理
SECTORS = [("XLK", "科技"), ("XLC", "通信服务"), ("XLY", "可选消费"),
           ("XLP", "必需消费"), ("XLE", "能源"), ("XLF", "金融"),
           ("XLV", "医疗"), ("XLI", "工业"), ("XLB", "原材料"),
           ("XLRE", "房地产"), ("XLU", "公用事业")]
# 重点个股：大市值 + 高关注度，用于挑出当日异动
WATCHLIST = ["NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
             "AMD", "NFLX", "ORCL", "CRM", "ADBE", "INTC", "MU", "QCOM",
             "PLTR", "SMCI", "ARM", "TSM", "JPM", "BRK.B", "LLY", "UNH",
             "XOM", "WMT", "COIN", "MSTR"]

# (feed_url, limit, is_general)
# is_general=True 表示这是泛财经源而非美股垂直源，条目要过 is_market_relevant 闸门。
# 垂直源不过闸，避免误伤标题不含关键词的正当选题。
#
# 选源标准：RSS 摘要或正文至少有一个能抓到。已排除：
#   · investing.com —— 标题好但摘要为空且正文被反爬拦截（实测 6 条全零内容）
#   · Seeking Alpha —— 付费墙
#   · Business Insider 市场源 —— 充斥律所集体诉讼通稿
#   · Motley Fool / MarketWatch topstories —— 退休理财建议专栏，非市场新闻
#   · Yahoo 的 feeds.finance.yahoo.com 按标的订阅接口 —— 持续返回 429
#     （只有下面这个 finance.yahoo.com/news/rssindex 可用）
#   · Reuters(404) / Barron's(403)
RSS_SOURCES = [
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html",        10, False),  # CNBC 财经头条
    ("https://www.cnbc.com/id/15839135/device/rss/rss.html",          8, False),  # CNBC 盈利/财报
    ("https://finance.yahoo.com/news/rssindex",                       8, False),  # Yahoo Finance
    ("https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",   6, False),  # MarketWatch 市场（深度，日更约 2-3 条）
    ("https://fortune.com/feed/fortune-feeds/?id=3230629",           10, True),   # Fortune（量大，约一半与美股无关，过闸门）
]

# ===== P2: 消息缓存（降级策略）=====
# 飞书推送失败时把稿件存到 pending_messages.json，避免内容丢失。
# 注意：**不做自动重发**——重发要判断"这稿子还是今天的吗"，跨天重发旧稿比丢一次
# 更糟。当天补救由 health_check → claude_catchup 重走完整流程负责，缓存只作为
# 人工恢复的兜底副本。
save_pending = make_pending_saver(CACHE_FILE)



# ===== 行情抓取 =====
def _num(s):
    """CNBC 返回的价格带千分位逗号与百分号，统一转成 float；失败返回 None。"""
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("%", "").replace("+", "").strip())
    except ValueError:
        return None


@with_retry(max_retries=2, base_delay=5, exceptions=(Exception,))
def fetch_quotes(symbols: list) -> dict:
    """一次取回全部标的行情，返回 {symbol: quote_dict}。"""
    url = CNBC_QUOTE_URL.format(syms="|".join(symbols))
    resp = SESSION.get(url, headers={"User-Agent": _UA}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    quotes = data.get("FormattedQuoteResult", {}).get("FormattedQuote", [])
    if isinstance(quotes, dict):          # 单标的时 CNBC 返回对象而非数组
        quotes = [quotes]
    return {q.get("symbol"): q for q in quotes if q.get("symbol")}


_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fmt_quote(q: dict) -> str:
    """格式化为「7,489.72 (+0.70%)」。"""
    if not q:
        return "未知"
    return f"{q.get('last', '?')} ({q.get('change_pct', '?')})"


def session_date(quotes: dict) -> str:
    """从标普的报价里取最近收盘交易日（YYYY-MM-DD）。"""
    q = quotes.get(".SPX", {})
    t = q.get("last_time", "") or ""
    return t.split("T")[0] if t else ""


# ===== 新闻抓取 =====
def build_news_context(entries: list) -> tuple:
    """整理新闻素材，返回 (news_lines, kept_per_source, drop_stats)。

    过滤顺序：标题/URL 缺失 → 单次运行内 URL 去重 → 跨天已播去重 → 48h 时间窗。
    用 48h 而非 24h：美股是隔夜市场，收盘后的解读文章常在次日发出。"""
    now        = datetime.now(timezone.utc)
    time_limit = now - timedelta(days=2)
    seen_urls: set = set()
    sent_before    = load_sent_urls(SENT_URLS)

    kept_per_source: dict = {}
    drops = {"dup": 0, "already_sent": 0, "stale": 0}
    picked = []

    for entry in entries:
        title = getattr(entry, "title", None)
        if not title:
            continue
        url = getattr(entry, "link", "") or getattr(entry, "id", "")
        low = url.lower()
        if not low or low in seen_urls:
            drops["dup"] += 1
            continue
        seen_urls.add(low)
        if url_key(url) in sent_before:
            drops["already_sent"] += 1
            continue
        pub = parse_entry_date(entry)
        if not pub or pub < time_limit:
            drops["stale"] += 1
            continue
        snippet = getattr(entry, "summary", "") or ""
        # 泛财经源过美股相关性闸门，垂直源直接放行
        if getattr(entry, "__general", False) and not is_market_relevant(title, snippet):
            drops["off_topic"] = drops.get("off_topic", 0) + 1
            continue
        src = getattr(entry, "__src", "?")
        kept_per_source[src] = kept_per_source.get(src, 0) + 1
        picked.append((title, url, src, snippet))

    def _material(item):
        title, url, src, snippet = item
        body = fetch_article_text(url)
        text = body if body else snippet[:500]
        kind = "正文" if body else "摘要"
        return src, text, (f"[原始英文标题] {title}\n[链接] {url}\n[媒体] {src}\n"
                           f"[正文/摘要（{kind}）] {text}\n----")

    lines = []
    if picked:
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(_material, picked))
        # 内容闸门：正文和摘要都抓不到的条目直接丢弃。
        # 被反爬拦截的源（RSS 摘要为空 + 正文抓不到）会白占名额，让 Claude 只能
        # 对着一个标题硬写——宁可少一条也不要无米之炊。丢弃后同步扣减该源的
        # kept 计数，这样零产监测统计的是"真正可用的产出"而非"进了池子的条数"。
        MIN_USABLE = 50
        for src, text, line in results:
            if len(text.strip()) < MIN_USABLE:
                drops["no_content"] = drops.get("no_content", 0) + 1
                kept_per_source[src] = max(0, kept_per_source.get(src, 1) - 1)
                continue
            lines.append(line)
    return lines, kept_per_source, drops


# ===== 推送飞书 =====
def send_report(text: str) -> int:
    """推送一份稿件，返回实际发出的消息条数。

    sanitize 一次、在转换前完成：HTML 只是内部中间格式，清洗保证正文里的裸
    < > & 不会被标签解析吃掉。转 post、按 20KB 分页、失败重试都由
    bot_utils.send_feishu 统一负责（三个 bot 共用同一实现）。"""
    return send_feishu(sanitize_html(text), FEISHU_WEBHOOK, FEISHU_SECRET)


# ===== 代理预检 + 端口自愈 =====
def _proxy_ok() -> bool:
    """代理预检 + 端口自愈，实现见 shared/bot_utils.proxy_ok。"""
    global _PROXY
    ok, _PROXY = proxy_ok(_PROXY, SESSION)
    return ok



# ===== 汇总抓取 =====
def gather() -> dict:
    print("📈 抓取行情...", file=sys.stderr)
    all_syms = ([s for s, _ in INDICES] + [s for s, _ in MACRO] +
                [s for s, _ in SECTORS] + WATCHLIST)
    try:
        quotes = fetch_quotes(all_syms)
    except Exception as e:
        print(f"[WARN] 行情抓取失败: {e}", file=sys.stderr)
        return None
    if not quotes.get(".SPX"):
        print("[WARN] 标普指数缺失，视为行情抓取失败", file=sys.stderr)
        return None
    print(f"  ✓ {len(quotes)} 个标的", file=sys.stderr)

    sess = session_date(quotes)
    print(f"  最近收盘交易日: {sess}", file=sys.stderr)

    print("\n📡 抓取 RSS 源...", file=sys.stderr)
    entries = []
    fetched_counts: dict = {}
    for url, limit, is_general in RSS_SOURCES:
        es = fetch_rss(url, limit)
        domain = url.split("/")[2]
        for e in es:
            e["__src"] = domain
            e["__general"] = is_general
        entries.extend(es)
        fetched_counts[domain] = fetched_counts.get(domain, 0) + len(es)
        print(f"  ✓ {len(es)} 条  {url}", file=sys.stderr)

    news_lines, kept_per_source, drops = build_news_context(entries)
    news_count = len(news_lines)

    source_stats = {d: {"fetched": n, "kept": kept_per_source.get(d, 0)}
                    for d, n in fetched_counts.items()}
    zero_sources = [d for d, s in source_stats.items() if s["kept"] == 0]
    stale_sources = update_zero_streak(ZERO_STREAK, zero_sources, list(source_stats),
                                       threshold=ZERO_STREAK_THRESHOLD)
    try:
        streak_now = json.loads(ZERO_STREAK.read_text(encoding="utf-8"))
    except Exception:
        streak_now = {}

    print(f"\n📰 共抓取 {len(entries)} 条 → 保留 {news_count} 条有效新闻", file=sys.stderr)
    print(f"   过滤明细：重复 {drops['dup']} · 已播过 {drops['already_sent']} · "
          f"超 48h {drops['stale']} · 非美股 {drops.get('off_topic', 0)} · "
          f"无正文 {drops.get('no_content', 0)}", file=sys.stderr)
    for d, s in sorted(source_stats.items(), key=lambda kv: -kv[1]["kept"]):
        n = streak_now.get(d, 0)
        flag = f"  ⚠️ 零产（连续 {n} 天）" if s["kept"] == 0 else ""
        print(f"   {d:26s} 抓{s['fetched']:>2} → 留{s['kept']:>2}{flag}", file=sys.stderr)

    return {"quotes": quotes, "session": sess, "news_lines": news_lines,
            "news_count": news_count, "entries": entries,
            "zero_sources": zero_sources, "source_stats": source_stats,
            "stale_sources": stale_sources,
            "today": datetime.now().strftime("%Y-%m-%d")}


def build_context(data: dict) -> str:
    """拼出交给 Claude 写稿的完整 context。"""
    q = data["quotes"]

    idx = "\n".join(f"{name}: {fmt_quote(q.get(sym))}" for sym, name in INDICES)
    mac = " | ".join(f"{name} {fmt_quote(q.get(sym))}" for sym, name in MACRO)

    # 板块按涨跌幅排序，让写稿时一眼看出轮动方向
    sec_rows = []
    for sym, name in SECTORS:
        qq = q.get(sym)
        if qq:
            sec_rows.append((name, sym, qq.get("change_pct", "?"), _num(qq.get("change_pct"))))
    sec_rows.sort(key=lambda r: (r[3] is None, -(r[3] or 0)))
    sec = "\n".join(f"{n}({s}): {p}" for n, s, p, _ in sec_rows)

    # 个股按涨跌幅排序，取两端各 8 个作为异动榜
    mv = []
    for sym in WATCHLIST:
        qq = q.get(sym)
        if qq:
            mv.append((sym, qq.get("shortName", sym), qq.get("last", "?"),
                       qq.get("change_pct", "?"), _num(qq.get("change_pct"))))
    mv.sort(key=lambda r: (r[4] is None, -(r[4] or 0)))
    gainers = "\n".join(f"{s}: {p} ({c})" for s, _, p, c, _ in mv[:8])
    losers  = "\n".join(f"{s}: {p} ({c})" for s, _, p, c, _ in mv[-8:][::-1])

    spx = q.get(".SPX", {})
    return (
        f"【收盘交易日】{data['session']}\n"
        f"----------------\n"
        f"【指数收盘】\n{idx}\n"
        f"标普 开{spx.get('open','?')} 高{spx.get('high','?')} 低{spx.get('low','?')} "
        f"52周高{spx.get('yrhiprice','?')} 52周低{spx.get('yrloprice','?')}\n"
        f"----------------\n"
        f"【宏观指标】\n{mac}\n"
        f"----------------\n"
        f"【板块表现（按涨跌幅排序）】\n{sec}\n"
        f"----------------\n"
        f"【重点个股涨幅前 8】\n{gainers}\n"
        f"----------------\n"
        f"【重点个股跌幅前 8】\n{losers}\n"
        f"----------------\n\n"
        + "\n".join(data["news_lines"])
    )


def _summary_and_metrics(data: dict) -> tuple:
    q = data["quotes"]
    spx, ixic = q.get(".SPX", {}), q.get(".IXIC", {})
    vix = q.get(".VIX", {})
    summary = (f"收盘{data['session']} 标普{spx.get('last','?')}({spx.get('change_pct','?')}) "
               f"纳指{ixic.get('last','?')}({ixic.get('change_pct','?')}) "
               f"VIX{vix.get('last','?')} → 抓取{len(data['entries'])}条 → 保留{data['news_count']}条")
    metrics = {
        "session": data["session"],
        "spx": spx.get("last"), "spx_pct": spx.get("change_pct"),
        "ixic_pct": ixic.get("change_pct"), "vix": vix.get("last"),
        "rss_fetched": len(data["entries"]), "rss_kept": data["news_count"],
        "rss_zero_sources": data["zero_sources"],
        "rss_source_stats": data["source_stats"],
        "rss_stale_sources": data["stale_sources"],
    }
    return summary, metrics


# ===== 模式 1：fetch =====
def run_fetch() -> int:
    if already_ran_today(LOG_FILE):
        print("=== SKIP_ALREADY_RAN ===")
        return 0

    if not _proxy_ok():
        print(f"=== SKIP_PROXY === {_PROXY}")
        return 0

    data = gather()
    if data is None:
        print("=== SKIP_NO_QUOTES ===")
        write_log("WARN", "行情数据抓取失败，跳过本次发送，等待下次运行")
        return 0

    # 同一个收盘场次只播一次：北京时间周日/周一没有新场次，
    # 美股假期同理。这是本 bot 特有的幂等条件，比"今天是否跑过"更准确。
    sess = data["session"]
    try:
        last = LAST_SESSION.read_text(encoding="utf-8").strip()
    except Exception:
        last = ""
    if sess and sess == last:
        print(f"=== SKIP_NO_NEW_SESSION === {sess}")
        write_log("WARN", f"无有效新闻（{sess} 收盘已播报过，无新交易日）")
        return 0

    summary, metrics = _summary_and_metrics(data)
    FETCH_META.write_text(
        json.dumps({"log_summary": summary, "metrics": metrics,
                    "session": sess}, ensure_ascii=False),
        encoding="utf-8",
    )

    # 先攒成列表再一次性交给 emit_fetch_output，顺带落盘 logs/last_context.txt。
    out = ["=== FETCH_OK ===",
           f"今天日期：{data['today']}",
           f"收盘交易日：{sess}",
           f"保留 {data['news_count']} 条有效新闻（共抓取 {len(data['entries'])} 条）"]
    # 上次播的是哪个场次。本 bot 按"最近一个已收盘交易日"取数，中间漏跑几天时会
    # 直接跳到最新场次，被跳过的那些收盘就永远不会播——2026-08-27 的收盘就是这么
    # 丢的（08-27 播的是 08-26，之后两天没跑，08-30 直接播 08-28）。两个日期并排
    # 打出来，写稿方一眼能看出中间是否缺了场次。
    if last and last != sess:
        out.append(f"上次播报的收盘交易日：{last}")
    if data["zero_sources"]:
        out.append(f"零产源：{', '.join(data['zero_sources'])}")
    if data["stale_sources"]:
        out.append("=== SOURCE_ALERT ===")
        for d, n in data["stale_sources"].items():
            out.append(f"{d} 已连续 {n} 天零产，建议从 RSS_SOURCES 移除或更换")
        out.append("=== SOURCE_ALERT_END ===")
    out += ["=== CONTEXT_BEGIN ===", build_context(data), "=== CONTEXT_END ==="]
    emit_fetch_output(out, LAST_CONTEXT)
    return 0


# ===== 模式 2：send =====
def run_send() -> int:
    t0 = time.time()

    if already_ran_today(LOG_FILE):
        print("今天已成功运行过，跳过发送。如需强制请设置 FORCE_RUN=1。", file=sys.stderr)
        return 0

    if not DRAFT_MARKET.exists() or not DRAFT_NEWS.exists():
        write_log("FAIL", f"稿子文件缺失：需要 {DRAFT_MARKET.name} 与 {DRAFT_NEWS.name}")
        return 1
    market = DRAFT_MARKET.read_text(encoding="utf-8").strip()
    news   = DRAFT_NEWS.read_text(encoding="utf-8").strip()
    if not market or not news:
        write_log("FAIL", "稿子文件为空（消息①或②）")
        return 1

    # 不再做代理预检：飞书直连可达，推送阶段本来就不需要翻墙代理。
    # （Telegram 时代代理一挂当天就整个不播，现在只有抓取阶段依赖它。）
    save_pending([market, news])
    print("📨 推送到飞书...", file=sys.stderr)
    send_report(market)
    save_pending([news])          # 消息①已发：更新缓存，防止重跑时重复推送
    send_report(news)
    CACHE_FILE.unlink(missing_ok=True)
    print("  ✓ 推送成功", file=sys.stderr)

    hrefs = extract_hrefs(market) + extract_hrefs(news)
    if hrefs:
        total = record_sent_urls(SENT_URLS, hrefs)
        print(f"  ✓ 已归档 {len(hrefs)} 条链接用于跨天去重（档案共 {total} 条）",
              file=sys.stderr)

    try:
        meta = json.loads(FETCH_META.read_text(encoding="utf-8"))
    except Exception:
        meta = {"log_summary": "Claude写稿", "metrics": {}, "session": ""}

    # 记下已播报的收盘场次，供下次 fetch 判断有无新场次
    if meta.get("session"):
        try:
            LAST_SESSION.write_text(meta["session"], encoding="utf-8")
        except Exception as e:
            print(f"[WARN] .last_session 写入失败: {e}", file=sys.stderr)

    duration = round(time.time() - t0, 1)
    write_log(
        "OK",
        f"Claude写稿 → {meta.get('log_summary', '')} → 飞书推送成功",
        metrics={**meta.get("metrics", {}), "ai_calls": 0, "source": "claude",
                 "duration_s": duration},
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="美股收盘日报")
    parser.add_argument(
        "--mode", choices=["fetch", "send"], required=True,
        help="fetch=抓取并输出 context（供 Claude 写稿，零 API）/ send=发送 Claude 写好的两稿",
    )
    parsed = parser.parse_args()

    try:
        sys.exit(run_fetch() if parsed.mode == "fetch" else run_send())
    except Exception:
        err = traceback.format_exc().strip().splitlines()[-1]
        write_log("FAIL", err)
        raise
