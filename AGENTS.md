# US Stock Bot — AI 操作手册

> 面向任意 AI 工具的完整上下文。Claude Code 专属指令见 [`CLAUDE.md`](./CLAUDE.md)。

---

## 这个 bot 做什么

每天推送两条飞书卡片消息，内容是**最近一个已收盘的美股交易日**：

- **消息①《美股收盘》** — 指数 / 宏观指标 / 板块轮动 / 个股异动 / 核心叙事 / 分析师观点
- **消息②《美股要闻》** — 30秒速览 + 按 3/4/5 分评级排序的新闻条目

写稿由 Claude 完成，本仓库的 `stock_report.py` 只负责抓取（`fetch`）与发送（`send`），
全程零第三方大模型 API、零 token 成本。

---

## 时间口径（本 bot 特有，务必理解）

播报的是**最近一个已收盘的美股交易日**，不是"今天"：

| 北京时间推送日 | 覆盖的美股场次 |
|---|---|
| 周二 ～ 周六 10:06 | 前一交易日收盘（周一～周五） |
| 周日、周一 | **无新场次**，脚本输出 `SKIP_NO_NEW_SESSION` 跳过 |

判定方式：从标普报价的 `last_time` 取收盘日，与 `logs/.last_session` 比对。
相同则说明这个场次已经播过（周末、美股假期都会走到这个分支），直接跳过而不是
把上周五的行情当成新内容重播一次。

---

## 每日时间线

```
10:00  Claude routine（唯一写稿入口，实际 10:06 触发）
         └─ claude_report.sh fetch → Claude 写两稿 → claude_report.sh send → run.log [OK/FAIL]

11:00  launchd ──▶ health_check.sh
                        │
                  [OK] ──┼── .ok_streak +1（连续 3 次后清理 changelog）
                        │
                  [NO_NEWS] ── 当天确实无新场次/无新闻，正常终态，不补跑
                        │
                  [无记录] ── claude_catchup.sh 无头补跑
                        │
                  [FAIL] ── changelog 新增条目 → auto_repair.sh（前台运行）
                                  ├─ Level 1：等 30s 重跑 send
                                  ├─ Level 2：claude CLI 诊断修复 → 重跑 send
                                  └─ 最终兜底：claude_catchup.sh 无头补跑
```

主 plist（`com.shirley.us-stock-bot`）**不承担调度**，ProgramArguments 是
`/usr/bin/true`，仅作为密钥与代理的环境变量配置源。

---

## 数据源

### 行情：CNBC quote API
免费、无需 API key、一次调用取回全部标的。

| 类别 | 标的 |
|---|---|
| 指数 | `.SPX` `.IXIC` `.DJI` `.RUT` |
| 宏观 | `.VIX` `US10Y` `.DXY` |
| 板块 | 11 个 SPDR 行业 ETF（XLK/XLC/XLY/XLP/XLE/XLF/XLV/XLI/XLB/XLRE/XLU） |
| 个股 | 28 支大市值 + 高关注度标的（见 `WATCHLIST`） |

标普缺失即视为行情抓取失败，输出 `SKIP_NO_QUOTES` 并跳过（不会发一份没有指数的稿）。

> Yahoo Finance 的 chart API 曾被评估，实测持续返回 429，故改用 CNBC。

### 新闻：RSS × 3

| 源 | 额度 | 定位 |
|---|---|---|
| CNBC 财经头条 | 10 | 宏观 + 市场 |
| CNBC 盈利/财报 | 8 | 财报专线 |
| Yahoo Finance | 8 | 个股 + 分析 |

**选源标准：RSS 摘要或正文至少有一个能抓到。** 已排除：
- `investing.com` —— 标题很好但摘要为空且正文被反爬拦截（实测 6 条全零内容）
- Seeking Alpha —— 付费墙
- Business Insider 市场源 —— 充斥律所集体诉讼通稿

---

## 过滤链路

```
抓取 → URL 去重（单次运行内）
     → 跨天已播去重（logs/sent_urls.json，保留 7 天）
     → 48h 时间窗（比 AI bot 的 24h 宽：美股是隔夜市场，收盘解读常在次日发出）
     → 内容闸门（正文与摘要都 <50 字的条目直接丢弃）
```

内容闸门是通用保护，不针对特定源：任何源被反爬拦截变成"只剩标题"时都会被拦下，
且同步扣减该源的 kept 计数，让零产监测统计的是**真正可用的产出**。

---

## 与 Crypto Daily Bot 的分工

两个 bot 会碰到同一家公司的新闻，分工写在 `prompt_news.md` 里：

- **本 bot 管股权角度**：财报、股价、估值、评级、并购、管理层动作
- **Crypto bot 管资产角度**：买卖了多少 BTC、对加密资产供需的影响

例：MicroStrategy 增持比特币 → Crypto bot；MicroStrategy 财报与股价 → 本 bot。

---

## 文件结构

| 文件 | 作用 | 改动频率 |
|---|---|---|
| `stock_report.py` | 主脚本：`--mode fetch` / `send` | 低 |
| `claude_report.sh` | fetch/send 封装（从 plist 加载环境变量） | 极低 |
| `prompt_market.md` | 消息①写稿规范（唯一权威源） | 中 |
| `prompt_news.md` | 消息②写稿规范（唯一权威源） | 中 |
| `health_check.sh` | 体检（11:00 由 launchd 触发） | 低 |
| `auto_repair.sh` | 薄包装 → `../shared/auto_repair_base.sh` | 极低 |
| `claude_catchup.sh` | 薄包装 → `../shared/headless_catchup_base.sh` | 极低 |
| `logs/report_market.txt` | 当日消息①稿件 | 每日 |
| `logs/report_news.txt` | 当日消息②稿件 | 每日 |
| `logs/fetch_meta.json` | fetch 边车：日志摘要 + 指标 | 每日 |
| `logs/run.log` | 单行摘要日志（人类可读） | 每日 |
| `logs/run.jsonl` | 结构化指标（含分源 fetched/kept） | 每日 |
| `logs/sent_urls.json` | 跨天去重档案（保留 7 天） | 每日 |
| `logs/.zero_streak.json` | 各源连续零产天数（fetch 单点写入） | 每日 |
| `logs/.last_session` | 已播报过的收盘交易日 | 每日 |
| `com.shirley.us-stock-bot-health.plist` | health_check launchd 配置（11:00 触发） | 极低 |

---

## 分源零产监控与源淘汰

- 每次 fetch 把各源 `{fetched, kept}` 写入 `rss_source_stats`；`rss_zero_sources`
  列出**过滤后一条都没剩**的源
- `logs/.zero_streak.json` 累计连续零产天数，**由 fetch 单点写入**，health_check
  只读不写（两处各加一次会让天数翻倍）
- 连续 **3 天**零产 → fetch 的 stdout 输出 `=== SOURCE_ALERT ===` 块，routine 会在
  汇报里单列「RSS 源健康」，同时写入 metrics 的 `rss_stale_sources`

---

## 修改禁区

| 禁止操作 | 原因 |
|---|---|
| 删除 `save_pending()` 调用 | 发送失败时稿件会永久丢失，无法人工恢复 |
| 在 prompt 之外定义稿件格式 | `prompt_market.md` / `prompt_news.md` 是唯一权威源 |
| 让 fetch 阶段写 `.last_session` | 只有 send 成功后才算"播报过"，提前写会导致漏播 |
| 在 health_check 里累加 `.zero_streak.json` | 该文件由 fetch 单点维护，两处写会让天数翻倍 |
| 用 Markdown 语法写稿 | 稿件走 `<b>`/`<a href>` 的 HTML 中间格式，`**` 只会被原样当成正文发出去 |
