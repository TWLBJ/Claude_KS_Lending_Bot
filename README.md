# Bitfinex 放貸機器人

穩定高年化的 Bitfinex USD 自動放貸機器人。
策略與架構細節見 [DESIGN.md](DESIGN.md)。

- 🤖 **機器人**：Python，IQM 錨點 + 階梯掛單 + spike 追高 + 高利鎖長天期 + 自動重掛
- 📱 **Telegram**：成交/飆漲/錯誤即時推播，`/status` `/rates` `/earnings` `/pause` 指令
- 📊 **網頁 Dashboard**（GitHub Pages）：即時市場數據（WebSocket）+ 個人放貸總覽
- 🗄 **Supabase**：歷史快照、每日收益、機器人狀態

## 專案結構

```
lendbot/             機器人本體
├── __main__.py      入口（python -m lendbot）
├── config.py        設定載入（config.yaml + .env）
├── bfx_client.py    Bitfinex REST API（公開 + HMAC 私有）
├── strategy.py      策略引擎（純函式，有單元測試）
├── engine.py        核心循環（決策 → 下單 → 記錄 → 推播）
├── store.py         Supabase 寫入層
└── telegram_bot.py  Telegram 推播 + 指令
config.yaml          ★ 策略參數都在這，調整不用改程式
supabase/schema.sql  資料庫 schema（貼到 Supabase SQL Editor 執行）
web/                 GitHub Pages 靜態網頁
tests/               單元測試 + 煙霧測試
```

## 快速開始（本機模擬）

```bash
pip install -r requirements.txt
copy .env.example .env        # 什麼都不填 = 模擬模式
python -m lendbot --once      # 跑一個循環看決策
python -m lendbot             # 持續跑
python -m pytest tests/test_strategy.py   # 單元測試
```

模式由 `.env` 決定：

| BFX key | DRY_RUN | 行為 |
|---|---|---|
| 沒填 | — | **模擬模式**：模擬餘額 + 模擬成交，安全測試 |
| 有填 | `true` | **觀察模式**：讀真實帳戶，只記錄「會做什麼」不下單 |
| 有填 | `false` | **真實模式**：真正下單 |

## 上線設定

### 1. Bitfinex API Key
Bitfinex → API Keys → 建立，**只開** Account Info(讀)、Wallets(讀)、
Margin Funding(讀+寫)。**不要開提幣權限！**

### 2. Telegram
1. 找 @BotFather `/newbot` 拿 token
2. 找 @userinfobot 拿自己的 chat id
3. 先跟你的 bot 說一句話（bot 不能主動開聊）

### 3. Supabase
1. 建專案 → SQL Editor → 貼上 `supabase/schema.sql`
   （先把裡面的 `CHANGE_ME_TO_YOUR_SECRET_TOKEN` 改成你的 Dashboard 密碼）
2. Settings → API：`service_role` key 填到 `.env`（伺服器用），
   `anon` key 填到 `web/config.js`（網頁用，公開沒關係）

### 4. Zeabur 部署
1. 整個 repo 推上 GitHub（private 建議）
2. Zeabur → New Service → 連 GitHub repo，會自動偵測 Dockerfile
3. 環境變數照 `.env.example` 填上（`DRY_RUN` 先 `true` 觀察，穩了改 `false`）

### 5. GitHub Pages（網頁）
1. `web/config.js` 填入 Supabase URL + anon key 後 commit
2. GitHub repo → Settings → Pages → Source 選 **GitHub Actions**
3. push 到 main 自動部署（workflow 在 `.github/workflows/pages.yml`）
4. 手機開 `https://<帳號>.github.io/<repo>/`，輸入 Dashboard 密碼

> ⚠️ 用 GitHub Actions 部署 Pages，repo 可以維持 **private**，
> 但 Pages 網址本身是公開的——個人數據有密碼（token）保護，市場數據本來就公開。

## 策略調整

都在 `config.yaml`：階梯檔位/利率倍率、天期門檻、spike 靈敏度、
重掛時間、最低年化底線。改完重啟即生效，參數意義見檔內註解與 DESIGN.md。

## 風險提醒

- 放貸年化隨市場波動（牛市 15-30%+，平靜期可能 <5%），無法保證固定報酬
- 資金放在交易所有交易所風險，請自行評估投入比例
- 先用觀察模式跑幾天，確認決策合理再開真實模式
