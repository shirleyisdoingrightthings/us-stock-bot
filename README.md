# US Stock & Crypto Daily Bot

Automated morning briefing covering US equity market close, macro indicators, 24h core crypto metrics, and financial news, delivered to Feishu and Notion (Tue–Sat at 10:00 CST).

## Workflow

```text
CNBC Quotes (Indices / Macro / 11 Sectors / RSP / 28 Stocks / Sector Leaders) + CoinGecko / F&G + Financial RSS
  │
  ▼
1. Fetch Market & News Data (stock_report.py --mode fetch)
  ├─ Check trading session against .last_session (auto-skip weekends & US market holidays)
  ├─ Pull closing quotes for S&P 500, Nasdaq, Dow, Russell 2000, VIX, 10Y Treasury (with bp delta), DXY, sectors & movers
  ├─ Fallback to FMP + Twelve Data (incl. RSP & sector members in FMP whitelist) if CNBC is unavailable
  ├─ Pull 24h prices & market cap for BTC, ETH, SOL, BNB, XRP, HYPE + Fear & Greed Index
  └─ Fetch & extract full-text financial news from CNBC, MarketWatch, and Nasdaq (deduplicated via sent_urls.json)
  │
  ▼
2. Draft Two Reports (Scheduled Sidecar at 10:00 CST, Tue–Sat)
  ├─ Message 1 (Market Dashboard): Follow prompt_market.md to write logs/report_market.txt
  └─ Message 2 (Market News): Follow prompt_news.md to write logs/report_news.txt (up to 12 items)
  │
  ▼
3. Sanitize & Deliver (stock_report.py --mode send)
  ├─ Sanitize HTML & convert both reports to Feishu card Markdown
  ├─ Push Message 1 and Message 2 sequentially to Feishu webhook
  └─ Archive combined report to Notion, record .last_session, update market_memory.json & sent_urls.json
```

## Operation

1. **Fetch Materials**: Run `bash claude_report.sh fetch` to gather market quotes, crypto metrics, and financial news into `logs/last_context.txt`.
2. **Generate Drafts**: Read `logs/last_context.txt` and generate `logs/report_market.txt` (`prompt_market.md`) and `logs/report_news.txt` (`prompt_news.md`) via `write_file`.
3. **Send to Feishu & Notion**: Run `bash claude_report.sh send` to push both messages and archive to Notion.
