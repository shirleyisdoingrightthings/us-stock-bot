# US Stock Bot

每天早上由 Claude 写稿：拉四大指数、VIX、美债与美元、11 个 GICS 板块、28 支重点个股的
收盘数据，加上三家财经媒体的当日要闻，生成《美股收盘》+《美股要闻》推送到飞书。
全程零第三方大模型 API——脚本只负责抓取与发送。

行情主源是 CNBC 的批量接口；2026-09-04 起加了 Twelve Data 作为兜底，但它免费档只覆盖
11 个板块 ETF、拿不到指数。所以 CNBC 挂掉时会**降级出稿**——发一份「有板块、无指数」的
日报，并在稿子开头明确告知读者本期指数数据缺失，而不是整份不发。细节见 `AGENTS.md`。

播的是最近一个已收盘的美股交易日，不是"今天"。北京时间早上推送时美股昨夜行情已完全
落定，不需要熬夜看盘。

## 数据来源

| 类型 | 数据源 |
|---|---|
| 指数 | 标普500 / 纳斯达克 / 道琼斯 / 罗素2000（CNBC） |
| 宏观 | VIX / 10 年期美债收益率 / 美元指数 |
| 板块 | 11 个 GICS 板块（SPDR 行业 ETF 代理），按涨跌幅排序 |
| 个股 | 28 支大市值 + 高关注度标的，自动挑两端异动 |
| 新闻 | CNBC 财经头条 · CNBC 盈利/财报 · MarketWatch 市场 · Fortune |

行情走 CNBC 免费 quote 接口，一次调用取回全部 46 个标的，无需 API key。

## 主要行为

- 只播真正的新场次：周日、周一（美股无新场次）自动跳过，不把上周五的行情当新内容重播。
  判定靠标普报价里的收盘日与 `logs/.last_session` 比对，美股假期同样能正确识别。
- 48 小时时间窗：比 AI 日报的 24h 更宽，因为收盘解读文章常在次日发出。
- 内容闸门：正文与摘要都抓不到的条目直接丢弃，不让 Claude 对着光秃秃的标题硬写。
- 跨天去重：`send` 成功后归档稿件里用到的链接（留 7 天），下次抓取排除。
- 分页：超过飞书 webhook 单条 20KB 上限时按段落切分并标 `(n/N)` 页码。

## 与 Crypto Daily Bot 的分工

两个 bot 会碰到同一家公司的新闻（Coinbase、MicroStrategy、矿业股等）：

- 本 bot 管股权角度：财报、股价、估值、评级、并购、管理层动作
- Crypto bot 管资产角度：买卖了多少 BTC、对加密资产供需的影响

同一件事从两个角度看本来就是两条新闻。规则写在各自的 `prompt_news.md` 里。

## 工作流

```
CNBC quote API（指数/VIX/美债/美元/11 板块/28 个股）─┐
RSS × 4（CNBC 头条 / 财报 / MarketWatch / Fortune） ├─▶ stock_report.py --mode fetch
                                                    ┘     │  ├─ URL 去重 + 跨天去重
                                                          │  ├─ 48h 时间窗
                                                          │  ├─ 内容闸门（丢弃无正文条目）
                                                          │  └─ 并发抓正文
                                                          ▼
                                          场次检查（.last_session）
                                          无新收盘 → SKIP_NO_NEW_SESSION
                                                          ▼
                                    Claude 写两稿 → logs/report_market.txt
                                                  → logs/report_news.txt
                                                          ▼
                                    stock_report.py --mode send
                                                          │  清洗 HTML，依次发送
                                                          │  超长分页 + (n/N)
                                                          │  成功 → 归档链接 + 记录场次
                                                          ▼
                                                   飞书「美股日报」

10:00  Claude 定时任务（唯一写稿入口）
11:00  launchd → health_check.sh（体检，详见 AGENTS.md）
```

## 环境变量

写在 `~/Library/LaunchAgents/com.shirley.us-stock-bot.plist`，`claude_report.sh` 每次
运行时直接读文件。仓库里只留 `.plist.example` 模板。该 plist 不承担调度，只作为配置源。

| 变量 | 说明 |
|---|---|
| `FEISHU_WEBHOOK` | 飞书机器人 webhook |
| `FEISHU_SECRET` | 签名密钥，未开签名校验则留空 |
| `HTTPS_PROXY` / `HTTP_PROXY` | 本地代理 |

## 用法

```bash
bash claude_report.sh fetch     # 抓行情+新闻，把素材打到 stdout
bash claude_report.sh send      # 读两稿，清洗后依次推送
```

调试：

```bash
tail -5 logs/run.log                          # 最近运行状态
tail -3 logs/run.jsonl | python3 -m json.tool # 结构化指标
bash health_check.sh                          # 手动体检
```

依赖：`pip3 install -r requirements.txt`。共享层在 `../shared/`。

详细操作规范见 [`AGENTS.md`](./AGENTS.md)；换机与排障见 [TROUBLESHOOTING.md](https://github.com/shirleyisdoingrightthings/bot-ops/blob/main/TROUBLESHOOTING.md)。
