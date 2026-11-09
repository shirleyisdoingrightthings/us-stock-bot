#!/usr/bin/python3
"""
美股收盘日报
数据源：
  · CNBC quote API: 指数 / 波动率 / 美债 / 美元 / 11 个 GICS 板块 / 重点个股（免费、无需 key）
  · RSS × 4: CNBC 财经头条 / CNBC 盈利 / MarketWatch 市场 / Fortune
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
# 里取得。
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")

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

# 11 个 GICS 板块的核心成分股映射（用于排出领涨 Top 3 板块 & 领跌 Top 3 板块内各自的 Top 3 领涨/领跌个股）
SECTOR_STOCKS = {
    "XLK":  [("NVDA", "英伟达"), ("AAPL", "苹果"), ("MSFT", "微软"), ("AVGO", "博通"),
             ("AMD", "AMD"), ("ORCL", "甲骨文"), ("CRM", "Salesforce"), ("MU", "美光"),
             ("QCOM", "高通"), ("PLTR", "Palantir"), ("ADBE", "Adobe"), ("INTC", "英特尔")],
    "XLC":  [("META", "Meta"), ("GOOGL", "Alphabet"), ("NFLX", "奈飞"),
             ("TMUS", "T-Mobile"), ("DIS", "迪士尼"), ("VZ", "Verizon")],
    "XLY":  [("AMZN", "亚马逊"), ("TSLA", "特斯拉"), ("HD", "家得宝"),
             ("MCD", "麦当劳"), ("NKE", "耐克"), ("BKNG", "Booking")],
    "XLP":  [("WMT", "沃尔玛"), ("COST", "好市多"), ("PG", "宝洁"),
             ("KO", "可口可乐"), ("PEP", "百事"), ("PM", "菲利普莫里斯")],
    "XLE":  [("XOM", "埃克森美孚"), ("CVX", "雪佛龙"), ("COP", "康菲石油"),
             ("SLB", "斯伦贝谢"), ("EOG", "EOG能源"), ("OXY", "西方石油")],
    "XLF":  [("BRK.B", "伯克希尔"), ("JPM", "摩根大通"), ("V", "Visa"),
             ("MA", "万事达"), ("GS", "高盛"), ("COIN", "Coinbase")],
    "XLV":  [("LLY", "礼来"), ("UNH", "联合健康"), ("JNJ", "强生"),
             ("ABBV", "艾伯维"), ("MRK", "默沙东"), ("ISRG", "直觉外科")],
    "XLI":  [("GE", "GE航空"), ("CAT", "卡特彼勒"), ("RTX", "雷神技术"),
             ("UNP", "联合太平洋"), ("HON", "霍尼韦尔"), ("ETN", "伊顿")],
    "XLB":  [("LIN", "林德气体"), ("NEM", "纽蒙特矿业"), ("FCX", "自由港麦克莫兰"),
             ("SHW", "宣伟"), ("APD", "空气化工"), ("NUE", "纽柯钢铁")],
    "XLRE": [("PLD", "安博"), ("AMT", "美国电塔"), ("EQIX", "Equinix"),
             ("WELL", "Welltower"), ("SPG", "西蒙地产"), ("O", "Realty Income")],
    "XLU":  [("NEE", "NextEra"), ("CEG", "Constellation核电"), ("VST", "Vistra电力"),
             ("SO", "南方电力"), ("DUK", "杜克能源"), ("AEP", "美国电力")],
}

# 重点个股：大市值 + 高关注度（含 AI/芯片/加密映射股），用于全市场焦点异动与宽度统计
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
#   · Yahoo 全部接口 —— feeds.finance.yahoo.com 按标的订阅持续返回 429；
#     news/rssindex 虽可抓，但 2026-09-04 已因留存率最低撤除（见下），本源不再使用
#   · Reuters(404) / Barron's(403)
RSS_SOURCES = [
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html",        10, False),  # CNBC 财经头条
    ("https://www.cnbc.com/id/15839135/device/rss/rss.html",          8, False),  # CNBC 盈利/财报
    # 2026-09-04：撤掉 Yahoo Finance（finance.yahoo.com/news/rssindex）。
    # 9-03 那轮 fetched=8 / kept=2，是五个源里留存率最低的；内容以聚合转载为主，
    # 与 CNBC 两个源高度重叠，占着抓取额度却很少进正文。不要再加回来。
    ("https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",   6, False),  # MarketWatch 市场（深度，日更约 2-3 条）
    # 2026-11-06：撤掉泛商业杂志源 Fortune（约一半为职场/管理软文），
    # 经实测对比（Investing.com 摘要与正文均被反爬清零、Benzinga 新闻/财报源 403），
    # 替换为 Nasdaq 官方 Markets + Earnings 双源（与 CNBC/MarketWatch 零重合，且 100% 带结构化财报/市场数据摘要）：
    ("https://www.nasdaq.com/feed/rssoutbound?category=Markets",      6, False),  # Nasdaq 市场动态与焦点个股
    ("https://www.nasdaq.com/feed/rssoutbound?category=Earnings",     5, False),  # Nasdaq 财报电话会要点与业绩指引
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


# ===== 行情兜底源：Twelve Data（2026-09-04 加）=====
# 加它的理由：在此之前 18 个标的 100% 来自 CNBC 那一个接口，它一挂就是
# SKIP_NO_QUOTES —— 两条消息都不发，不只是行情那条。这是全 bot 唯一的单点。
#
# ⚠️ **免费档只覆盖板块 ETF，不含指数**。2026-09-04 用真 key 实测：
#   11 个板块 ETF（XLK/XLC/...）  ✓ 正常
#   4 个指数 + VIX + 美债 + 美元指数  ✗ 全部 symbol not found，
#   其中 TNX 明确回 "not available with your plan" —— 指数是付费功能。
#   symbol_search 反查 "S&P 500"/"VIX" 返回的也全是 ETF、没有指数本体。
# 所以本兜底只能出「有板块、无指数」的降级日报，比整个不发强，但不是完整替代。
# 指数那部分已于 2026-09-04 晚由 FMP 补上并实测通过（见下方 FMP 兜底）。
#
# ⚠️ 不要用 SPY/QQQ 冒充标普/纳指：百分比接近，但日报印的是指数点位，
# 塞 ETF 价格进去就是错的。
#
# 免费档限流：8 credit/分钟，按 symbol 计。11 个 ETF 必须分两批（8+3），
# 中间等一分钟——所以兜底路径最慢要跑 1 分多钟，只在 CNBC 已经失败时才走。
# ===== 行情兜底源之二：FMP（2026-09-04 加）=====
# 补的是 Twelve Data 拿不到的那半边：指数与宏观。两家的免费档限制方式完全不同——
#   Twelve Data 按**品类**限制：ETF 和股票开放，指数收费。
#   FMP 按**符号白名单**限制：不分品类，挑着给。
# 2026-09-04 用真 key 逐个实测的结果（别再重新试）：
#   ✓ ^GSPC ^IXIC ^DJI ^RUT ^VIX          —— 4 指数 + VIX 全可取，
#                                            数值与 CNBC 当日逐位吻合（7747.71 / 26584.06）
#   ✓ 10 年期美债                          —— quote 端点取不到（^TNX/TNX/US10Y 全 402），
#                                            但 /stable/treasury-rates 免费可用，
#                                            返回完整收益率曲线（year10 等，63 天历史）
#   ✗ 美元指数                             —— DX-Y.NYB / ^DXY / DXY / USDX 四种写法全 402
#   ✗ 11 个板块 ETF                        —— XLK/XLC/XLY/XLRE/XLU 抽样全 402，一个都取不到
#                                            （所以 Twelve Data 不能删，板块只有它能兜）
#   ✓ 18/28 只个股                         —— 白名单，取不到的是 AVGO ORCL CRM MU QCOM
#                                            SMCI ARM BRK.B LLY MSTR
#
# ⚠️ 三个必须知道的接口事实：
#   1. **v3 端点已废弃**：/api/v3/quote/ 返回 403 "Legacy Endpoint"，
#      仅 2025-08-31 前订阅的老用户可用。当前端点是 /stable/quote?symbol=X。
#      网上多数教程还在教 v3 写法，会直接撞墙。
#   2. **批量是付费的**：symbol=A,B,C 返回 402，只能一符号一请求。
#      但免费档 250 calls/day，我们一天最多 8 次，占 3%，无压力。
#   3. treasury-rates 的最新日期可能比收盘场次晚一天（美债数据本身滞后），
#      取值时要按日期挑最接近的那条，不能默认第一条就是当天。
FMP_API_KEY = os.getenv("FMP_API_KEY", "")
FMP_QUOTE_URL = "https://financialmodelingprep.com/stable/quote"
FMP_TREASURY_URL = "https://financialmodelingprep.com/stable/treasury-rates"
# CNBC 符号 → FMP 符号。美元指数 .DXY 不在这里：免费档取不到，只能标记缺失。
FMP_SYMBOL_MAP = {".SPX": "^GSPC", ".IXIC": "^IXIC", ".DJI": "^DJI",
                  ".RUT": "^RUT", ".VIX": "^VIX"}
# 交叉验证只查这两个：实测它俩与 CNBC 逐位吻合，适合做基准。
# VIX 不纳入——两边快照时点不同，实测 14.18 vs 14.32 差约 1%，
# 按 0.5% 阈值会天天误报。
# FMP 免费档能取到的个股白名单（2026-09-04 对 WATCHLIST 全部 28 只逐个实测）。
# 取不到的 10 只：AVGO ORCL CRM MU QCOM SMCI ARM BRK.B LLY MSTR —— 402 premium，
# 没有规律可循，就是白名单挑着给。**改 WATCHLIST 时记得同步实测这个列表**，
# 加进来没实测过的符号只会在兜底时白白消耗配额并报 402。
FMP_STOCK_OK = ["NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AMD",
                "NFLX", "ADBE", "INTC", "PLTR", "TSM", "JPM", "UNH", "XOM",
                "WMT", "COIN"]
# 逐个请求之间的间隔。免费档没有公布每分钟上限，给 0.5s 是保守值，
# 兜底路径取 18 只个股约需 9 秒——只在 CNBC 已经挂掉时才走这条路。
FMP_PACING_S = 0.5

FMP_CROSSCHECK = [".SPX", ".IXIC"]
FMP_CROSSCHECK_TOLERANCE = 0.005      # 相对偏差超过 0.5% 就告警（不阻断发稿）


def fetch_quotes_fmp(cnbc_symbols: list) -> dict:
    """兜底行情源之二。返回与 fetch_quotes 同形的 {cnbc_symbol: quote_dict}。

    只处理 FMP_SYMBOL_MAP 里有映射的符号（4 指数 + VIX）；
    10 年期美债由 fetch_treasury_10y 单独取，走的是完全不同的端点。
    任何一个符号失败只丢那一个，其余照常返回。
    """
    if not FMP_API_KEY:
        print("[WARN] 未配置 FMP_API_KEY，跳过 FMP 兜底", file=sys.stderr)
        return {}
    out = {}
    for cnbc in cnbc_symbols:
        # 指数/宏观走映射表；个股符号两边同名，但必须在实测过的白名单里
        fmp_sym = FMP_SYMBOL_MAP.get(cnbc)
        if not fmp_sym and cnbc in FMP_STOCK_OK:
            fmp_sym = cnbc
        if not fmp_sym:
            continue
        try:
            resp = SESSION.get(FMP_QUOTE_URL,
                               params={"symbol": fmp_sym, "apikey": FMP_API_KEY},
                               headers={"User-Agent": _UA}, timeout=25)
            resp.raise_for_status()
            rows = resp.json()
        except Exception as exc:
            print(f"[WARN] FMP {fmp_sym} 失败: {exc}", file=sys.stderr)
            continue
        if not isinstance(rows, list) or not rows:
            continue
        q = rows[0]
        price, pct = q.get("price"), q.get("changePercentage")
        if price is None:
            continue
        out[cnbc] = {
            "symbol": cnbc,
            "last": _td_fmt_price(price),
            "change_pct": _td_fmt_pct(pct),
            # FMP 不给收盘时间戳，交由调用方用别的标的推 session
            "last_time": "",
        }
        time.sleep(FMP_PACING_S)
    return out


def fetch_treasury_10y(session_day: str = "") -> dict:
    """取 10 年期美债收益率。返回 {"US10Y": quote_dict} 或 {}。

    走 /stable/treasury-rates（免费可用），返回的是收益率曲线的历史序列。
    ⚠️ 最新一条可能比收盘场次晚一天，所以按 session_day 挑「不晚于该日的最近一条」；
    没给 session_day 就取最新一条。
    """
    if not FMP_API_KEY:
        return {}
    try:
        resp = SESSION.get(FMP_TREASURY_URL, params={"apikey": FMP_API_KEY},
                           headers={"User-Agent": _UA}, timeout=25)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        print(f"[WARN] FMP 国债收益率失败: {exc}", file=sys.stderr)
        return {}
    if not isinstance(rows, list) or not rows:
        return {}
    pick = None
    for row in rows:                       # 接口按日期倒序返回
        if not session_day or (row.get("date") or "") <= session_day:
            pick = row
            break
    pick = pick or rows[0]
    y10 = pick.get("year10")
    if y10 is None:
        return {}
    note = "" if pick.get("date") == session_day else f"（{pick.get('date')}）"
    return {"US10Y": {"symbol": "US10Y", "last": f"{y10}%{note}",
                      "change_pct": "", "last_time": ""}}


TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
TWELVEDATA_URL = "https://api.twelvedata.com/quote"
_TD_BATCH = 8
_TD_WAIT_S = 62


def _td_fmt_price(v) -> str:
    """"185.97000" → "185.97"（带千分位，对齐 CNBC 的 last 字段格式）。"""
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "?"


def _td_fmt_pct(v) -> str:
    """"1.29085" → "+1.29%"（对齐 CNBC 的 change_pct 格式）。"""
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "?"


def fetch_quotes_twelvedata(symbols: list) -> dict:
    """兜底行情源。返回与 fetch_quotes 同形的 {symbol: quote_dict}。

    输出字段刻意对齐 CNBC（last / change_pct / last_time），
    这样 fmt_quote 和 session_date 都不用改。
    任何一批失败只丢那一批，已拿到的照常返回。
    """
    if not TWELVEDATA_API_KEY:
        print("[WARN] 未配置 TWELVEDATA_API_KEY，跳过兜底行情源", file=sys.stderr)
        return {}
    out = {}
    batches = [symbols[i:i + _TD_BATCH] for i in range(0, len(symbols), _TD_BATCH)]
    for n, batch in enumerate(batches):
        if n:
            print(f"  … 免费档限流 {_TD_BATCH} credit/分钟，等待 {_TD_WAIT_S}s",
                  file=sys.stderr)
            time.sleep(_TD_WAIT_S)
        try:
            resp = SESSION.get(TWELVEDATA_URL,
                               params={"symbol": ",".join(batch),
                                       "apikey": TWELVEDATA_API_KEY},
                               timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"[WARN] Twelve Data 第 {n + 1} 批失败: {exc}", file=sys.stderr)
            continue
        # 单标的时返回对象本身，多标的时返回 {symbol: obj}
        items = data if len(batch) > 1 else {batch[0]: data}
        if isinstance(items, dict) and items.get("status") == "error":
            print(f"[WARN] Twelve Data 第 {n + 1} 批报错: "
                  f"{str(items.get('message'))[:120]}", file=sys.stderr)
            continue
        for sym, q in items.items():
            if not isinstance(q, dict) or not q.get("close"):
                continue
            day = q.get("datetime", "")
            out[sym] = {
                "symbol": sym,
                "last": _td_fmt_price(q.get("close")),
                "change_pct": _td_fmt_pct(q.get("percent_change")),
                # session_date 只取 T 之前那段，补个收盘时刻即可
                "last_time": f"{day}T16:00:00" if day else "",
            }
    return out


def fmt_quote(q: dict) -> str:
    """格式化为「7,489.72 (+0.70%)」。"""
    if not q:
        return "未知"
    return f"{q.get('last', '?')} ({q.get('change_pct', '?')})"


def session_date(quotes: dict) -> str:
    """取最近收盘交易日（YYYY-MM-DD）。优先用标普，缺失时退回任意有时间的标的。

    退回分支是给 Twelve Data 兜底路径用的：那条路拿不到指数，只有板块 ETF，
    但 ETF 和指数在同一个交易时段收盘，日期一致。session 是本 bot 的幂等键
    （同一场次只播一次），推不出来的话整个流程就断了。
    """
    q = quotes.get(".SPX", {})
    t = q.get("last_time", "") or ""
    if t:
        return t.split("T")[0]
    for qq in quotes.values():
        t = (qq or {}).get("last_time", "") or ""
        if t:
            return t.split("T")[0]
    return ""


# ===== 加密核心行情（2026-11 合并自 crypto daily bot，仅保留行情指标，不抓加密新闻）=====
def fetch_crypto_snapshot() -> dict:
    """抓取核心币价（BTC/ETH/SOL/BNB/XRP/HYPE）、全球加密市值/BTC 市占率、恐惧贪婪指数。
    任一子项失败均降级为“未知”，绝不阻断美股主流程。"""
    cg_headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    prices = {k: "未知" for k in ("BTC", "ETH", "SOL", "BNB", "XRP", "HYPE")}
    try:
        url = (
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin,ethereum,solana,binancecoin,ripple,hyperliquid"
            "&vs_currencies=usd&include_24hr_change=true"
        )
        resp = SESSION.get(url, headers=cg_headers, timeout=15)
        resp.raise_for_status()
        pdata = resp.json()

        def _fmt_cg(key: str) -> str:
            d = pdata.get(key, {})
            price = d.get("usd", 0)
            change = d.get("usd_24h_change", 0) or 0
            if not price:
                return "未知"
            sign = "+" if change >= 0 else ""
            return f"${price:,.2f} ({sign}{change:.2f}%)"

        prices = {
            "BTC":  _fmt_cg("bitcoin"),
            "ETH":  _fmt_cg("ethereum"),
            "SOL":  _fmt_cg("solana"),
            "BNB":  _fmt_cg("binancecoin"),
            "XRP":  _fmt_cg("ripple"),
            "HYPE": _fmt_cg("hyperliquid"),
        }
    except Exception as e:
        print(f"[WARN] 加密币价抓取失败: {e}", file=sys.stderr)

    global_mkt = {
        "btc_dominance": "未知",
        "total_market_cap_b": "未知",
        "market_cap_change_24h": "未知",
        "bull_bear_label": "未知",
    }
    try:
        resp = SESSION.get("https://api.coingecko.com/api/v3/global",
                           headers=cg_headers, timeout=15)
        resp.raise_for_status()
        d = resp.json().get("data", {})
        btc_dom = d.get("market_cap_percentage", {}).get("btc", 0) or 0
        total_mc = d.get("total_market_cap", {}).get("usd", 0) or 0
        mc_chg = d.get("market_cap_change_percentage_24h_usd", 0) or 0
        if btc_dom >= 58:
            label = "BTC 主导（避险情绪，山寨承压）"
        elif btc_dom >= 52:
            label = "过渡区（BTC 与山寨拉锯）"
        elif btc_dom >= 46:
            label = "偏牛（资金轮动山寨）"
        else:
            label = "山寨季（高风险偏好）"
        global_mkt = {
            "btc_dominance": f"{btc_dom:.1f}%",
            "total_market_cap_b": f"${total_mc / 1e9:,.0f}B",
            "market_cap_change_24h": f"{'+' if mc_chg >= 0 else ''}{mc_chg:.2f}%",
            "bull_bear_label": label,
        }
    except Exception as e:
        print(f"[WARN] 加密全球市值抓取失败: {e}", file=sys.stderr)

    fear_val, fear_label = "未知", "未知"
    try:
        resp = SESSION.get("https://api.alternative.me/fng/", timeout=10)
        resp.raise_for_status()
        item = resp.json()["data"][0]
        fear_val, fear_label = item["value"], item["value_classification"]
    except Exception as e:
        print(f"[WARN] 恐惧贪婪主源(alternative.me)失败: {e}，改用 CMC 备用源", file=sys.stderr)
        try:
            now_ts = int(time.time())
            resp = SESSION.get(
                "https://api.coinmarketcap.com/data-api/v3/fear-greed/chart"
                f"?start={now_ts - 86400 * 2}&end={now_ts}",
                headers={"User-Agent": _UA}, timeout=10)
            resp.raise_for_status()
            point = resp.json()["data"]["dataList"][-1]
            fear_val, fear_label = str(point["score"]), point["name"]
        except Exception as e2:
            print(f"[WARN] 恐惧贪婪备用源(CMC)也失败: {e2}", file=sys.stderr)

    return {
        "prices": prices,
        "global": global_mkt,
        "fear_val": fear_val,
        "fear_label": fear_label,
    }


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
    return send_feishu(sanitize_html(text), FEISHU_WEBHOOK)


# ===== 代理预检 + 端口自愈 =====
def _proxy_ok() -> bool:
    """代理预检 + 端口自愈，实现见 shared/bot_utils.proxy_ok。"""
    global _PROXY
    ok, _PROXY = proxy_ok(_PROXY, SESSION)
    return ok



# ===== 均线趋势与乖离率（2026-11 借鉴 daily_stock_analysis 新增）=====
_US_MA_MAP = {
    ".SPX": ("us.INX", "标普500"),
    ".IXIC": ("us.IXIC", "纳斯达克"),
    ".DJI": ("us.DJI", "道琼斯"),
}


def fetch_us_ma_trends() -> dict:
    """直连腾讯财经日 K 接口拉取美股三大指数最近 25 日收盘价，计算 MA5/MA10/MA20 与 MA5 乖离率。"""
    out = {}
    s = requests.Session()
    s.trust_env = False
    s.proxies = {}
    for cnbc_sym, (gt_code, label) in _US_MA_MAP.items():
        try:
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={gt_code},day,,,25,qfq"
            resp = s.get(url, headers={"User-Agent": _UA}, timeout=8)
            klines = resp.json().get("data", {}).get(gt_code, {}).get("day") or []
            closes = [float(r[2]) for r in klines if len(r) >= 3 and r[2]]
            if len(closes) < 20:
                continue
            cur = closes[-1]
            ma5 = sum(closes[-5:]) / 5.0
            ma10 = sum(closes[-10:]) / 10.0
            ma20 = sum(closes[-20:]) / 20.0
            bias5 = (cur - ma5) / ma5 * 100.0 if ma5 else 0.0
            if ma5 > ma10 > ma20:
                trend = "🟢多头排列"
            elif ma5 < ma10 < ma20:
                trend = "🔴空头排列"
            elif cur >= ma20:
                trend = "🟡均线缠绕偏强(站上MA20)"
            else:
                trend = "🟡均线缠绕偏弱(跌破MA20)"
            out[cnbc_sym] = f"{trend} | MA5={ma5:,.2f} (乖离率 {bias5:+.2f}%) | MA20={ma20:,.2f}"
        except Exception:
            continue
    return out


# ===== 汇总抓取 =====
def gather() -> dict:
    print("📈 抓取行情...", file=sys.stderr)
    sector_member_syms = [sym for members in SECTOR_STOCKS.values() for sym, _ in members]
    all_syms = list(dict.fromkeys(
        [s for s, _ in INDICES] + [s for s, _ in MACRO] +
        [s for s, _ in SECTORS] + WATCHLIST + sector_member_syms + ["RSP"]
    ))
    quotes, degraded = {}, False
    try:
        quotes = fetch_quotes(all_syms)
    except Exception as e:
        print(f"[WARN] CNBC 行情抓取失败: {e}", file=sys.stderr)

    # CNBC 挂了（或返回里没有标普）→ 走 Twelve Data 兜底。
    # 兜底只覆盖板块 ETF，指数/宏观拿不到（免费档不含指数，见上面的说明），
    # 所以这是**降级出稿**：有板块轮动、没有指数点位，比 SKIP_NO_QUOTES
    # 整个不发要好。degraded 会一路传到 context，让写稿方明确交代。
    if not quotes.get(".SPX"):
        # 两家兜底分工互补，合起来覆盖「指数 + 宏观 + 板块」：
        #   FMP          → 4 指数 + VIX（+ 国债走另一个端点）；板块 ETF 它一个都取不到
        #   Twelve Data  → 11 个板块 ETF；指数它一个都取不到
        # 仍然补不上的只有美元指数和个股异动榜，由 build_context 按实际缺项标注。
        print("[WARN] CNBC 无标普数据，启用兜底源（FMP 指数 + Twelve Data 板块）",
              file=sys.stderr)
        # 指数/宏观 + 白名单内的个股一起要（个股用于异动榜，缺了榜就短一截）
        fb1 = fetch_quotes_fmp([s for s, _ in INDICES] + [s for s, _ in MACRO]
                               + [s for s in WATCHLIST if s in FMP_STOCK_OK])
        if fb1:
            quotes.update({k: v for k, v in fb1.items() if k not in quotes})
            n_idx = sum(1 for k in fb1 if k in FMP_SYMBOL_MAP)
            print(f"  ✓ FMP 取到 {n_idx} 个指数/宏观 + {len(fb1) - n_idx} 只个股",
                  file=sys.stderr)
        fb2 = fetch_quotes_twelvedata([s for s, _ in SECTORS])
        if fb2:
            quotes.update({k: v for k, v in fb2.items() if k not in quotes})
            print(f"  ✓ Twelve Data 取到 {len(fb2)} 个板块 ETF", file=sys.stderr)
        degraded = bool(fb1 or fb2)

    if not quotes:
        print("[WARN] 主源与兜底源均无数据，视为行情抓取失败", file=sys.stderr)
        return None
    print(f"  ✓ {len(quotes)} 个标的"
          f"{'（降级：数据来自兜底源）' if degraded else ''}", file=sys.stderr)

    sess = session_date(quotes)
    if not sess:
        print("[WARN] 推不出收盘交易日，视为行情抓取失败", file=sys.stderr)
        return None
    print(f"  最近收盘交易日: {sess}"
          f"{'（由兜底源推出）' if degraded else ''}", file=sys.stderr)

    # 国债要等 session 定下来才能取（要挑不晚于收盘日的那条），所以放在这里
    if degraded and not quotes.get("US10Y"):
        t10 = fetch_treasury_10y(sess)
        if t10:
            quotes.update(t10)
            print(f"  ✓ FMP 补到 10 年期美债 {t10['US10Y']['last']}", file=sys.stderr)

    # 交叉验证：CNBC 正常时用 FMP 核对两个基准指数。
    # 这是监控手段不是准入门槛——偏差只告警，绝不阻止发稿；
    # FMP 那侧失败就静默跳过，不制造噪音、不重试。
    crosscheck_note = ""
    if not degraded and FMP_API_KEY:
        try:
            ref = fetch_quotes_fmp(FMP_CROSSCHECK)
            for sym in FMP_CROSSCHECK:
                a, b = quotes.get(sym, {}).get("last"), ref.get(sym, {}).get("last")
                if not (a and b):
                    continue
                fa, fb_ = _num(a), _num(b)
                if not (fa and fb_):
                    continue
                dev = abs(fa - fb_) / fb_
                if dev > FMP_CROSSCHECK_TOLERANCE:
                    msg = (f"{sym} 交叉验证偏差 {dev:.2%}："
                           f"CNBC={a} vs FMP={b}")
                    write_log("WARN", msg)
                    crosscheck_note += f"⚠️ {msg}\n"
                    print(f"  ⚠️ {msg}", file=sys.stderr)
                else:
                    print(f"  ✓ {sym} 交叉验证通过（偏差 {dev:.3%}）", file=sys.stderr)
        except Exception as exc:
            print(f"  … 交叉验证跳过: {exc}", file=sys.stderr)

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

    print("\n🪙 抓取加密核心行情...", file=sys.stderr)
    crypto = fetch_crypto_snapshot()
    print(f"  ✓ BTC {crypto['prices'].get('BTC', '未知')} | 恐贪指数 {crypto['fear_val']} ({crypto['fear_label']})",
          file=sys.stderr)

    ma_trends = fetch_us_ma_trends()

    return {"quotes": quotes, "session": sess, "degraded": degraded,
            "crosscheck_note": crosscheck_note,
            "crypto": crypto,
            "ma_trends": ma_trends,
            "news_lines": news_lines,
            "news_count": news_count, "entries": entries,
            "zero_sources": zero_sources, "source_stats": source_stats,
            "stale_sources": stale_sources,
            "today": datetime.now().strftime("%Y-%m-%d")}


def build_context(data: dict) -> str:
    """拼出交给 Claude 写稿的完整 context。"""
    q = data["quotes"]
    # 降级模式：CNBC 挂了、走的 Twelve Data 兜底，只有板块 ETF 没有指数。
    # 必须显式告诉写稿方，否则它会照着模板去写「标普收于未知」这种句子。
    # 降级告示按**实际缺了什么**分级，而不是一律说"无指数"。
    # 两家兜底互补（FMP 给指数/宏观、Twelve Data 给板块），多数情况下骨架是全的，
    # 只缺美元指数和个股异动榜——那种程度不该让写稿方以为大盘数据没了。
    degraded_note = ""
    if data.get("degraded"):
        have_idx = [n for sym, n in INDICES if q.get(sym)]
        miss_idx = [n for sym, n in INDICES if not q.get(sym)]
        miss_mac = [n for sym, n in MACRO if not q.get(sym)]
        miss_sec = [n for sym, n in SECTORS if not q.get(sym)]
        miss_stk = [sym for sym in WATCHLIST if not q.get(sym)]

        lines = ["=== DEGRADED ===",
                 "⚠️ 本次 CNBC 行情源不可用，数据来自兜底源（FMP 指数/宏观 + Twelve Data 板块）。"]
        if miss_idx:
            lines.append(f"**缺失的指数**：{'、'.join(miss_idx)}")
        if miss_mac:
            lines.append(f"**缺失的宏观指标**：{'、'.join(miss_mac)}")
        if miss_sec:
            lines.append(f"**缺失的板块**：{'、'.join(miss_sec)}")
        if miss_stk:
            lines.append(f"**个股异动榜缺失 {len(miss_stk)} 只**：{'、'.join(miss_stk)}")

        lines.append("")
        lines.append("写稿要求：")
        if not have_idx:
            lines.append("· 指数全部缺失——不要写指数点位、不要编造涨跌幅、不要用「大盘收涨」"
                         "这类需要指数支撑的表述；改为只写板块轮动，"
                         "并在开头说明「本期指数数据缺失，仅呈现板块表现」。")
        else:
            lines.append(f"· 指数可用（{'、'.join(have_idx)}），照常写大盘。"
                         "上面列出的缺失项**一律不要提及、更不要编造数值**，"
                         "当它们不存在即可，不必向读者解释缺了什么。")
        if miss_stk:
            lines.append("· 个股异动榜按实际拿到的标的写，数量不足就少写几条，不要凑数。")
        lines.append("· 消息②（要闻）不受影响，照常写。")
        lines.append("=== DEGRADED_END ===")
        degraded_note = "\n".join(lines) + "\n\n"

    # 交叉验证告警：CNBC 与 FMP 对不上时提示写稿方，但不影响正常出稿
    degraded_note = (data.get("crosscheck_note") or "") + degraded_note

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

    def _sector_top3_stocks(sec_sym: str, is_gainer: bool) -> str:
        members = SECTOR_STOCKS.get(sec_sym, [])
        items = []
        for m_sym, m_cn in members:
            mq = q.get(m_sym)
            if mq and _num(mq.get("change_pct")) is not None:
                items.append((m_sym, m_cn, mq.get("last", "?"),
                              mq.get("change_pct", "?"), _num(mq.get("change_pct"))))
        items.sort(key=lambda x: x[4], reverse=is_gainer)
        top3 = items[:3]
        if not top3:
            return "暂无成分股行情"
        return "、".join(f"{cn}({sym}) {last} ({pct})" for sym, cn, last, pct, _ in top3)

    valid_secs = [r for r in sec_rows if r[3] is not None]
    top3_gain_secs = valid_secs[:3]
    top3_lose_secs = list(reversed(valid_secs[-3:])) if len(valid_secs) >= 3 else []

    sec_top3_gain_lines = []
    for idx_i, (n, s, p, _) in enumerate(top3_gain_secs, 1):
        sec_top3_gain_lines.append(
            f"{idx_i}. 【{n}({s})】 板块涨跌幅：{p}\n"
            f"   · 板块内领涨 Top 3：{_sector_top3_stocks(s, is_gainer=True)}"
        )
    sec_top3_lose_lines = []
    for idx_i, (n, s, p, _) in enumerate(top3_lose_secs, 1):
        sec_top3_lose_lines.append(
            f"{idx_i}. 【{n}({s})】 板块涨跌幅：{p}\n"
            f"   · 板块内领跌 Top 3：{_sector_top3_stocks(s, is_gainer=False)}"
        )
    sec_detail_block = (
        "🔥 领涨 Top 3 板块（及板块内涨幅 Top 3 个股）：\n"
        + ("\n".join(sec_top3_gain_lines) or "未知")
        + "\n\n❄️ 领跌 Top 3 板块（及板块内跌幅 Top 3 个股）：\n"
        + ("\n".join(sec_top3_lose_lines) or "未知")
    )

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
    rsp = q.get("RSP", {})
    ma_trends = data.get("ma_trends") or {}
    ma_lines = "\n".join(
        f"{name} 均线趋势: {ma_trends[sym]}"
        for sym, name in INDICES if ma_trends.get(sym)
    ) or "均线数据暂缺"

    # 市场宽度统计（11大板块涨跌比 + 28只核心权重股涨跌比 + 等权标普RSP对比）
    sec_up = sum(1 for _, _, _, v in sec_rows if v is not None and v > 0)
    sec_down = sum(1 for _, _, _, v in sec_rows if v is not None and v < 0)
    stk_up = sum(1 for _, _, _, _, v in mv if v is not None and v > 0)
    stk_down = sum(1 for _, _, _, _, v in mv if v is not None and v < 0)
    breadth_block = (
        f"11大GICS板块：{sec_up} 涨 {sec_down} 跌 | "
        f"28只核心权重股：{stk_up} 涨 {stk_down} 跌 | "
        f"等权标普(RSP)：{fmt_quote(rsp)}"
    )

    c = data.get("crypto") or {}
    cp = c.get("prices", {})
    cg = c.get("global", {})
    crypto_block = (
        f"BTC: {cp.get('BTC', '未知')} | ETH: {cp.get('ETH', '未知')} | SOL: {cp.get('SOL', '未知')}\n"
        f"BNB: {cp.get('BNB', '未知')} | XRP: {cp.get('XRP', '未知')} | HYPE: {cp.get('HYPE', '未知')}\n"
        f"全球加密市值: {cg.get('total_market_cap_b', '未知')} ({cg.get('market_cap_change_24h', '未知')}) | "
        f"BTC 市占率: {cg.get('btc_dominance', '未知')}（{cg.get('bull_bear_label', '未知')}） | "
        f"恐惧贪婪指数: {c.get('fear_val', '未知')} ({c.get('fear_label', '未知')})"
    )
    return (
        degraded_note
        + f"【收盘交易日】{data['session']}\n"
        f"----------------\n"
        f"【指数收盘】\n{idx}\n"
        f"标普 开{spx.get('open','?')} 高{spx.get('high','?')} 低{spx.get('low','?')} "
        f"52周高{spx.get('yrhiprice','?')} 52周低{spx.get('yrloprice','?')}\n"
        f"----------------\n"
        f"【市场宽度与均线乖离率】\n{breadth_block}\n{ma_lines}\n"
        f"----------------\n"
        f"【宏观指标】\n{mac}\n"
        f"----------------\n"
        f"【加密核心行情（24h）】\n{crypto_block}\n"
        f"----------------\n"
        f"【11大板块表现（按涨跌幅排序）】\n{sec}\n"
        f"----------------\n"
        f"【领涨 Top 3 板块 & 领跌 Top 3 板块（含各板块内 Top 3 个股）】\n{sec_detail_block}\n"
        f"----------------\n"
        f"【全市场焦点个股涨幅前 8】\n{gainers}\n"
        f"----------------\n"
        f"【全市场焦点个股跌幅前 8】\n{losers}\n"
        f"----------------\n\n"
        + "\n".join(data["news_lines"])
    )


def _summary_and_metrics(data: dict) -> tuple:
    q = data["quotes"]
    spx, ixic = q.get(".SPX", {}), q.get(".IXIC", {})
    vix = q.get(".VIX", {})
    btc = (data.get("crypto") or {}).get("prices", {}).get("BTC", "?")
    summary = (f"收盘{data['session']} 标普{spx.get('last','?')}({spx.get('change_pct','?')}) "
               f"纳指{ixic.get('last','?')}({ixic.get('change_pct','?')}) "
               f"VIX{vix.get('last','?')} BTC {btc} → 抓取{len(data['entries'])}条 → 保留{data['news_count']}条")
    metrics = {
        "session": data["session"],
        "spx": spx.get("last"), "spx_pct": spx.get("change_pct"),
        "ixic_pct": ixic.get("change_pct"), "vix": vix.get("last"),
        "btc": btc,
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
