# Bitfinex 放貸機器人 — 設計文件

> 目標：穩定高年化的 Bitfinex USD 放貸機器人，24hr 跑在伺服器（Zeabur），
> Telegram 即時推播，GitHub Pages 網頁 Dashboard，Supabase 存歷史資料。

## 一、整體架構

```
┌─────────────────────────── Zeabur (Docker) ───────────────────────────┐
│  lendbot (Python)                                                     │
│  ├─ 每 5 分鐘一個循環：抓市場 → 策略決策 → 撤單/掛單 → 寫入 DB          │
│  └─ Telegram 執行緒：推播通知 + 回應指令 (/status /earnings ...)        │
└──────────┬──────────────────────┬─────────────────────┬───────────────┘
           │ REST (HMAC 簽名)      │ service_role key     │ Bot API
           ▼                      ▼                     ▼
   Bitfinex API            Supabase (Postgres)      Telegram
   api.bitfinex.com        歷史快照/收益/狀態
   api-pub.bitfinex.com           ▲
           ▲                      │ RPC (anon key + 私人 token 驗證)
           │ 公開 API (CORS OK)    │
┌──────────┴──────────────────────┴───────────────┐
│  web/ — 靜態網頁 (GitHub Pages)                  │
│  市場數據：瀏覽器直接打 Bitfinex 公開 API（即時）  │
│  個人數據：Supabase RPC（需輸入 token 才看得到）  │
└─────────────────────────────────────────────────┘
```

技術選型與理由：

| 元件 | 選擇 | 理由 |
|---|---|---|
| 機器人 | Python 3.11，只用 `requests` + `PyYAML` | 依賴最少、好維護、好除錯 |
| 排程 | 程式內 while 迴圈（非 cron） | 狀態（已知訂單、成交偵測）留在記憶體，簡單可靠 |
| 資料庫 | Supabase（Postgres + PostgREST） | 免費額度夠用、網頁可直接用 anon key 讀 |
| 網頁 | 純 HTML/JS + Chart.js CDN，零建置 | GitHub Pages 直接放，不需 Node、不需 CI 編譯 |
| 部署 | Dockerfile → Zeabur | 一個 Dockerfile 哪裡都能跑 |

## 二、策略設計（核心）

參考 FULY.AI / AltInvest / 開源 BitfinexLendingBot 等做法後的改良版：

### 2.1 市場分析（每循環）
- `ticker fUSD`：取 FRR（僅做參考下限，**不用 FRR 掛單**——它落後市場）
- `book fUSD P0`：掛單簿，算「前 N 萬美元深度處的利率」＝要排在隊伍前面需要的利率
- `trades fUSD hist`：近 120 筆成交，算 **IQM（四分位距內平均）** 當錨點利率
  （比平均值抗極端值，比中位數平滑），另取近 15 分鐘最高成交利率做 spike 偵測

### 2.2 錨點利率
```
anchor = max(trade_IQM, book 深度利率, 設定的最低利率底線)
```

### 2.3 階梯掛單（Ladder）
資金拆成多檔（config 可調），預設：

| 檔位 | 資金占比 | 利率 | 目的 |
|---|---|---|---|
| 1 | 50% | anchor × 1.00 | 快速放出去，減少閒置 |
| 2 | 30% | anchor × 1.15 | 吃小波動 |
| 3 | 20% | anchor × 1.45 | 等利率飆漲（spike） |

偵測到 spike（近 15 分鐘最高成交 > IQM × spike 倍率）時，
第 3 檔改掛在 `近期最高成交 × 0.95`，搶飆漲單。

### 2.4 天期選擇（高利鎖長天期）
依該檔利率的年化（複利換算）決定天期，預設門檻：

| 年化 | 天期 |
|---|---|
| < 8% | 2 天（快速周轉） |
| ≥ 8% | 7 天 |
| ≥ 12% | 30 天 |
| ≥ 18% | 120 天（鎖住高利） |

### 2.5 重掛機制
掛單超過 `stale_minutes`（預設 10 分鐘）未成交，且利率高於目前錨點
`cancel_threshold`（預設 5%）以上 → 撤單，下一循環以新利率重掛。
（避免掛太高一直借不出去 = 閒置成本）

### 2.6 風控 / 限制
- Bitfinex 最小掛單 150 USD，不足的零頭留到下一循環
- `min_rate` 底線：低於此年化寧可不掛（config 可調）
- DRY_RUN 模式：完整跑策略但不真正下單（本機測試用）
- API 錯誤重試 + 失敗推播 Telegram

## 三、資料庫設計（Supabase）

| 表 | 用途 |
|---|---|
| `market_snapshots` | 每循環的市場數據（FRR、IQM、最佳掛單利率、spike） |
| `actions_log` | 機器人每個動作（掛單/撤單/成交） |
| `credits_snapshots` | 放貸中部位快照（總額、加權利率、估年化） |
| `earnings` | 每日利息收益（從 Bitfinex ledger category 28 同步） |
| `bot_status` | 單列：機器人最新狀態（網頁總覽用） |
| `app_settings` | 存 dashboard 私人 token 的雜湊 |

安全模型：
- 機器人用 `service_role` key 寫入（不受 RLS 限制，只放在伺服器環境變數）
- 所有表開 RLS 且 **anon 不可直接讀**
- 網頁透過 `dashboard_data(p_token)` RPC（SECURITY DEFINER）讀取，
  token 不對就回 null → anon key 公開在網頁也不會洩漏個人數據

## 四、Telegram 設計

推播（主動）：
- 放貸成交（金額/利率/天期/年化）
- 利率 spike 警報
- 每日收益總結（時間可設定）
- 錯誤警報

指令（被動）：
- `/status` 餘額、放貸中、掛單中、估年化
- `/rates` 目前市場利率
- `/earnings` 今日 / 7日 / 30日收益
- `/pause` `/resume` 暫停／恢復掛單
- `/help`

## 五、網頁 Dashboard（GitHub Pages）

- **市場區**（免登入）：瀏覽器直接打 Bitfinex 公開 API —— FRR、掛單簿深度、
  近期成交利率走勢圖、即時年化換算
- **個人區**（輸入 token 後解鎖，存 localStorage）：總資產、放貸中金額、
  加權利率、估年化、30 日收益長條圖、目前掛單、機器人心跳狀態
- 手機 RWD，深色主題

## 六、實作步驟（依此順序進行）

- [x] **Step 0** 策略研究
- [ ] **Step 1** 專案初始化：目錄結構、git、config、.env 範本
- [ ] **Step 2** Bitfinex API 客戶端（公開 + HMAC 私有），可獨立測試
- [ ] **Step 3** 策略引擎（純函式，可單元測試）：市場分析、階梯、天期、重掛判斷
- [ ] **Step 4** 核心循環 engine：排程、下單、撤單、成交偵測、DRY_RUN
- [ ] **Step 5** Supabase：schema.sql + 寫入層
- [ ] **Step 6** Telegram：推播 + 指令執行緒
- [ ] **Step 7** 網頁 Dashboard + GitHub Pages workflow
- [ ] **Step 8** 單元測試 + 本機 DRY_RUN 實測（接真實公開市場數據）
- [ ] **Step 9** 部署文件：Dockerfile、Zeabur 設定、上線檢查清單

## 七、上線流程（規劃）

1. 本機 Claude Code：DRY_RUN 跑 1-2 天觀察決策合理性
2. 本機接真實 API key、小額（如 150-500 USD）實測
3. 推上 GitHub → Zeabur 連 repo 部署（環境變數設定 key）
4. 網頁 GitHub Pages 上線
5. 觀察一週後放大資金
