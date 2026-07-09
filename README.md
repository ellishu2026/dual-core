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

## 版本记录

- **v1.0.1**：初版。双标的（NVDA/LLY）、EMA 八线、进场三档/止损/止盈状态机、单页浅色仪表盘、GitHub Actions 每日自动刷新。

后续每次迭代版本号 +1（v1.0.2、v1.0.3…），修改 `fetch_data.py` 顶部的 `VERSION` 常量并同步更新本文件版本记录。

## 说明

- 数据源：yfinance，免费但偶有限流/延迟，非专业行情源，仅供个人研究使用。
- `armed` 重新武装规则、止盈后的状态重置等属于我在把文字规则转成状态机时补的实现细节，不是你原始描述的一部分——过一遍看看是否符合你的预期，需要调整随时说。
- 不构成投资建议。
