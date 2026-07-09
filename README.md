# 双核 Dual Core — NVDA / LLY EMA 信号台

v1.0.1 · 单页网页 App，仅观察/交易 NVDA 与 LLY 两只标的，唯一指标为 EMA(5/9/20/60/120/180/195/225)。

## 交易逻辑

- **进场**：价 ≤ EMA180 → 观察；价 ≤ EMA195 → 开仓 30%；价 ≤ EMA225 → 加仓至 75%（仓位只升不降，除非触发止损/止盈）
- **止损**：价 ≤ 0.9×EMA225 → 清仓
- **止盈**（仅在有仓位时生效）：先出现多头排列 EMA5>9>20>60，排列确认后，价 < EMA5 → 减仓一半；价 < EMA9 → 清仓
- **止损后重新武装**：为避免在深跌中每天"止损→再进场"来回抽打（whipsaw），止损清仓后价格需重新回升至 EMA225 之上，进场阶梯才会重新生效。这条是我在实现时加的安全阀，原始规则未覆盖这个边界情况。
- **状态显示**：只要当前没有仓位（未触发进场/止损/止盈的持仓状态），一律显示"观察"，不再区分"观察"和"空仓"两种状态。

## 本地运行

```bash
cd dual-core
pip install -r requirements.txt
python3 fetch_data.py
```

会生成 `data/signals.json`，双击 `index.html` 或起个本地服务器都能看：

```bash
python3 -m http.server 8000
# 浏览器打开 http://localhost:8000
```

## 部署到 Vercel（或任何静态托管）

`index.html` 里的数据请求走的是 `https://raw.githubusercontent.com/ellishu2026/dual-core/main/data/signals.json`，不是相对路径。这样浏览器每次打开页面都是直接问 GitHub 要最新数据，跟 Vercel 有没有重新部署完全无关——Vercel 只需要托管一次这个静态壳子，`.github/workflows/daily_update.yml` 每天往同一个仓库 commit 新的 `data/signals.json`，页面自动就能看到最新数据，不用等 Vercel 重新部署，也不需要拆成两个仓库。

```bash
cd dual-core
git init
git add .
git commit -m "v1.0.1 dual-core init"
git branch -M main
git remote add origin https://github.com/ellishu2026/dual-core.git
git push -u origin main
```

去 vercel.com Import 这个仓库：Framework Preset 选 **Other**，Build Command 留空，Output Directory 填 `.`。保存后拿到访问链接即可，之后不用再管 Vercel，它只需成功部署这一次。

如果仓库名不是 `dual-core`，或者不是挂在 `ellishu2026` 账号下，记得同步改 `index.html` 里的 `DATA_URL`。

## 目录结构

```
dual-core/
├── index.html                       # 唯一页面，浅色简洁风格
├── logo.svg                         # App 图标（NVDA绿 / LLY红 拼合，原创设计非公司商标）
├── fetch_data.py                    # 数据引擎：拉取行情 + 计算EMA + 状态机 + 写 signals.json
├── requirements.txt
├── data/signals.json                # 引擎输出，前端读取的唯一数据源
└── .github/workflows/daily_update.yml
```

## Version Log

- **v1.0.1**: Initial release. Two tickers (NVDA/LLY), 8-line EMA, entry ladder/stop-loss/take-profit state machine, single-page light dashboard, GitHub Actions daily refresh.
- **v1.0.2**: All-English UI. Rules condensed into 4 pills (Entry/Stop Loss/Take Profit/Parameters). Cards switched to full-width stacked layout. Added a Day/Week/Month EMA line chart per ticker (same daily-computed EMAs, just sampled at different granularity — no separate weekly/monthly EMA calculation). Re-arm threshold after stop-loss changed from EMA180 to EMA225. Flat "no position" state simplified to always show "Watching".
- **v1.0.3**: Recent Actions log now keeps every state-change event within the trailing 1 year (was capped at the last 5), with a scrollable list on the frontend so a busy year doesn't stretch the page.
- **v1.0.4**: Take-profit now requires BOTH bullish stack (5>9>20>60) AND a new all-time high since entry before the EMA5/EMA9 exit ladder arms. Data pull switched from 5y to full available history (`period="max"`) so "all-time high" is a true ATH, not just a 5-year-window high.
- **v1.0.5**: Corrected the take-profit high check from all-time high to a trailing 1-year high (252 trading days) — much more attainable for NVDA/LLY and closer to the intended signal. Data pull reverted to 5y (full history is no longer needed).
- **v1.0.6**: Rule pills now sit in a proper 2×2 grid instead of wrapping unevenly. EMA chart lines recolored: EMA5/9/180/195/225 (the lines the strategy actually acts on) get distinct colors and a thicker stroke; EMA20/60/120 stay thin light-gray as background context. Recent Actions log moved beside the chart (not below it) to cut vertical scroll on the page.
- **v1.0.7**: Take-profit's high check now requires the price to clear 1-year-high × 1.09 (not just any new high) — see note below on why this needs a real breakout, not a slow grind, to fire. Recent Actions column narrowed ~1/3 with smaller type. Chart x-axis now shows 5 evenly spaced M/D-formatted dates. Hovering the chart shows a readout (date, close, EMA5/9/180/195/225) for that point, with a guide line + dot marker. Added a take-profit trigger price display under the stop level.

  **Note on the 1.09× rule**: because it compares today's close to *yesterday's* trailing 1-year high, a slow smooth rally can "outrun" its own trailing high one small step at a time and never actually clear it by 9% in a single day. The rule reliably fires on a genuine sharp breakout (a gap or a fast multi-day surge) above a high that's been standing for a while — which matches the intent of confirming a decisive breakout — but won't fire on a gradual grind past the old high. Flagging this since it's a real behavior, not a bug, and worth knowing before relying on it.
- **v1.0.8**: Fixed the above — the 1-year-high reference for the take-profit trigger now freezes as of 21 trading days ago (`TP_HIGH_EXCLUDE_DAYS`) instead of chasing yesterday's close. This gives a smooth rally 3 weeks of real headroom to clear the 9% bar, so it's no longer gap/breakout-only. Verified against a synthetic smooth 1%/day rally (no gaps) — now triggers correctly.
- **v1.0.9**: Frontend-only. Stop/Take-profit moved from below the chart up next to the status badge, simplified to "Stop: $X" / "Take-profit: $X" (dropped the explanatory suffix text). Hover readout row switched to a tighter flex layout with smaller font and no thousands-comma formatting so all 7 values (date, close, 5 key EMAs) fit on one line without horizontal scrolling.
- **v1.0.10**: Fixed a real bug in the v1.0.8 "exclude last 21 days" window math. It was computing `rolling(252).max().shift(21)`, which reaches back 272 trading days (252+21), overstating the reference high with extra older history. Corrected to `shift(21).rolling(231).max()` (231 = 252-21), which precisely spans "252 trading days ago" to "21 trading days ago." Take-profit trigger prices will read noticeably lower after this fix — caught via a manual spot-check against LLY's actual data.
- **v1.0.11**: Added SOXL as a third ticker, watch-only — runs the exact same entry/stop-loss/take-profit engine as NVDA/LLY, just for reference rather than as a firm trading commitment. Note: the app is still named "Dual Core" / 双核 even though it now tracks three tickers; not renamed since that's cosmetic and the person may want to revert to two.
- **v1.0.12**: Switched the data source back from jsDelivr to `raw.githubusercontent.com`. jsDelivr's `@main` branch reference caches the "what commit does main point to" resolution separately from file content — a successful manual purge cleared the file cache but the branch pointer stayed stale for hours. raw.githubusercontent.com is close to real-time and the earlier 429 we hit was from concentrated rapid-fire testing traffic, not a problem for normal daily-checking usage. Also dropped the now-pointless jsDelivr purge step from the workflow.
- **v1.1.0**: Version numbering switched to standard semver rollover from here on — patch digit resets to 0 and minor increments at 10 (1.0.9 → 1.1.0 → 1.1.1…), not 1.0.10/1.0.11/1.0.12 as before. Also: hover readout now shows all 8 EMAs (was missing EMA20/60/120), and the Entry pill now shows the actual position percentages (30%/75%).
- **v1.1.1**: Chart is now horizontally scrollable on narrow (mobile) widths instead of shrinking to fit — below 520px the SVG renders at a fixed 600px so axis labels stay legible, and the chart container scrolls (swipe) to see the rest. Above that width it still scales to fill the card as before.
- **v1.1.2**: Added proper "Add to Home Screen" icon support. `logo.svg` alone doesn't work as an iOS home-screen icon (Apple requires PNG), so rasterized it to `icons/icon-180.png` (apple-touch-icon), `icon-192.png`/`icon-512.png` (Android/`manifest.json`), plus a `manifest.json` and the standalone-mode meta tags for both platforms. Re-add the site to your home screen after this deploy to pick up the new icon — iOS/Android cache the icon at add-time and won't auto-update an existing shortcut.

## 说明

- 数据源：yfinance，免费但偶有限流/延迟，非专业行情源，仅供个人研究使用。
- `armed` 重新武装规则、止盈后的状态重置等属于我在把文字规则转成状态机时补的实现细节，不是你原始描述的一部分——过一遍看看是否符合你的预期，需要调整随时说。
- 不构成投资建议。
