# US Stock Bot

每天早上由 Claude 自动写稿：拉取四大指数、VIX、美债与美元、11 个 GICS 板块、28 支重点个股的收盘数据，加上三家财经媒体的当日要闻，生成一份《美股收盘》+《美股要闻》，两条消息推送到飞书。

内容是**最近一个已收盘的美股交易日**——北京时间早上推送时，美股昨夜的行情已完全落定，不需要熬夜看盘。

---

## 核心特性

**数据来源 — 行情、宏观、板块、新闻，四维覆盖**

| 类型 | 数据源 |
|------|------|
| 指数 | 标普500 / 纳斯达克 / 道琼斯 / 罗素2000（CNBC） |
| 宏观 | VIX 恐慌指数 / 10年期美债收益率 / 美元指数 |
| 板块 | 11 个 GICS 板块（SPDR 行业 ETF 代理），按涨跌幅排序 |
| 个股 | 28 支大市值 + 高关注度标的，自动挑出两端异动 |
| 新闻 | CNBC 财经头条 · CNBC 盈利/财报 · Yahoo Finance |

行情走 CNBC 免费 quote 接口，一次调用取回全部 46 个标的，无需 API key、零成本。

**时间口径 — 只播真正的新场次**

- 北京时间周二～周六推送前一交易日收盘
- 周日、周一（美股无新场次）自动跳过，不把上周五的行情当新内容重播
- 判定靠标普报价里的收盘日与 `logs/.last_session` 比对，美股假期同样能正确识别

**内容处理 — 过滤后才算数**

- **跨天去重**：`send` 成功后归档稿件里实际用到的链接（保留 7 天），下次抓取自动排除
- **48 小时时间窗**：比 AI 日报的 24h 更宽，因为收盘解读文章常在次日发出
- **内容闸门**：正文与摘要都抓不到的条目直接丢弃，不让 Claude 对着光秃秃的标题硬写
- **价值评分**（3 / 4 / 5 分制）：美联储决议与万亿级财报占 5 分，评级调整占 3 分

**报告结构 — 30 秒看完，也能深读**

- **速览**：固定 5 条，每条带星级 + 至少一个具体数字，与正文前 5 条一一对应
- **收盘仪表盘**：指数 + 宏观三件套 + 板块轮动 + 个股异动，每条都给资金逻辑
- **超长自动分页**：超过飞书 webhook 单条 20KB 请求体上限时按段落切分，每条顶部标 `（n/N）` 页码

**稳定性 — 出了问题自己修**

- 每日体检：11:00 检查当天是否成功出稿，异常自动记 changelog 并触发自愈
- 两级自愈：瞬时故障等 30 秒重跑；持续故障调用 Claude CLI 诊断修复
- 无头补跑：当天根本没出稿或自愈无效时，claude CLI 自动完整重走一遍流程
- 代理端口自愈：配置端口不通时自动探测候选端口，换代理软件不再静默停摆
- 源淘汰监测：统计每个 RSS 源"过滤后还剩几条"，连续 3 天零产即告警建议移除

---

## 系统架构与工作流

```
【数据采集层】
CNBC quote API（指数 / VIX / 美债 / 美元 / 11 板块 / 28 个股）──┐
RSS × 3（CNBC 头条 / CNBC 财报 / Yahoo Finance）               ├──▶ stock_report.py --mode fetch
                                                               ┘     │  build_news_context()
                                                                     │  ├─ URL 去重 + 跨天去重
                                                                     │  ├─ 48h 时间窗
                                                                     │  ├─ 内容闸门（丢弃无正文条目）
                                                                     │  └─ 并发 best-effort 抓正文全文
                                                                     ▼
                                                        场次检查（.last_session）
                                                        无新收盘 → SKIP_NO_NEW_SESSION
                                                                     │
                                                                     ▼
                                                        Claude 按 prompt 写两稿
                                                        ├─ prompt_market.md → logs/report_market.txt（消息①）
                                                        └─ prompt_news.md   → logs/report_news.txt（消息②）
                                                                     │
                                                                     ▼
                                                        stock_report.py --mode send
                                                                     │  清洗 HTML，依次发送
                                                                     │  超 4096 → 分页 + (n/N) 页码
                                                                     │  发送成功 → 归档链接 + 记录场次
                                                                     ▼
                                                          飞书（2 条卡片消息）

【自动化调度】
10:00  Claude 定时任务（唯一写稿入口）
11:00  launchd ──▶ health_check.sh（体检；详见 AGENTS.md）
```

> 写稿由本地 Claude 定时任务完成。`stock_report.py` 只负责抓取与发送，全程零第三方大模型 API、零 token 成本。

---

## 与 Crypto Daily Bot 的分工

两个 bot 会碰到同一家公司的新闻（Coinbase、MicroStrategy、矿业股等），分工如下：

- **本 bot 管股权角度**：财报、股价、估值、评级、并购、管理层动作
- **Crypto bot 管资产角度**：买卖了多少 BTC、对加密资产供需的影响

同一件事从两个角度看本来就是两条不同的新闻。规则写在各自的 `prompt_news.md` 里。

---

## 环境变量

所有变量写在**唯一权威配置源** `~/Library/LaunchAgents/com.shirley.us-stock-bot.plist` 中，
`claude_report.sh` 从这里读取并自动注入。仓库内只保留 `.plist.example` 模板（不含密钥）。

| 变量 | 说明 |
|------|------|
| `FEISHU_WEBHOOK` | 飞书自定义机器人 webhook 地址 |
| `FEISHU_SECRET` | 机器人签名密钥（未开签名校验则留空） |
| `HTTPS_PROXY` / `HTTP_PROXY` | 本地代理地址（127.0.0.1:YOUR_PORT） |

> 该 plist 不承担任何调度职责，仅作为环境变量配置源。

---

## 快速开始

**手动抓取 / 发送（测试）**
```bash
cd ~/Desktop/bots/us\ stock\ bot
bash claude_report.sh fetch     # 抓取行情+新闻，把写稿素材打到 stdout
# （由 Claude 依两个 prompt 写稿并存入 logs/report_market.txt 与 report_news.txt）
bash claude_report.sh send      # 读取两稿，清洗 HTML 后推送飞书
```

**验证调度状态**
```bash
launchctl list | grep shirley
tail -5 logs/run.log
```

---

## 依赖安装

```bash
pip3 install -r requirements.txt
```

共享工具库位于 `../shared/bot_utils.py`，由三个 bot 共用。
