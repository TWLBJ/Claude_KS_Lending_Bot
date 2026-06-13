/* Bitfinex 放貸 Dashboard
 * 市場區：fUSD 與 fUST 並列。即時數據走 Bitfinex 公開 WebSocket
 *（REST 沒開 CORS），K 棒用 candles 頻道 + lightweight-charts。
 * 個人區：Supabase RPC dashboard_data(token)，token 存 localStorage
 */
"use strict";

const WS_URL = "wss://api-pub.bitfinex.com/ws/2";
const CFG = window.APP_CONFIG || {};
const $ = (id) => document.getElementById(id);

const SYMBOLS = [
  { sym: "fUSD", label: "💵 fUSD（美元）" },
  { sym: "fUST", label: "🪙 fUST（USDT）" },
];
const TFS = ["15m", "1h", "6h", "1D"];
const DEFAULT_TF = "1h";
const VISIBLE_BARS = 48; // 預設顯示根數（1h K ≈ 2 天）
// K 棒天期篩選：聚合 2-30 天 / 單一天期（FRR 長單多印在 120 天）
const PERIODS = [
  { key: "a30:p2:p30", label: "2-30天" },
  { key: "p2", label: "2天" },
  { key: "p30", label: "30天" },
  { key: "p120", label: "120天" },
];
const DEFAULT_PKEY = "a30:p2:p30";

const dailyToApy = (r) => (Math.pow(1 + r, 365) - 1) * 100;
const pct = (apy) => apy.toFixed(2) + "%";

// Bitfinex 對提供融資（放貸）賺到的利息抽 15% 手續費（官方 fee schedule）
const FUNDING_FEE = 0.15;

// 一筆 status 的「卡住的錢」：放貸中 + 掛單中（已被預留）+ 可用
const offersTotal = (s) => (s.offers || []).reduce((a, o) => a + (o.amount || 0), 0);
const walletTotal = (s) => (s.available || 0) + (s.total_lent || 0) + offersTotal(s);
// 總預估年化：只有放貸中的錢在賺，分母放整個錢包 → 反映閒置/掛單未成交的拖累
const estApy = (s) => {
  const w = walletTotal(s);
  return w ? (s.total_lent || 0) * (s.weighted_apy || 0) / w : 0;
};

// 剩餘時間：用開始時間 + 天期 推算到期點，前端即時換算成 天/時/分（比後端 X.X 天好懂）
function fmtRemaining(openedIso, period) {
  if (!openedIso || !period) return "—";
  let ms = new Date(openedIso).getTime() + period * 86400000 - Date.now();
  if (ms <= 0) return "已到期";
  const d = Math.floor(ms / 86400000); ms -= d * 86400000;
  const h = Math.floor(ms / 3600000); ms -= h * 3600000;
  const m = Math.floor(ms / 60000);
  return (d ? `${d}天` : "") + (d || h ? `${h}小時` : "") + `${m}分`;
}

// 已結束放貸的淨獲利：利息 = 金額 × 日利率 × 持有天數（最低 1 小時），再扣 15% 手續費
function closedProfit(d) {
  const days = Math.max(d.held_days || 0, 1 / 24);  // Bitfinex 最低收 1 小時利息
  return (d.amount || 0) * (d.rate || 0) * days * (1 - FUNDING_FEE);
}
const chartColors = {
  grid: "#2c3644", text: "#8b98a9",
  line: "#4fc3f7", good: "#4caf80", bad: "#ef5350", warn: "#ffb74d",
};
const SYMBOL_COLORS = { fUSD: "#4fc3f7", fUST: "#4caf80", USD: "#4fc3f7", UST: "#4caf80" };

Chart.defaults.color = chartColors.text;
Chart.defaults.borderColor = chartColors.grid;

function iqm(values) {
  if (!values.length) return 0;
  const s = [...values].sort((a, b) => a - b);
  if (s.length < 4) return s.reduce((a, b) => a + b, 0) / s.length;
  const q = Math.floor(s.length / 4);
  const mid = s.slice(q, s.length - q);
  return mid.reduce((a, b) => a + b, 0) / mid.length;
}

// ═══════════ 市場區 DOM 建立 ═══════════

function buildMarketDOM() {
  $("marketSections").innerHTML = SYMBOLS.map(({ sym, label }) => `
    <div class="sym-block">
      <h2 class="sym-title">${label}</h2>
      <div class="cards">
        <div class="card"><div class="label">FRR 年化</div><div class="value" id="frr-${sym}">—</div></div>
        <div class="card"><div class="label">最近成交</div><div class="value" id="last-${sym}">—</div></div>
        <div class="card"><div class="label">成交 IQM</div><div class="value" id="iqm-${sym}">—</div></div>
        <div class="card"><div class="label">隊首掛單（市場最低）</div><div class="value" id="ask-${sym}">—</div></div>
        <div class="card"><div class="label">近 1 小時最高</div><div class="value" id="high-${sym}">—</div></div>
      </div>
      <div class="sym-charts">
        <div class="chart-box">
          <div class="chart-head">
            <h3>成交利率 K 線（年化 %）</h3>
            <div class="btn-rows">
              <div class="tf-btns" id="tfs-${sym}">
                ${TFS.map((tf) => `<button class="tf ${tf === DEFAULT_TF ? "active" : ""}"
                   data-sym="${sym}" data-tf="${tf}">${tf}</button>`).join("")}
              </div>
              <div class="tf-btns" id="pds-${sym}">
                ${PERIODS.map((p) => `<button class="tf pd ${p.key === DEFAULT_PKEY ? "active" : ""}"
                   data-sym="${sym}" data-pkey="${p.key}">${p.label}</button>`).join("")}
              </div>
            </div>
          </div>
          <div class="ohlc muted small" id="ohlc-${sym}">（滑鼠移到 K 棒上顯示開高低收）</div>
          <div class="kchart" id="kchart-${sym}"></div>
        </div>
        <div class="chart-box">
          <div class="chart-head">
            <h3>掛單簿深度</h3>
            <div class="tf-btns" id="bks-${sym}">
              ${["2天", "3-30天", ">30天", "借款方"].map((bk) =>
                `<button class="tf bk ${bk !== "借款方" ? "active" : ""}"
                  data-sym="${sym}" data-bk="${bk}">${bk}</button>`).join("")}
            </div>
          </div>
          <canvas id="book-${sym}"></canvas>
        </div>
      </div>
    </div>`).join("");

  document.querySelectorAll(".tf:not(.pd):not(.bk)").forEach((btn) =>
    btn.addEventListener("click", () => switchTf(btn.dataset.sym, btn.dataset.tf)));
  document.querySelectorAll(".tf.pd").forEach((btn) =>
    btn.addEventListener("click", () => switchPeriod(btn.dataset.sym, btn.dataset.pkey)));
  document.querySelectorAll(".tf.bk").forEach((btn) =>
    btn.addEventListener("click", () => {
      const { sym, bk } = btn.dataset;
      const sel = market.states[sym].bookSel;
      sel.has(bk) ? sel.delete(bk) : sel.add(bk);  // 複選開關
      btn.classList.toggle("active", sel.has(bk));
      market.states[sym].dirty = true;
    }));
}

// ═══════════ 市場區 WebSocket ═══════════

const market = {
  ws: null,
  chan: {},     // chanId -> { sym, channel }
  states: {},   // sym -> { tf, ticker, trades[], book[], candleChanId, kchart, kseries, bookChart, dirty }
};

function candleKey(sym, tf, pkey) {
  return `trade:${tf}:${sym}:${pkey}`;
}

function initKChart(sym) {
  const el = $(`kchart-${sym}`);
  const chart = LightweightCharts.createChart(el, {
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: chartColors.text },
    grid: { vertLines: { color: chartColors.grid }, horzLines: { color: chartColors.grid } },
    timeScale: { timeVisible: true, borderColor: chartColors.grid },
    rightPriceScale: { borderColor: chartColors.grid },
    // Normal 模式：十字線跟著滑鼠座標自由移動（預設會吸附到收盤價）
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: { priceFormatter: (v) => v.toFixed(2) + "%" },
  });
  const series = chart.addCandlestickSeries({
    upColor: chartColors.good, downColor: chartColors.bad,
    wickUpColor: chartColors.good, wickDownColor: chartColors.bad,
    borderVisible: false,
    priceFormat: { type: "custom", formatter: (v) => v.toFixed(2) + "%", minMove: 0.01 },
  });
  // 滑過 K 棒時顯示該棒的開高低收
  chart.subscribeCrosshairMove((param) => {
    const el = $(`ohlc-${sym}`);
    const d = param?.seriesData?.get(series);
    if (!d || d.open === undefined) {
      el.textContent = "（滑鼠移到 K 棒上顯示開高低收）";
      return;
    }
    const t = new Date((param.time - TZ_SHIFT) * 1000).toLocaleString("zh-TW",
      { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
    const color = d.close >= d.open ? chartColors.good : chartColors.bad;
    el.innerHTML = `${t}｜開 ${d.open.toFixed(2)}%｜高 ${d.high.toFixed(2)}%｜` +
      `低 ${d.low.toFixed(2)}%｜收 <b style="color:${color}">${d.close.toFixed(2)}%</b>`;
  });
  return { chart, series };
}

// lightweight-charts 的時間軸固定用 UTC 顯示，把時間戳平移成本地時區（如 UTC+8）
const TZ_SHIFT = -new Date().getTimezoneOffset() * 60;

// Bitfinex candle 陣列順序：[MTS, OPEN, CLOSE, HIGH, LOW, VOLUME]（close 在 high 前！）
function mapCandle(c) {
  return { time: c[0] / 1000 + TZ_SHIFT, open: dailyToApy(c[1]), close: dailyToApy(c[2]),
           high: dailyToApy(c[3]), low: dailyToApy(c[4]) };
}

function startMarket() {
  if (market.ws) { market.ws.onclose = null; market.ws.close(); }
  market.chan = {};
  for (const { sym } of SYMBOLS) {
    const prev = market.states[sym];
    market.states[sym] = {
      tf: prev?.tf || DEFAULT_TF, pkey: prev?.pkey || DEFAULT_PKEY,
      bookSel: prev?.bookSel || new Set(["2天", "3-30天", ">30天"]),  // 預設不含借款方
      ticker: null, trades: [], book: [],
      candleChanId: null,
      kchart: prev?.kchart, kseries: prev?.kseries, bookChart: prev?.bookChart,
      dirty: false,
    };
    if (!market.states[sym].kseries) {
      const { chart, series } = initKChart(sym);
      market.states[sym].kchart = chart;
      market.states[sym].kseries = series;
    }
  }

  const ws = new WebSocket(WS_URL);
  market.ws = ws;

  ws.onopen = () => {
    for (const { sym } of SYMBOLS) {
      ws.send(JSON.stringify({ event: "subscribe", channel: "ticker", symbol: sym }));
      ws.send(JSON.stringify({ event: "subscribe", channel: "trades", symbol: sym }));
      ws.send(JSON.stringify({ event: "subscribe", channel: "book", symbol: sym,
                               prec: "P0", len: "100" }));
      ws.send(JSON.stringify({ event: "subscribe", channel: "candles",
                               key: candleKey(sym, market.states[sym].tf,
                                              market.states[sym].pkey) }));
    }
  };

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (!Array.isArray(msg)) {
      if (msg.event === "subscribed") {
        if (msg.channel === "candles") {
          const sym = msg.key.split(":")[2];
          market.chan[msg.chanId] = { sym, channel: "candles" };
          market.states[sym].candleChanId = msg.chanId;
        } else {
          market.chan[msg.chanId] = { sym: msg.symbol, channel: msg.channel };
        }
      } else if (msg.event === "unsubscribed") {
        delete market.chan[msg.chanId];
      }
      return;
    }
    const [chanId, payload, extra] = msg;
    if (payload === "hb") return;
    const meta = market.chan[chanId];
    if (!meta) return;
    handleChannel(meta.sym, meta.channel, payload, extra);
  };

  ws.onclose = () => setTimeout(startMarket, 3000);
  ws.onerror = () => { $("lastUpdate").textContent = "連線中斷，重連中…"; };
}

function handleChannel(sym, channel, payload, extra) {
  const st = market.states[sym];
  if (!st) return;

  if (channel === "ticker" && Array.isArray(payload)) {
    st.ticker = payload;
    st.dirty = true;
  } else if (channel === "trades") {
    if (Array.isArray(payload) && Array.isArray(payload[0])) {
      st.trades = payload.map((t) => ({ mts: t[1], rate: t[3] }));
    } else if (payload === "fte" && Array.isArray(extra)) {
      st.trades.unshift({ mts: extra[1], rate: extra[3] });
      st.trades = st.trades.slice(0, 250);
    }
    st.dirty = true;
  } else if (channel === "book") {
    if (Array.isArray(payload) && Array.isArray(payload[0])) {
      st.book = payload;
    } else if (Array.isArray(payload)) {
      applyBookUpdate(st, payload);
    }
    st.dirty = true;
  } else if (channel === "candles") {
    if (Array.isArray(payload) && Array.isArray(payload[0])) {
      // 快照：去重 + 由舊到新
      const seen = new Map();
      for (const c of payload) seen.set(c[0], c);
      const data = [...seen.values()].sort((a, b) => a[0] - b[0]).map(mapCandle);
      st.kseries.setData(data);
      // 預設只顯示最近約 48 根（1h K 約 2 天），太寬會密密麻麻；使用者仍可自由縮放/平移
      const bars = data.length;
      if (bars > VISIBLE_BARS) {
        st.kchart.timeScale().setVisibleLogicalRange({ from: bars - VISIBLE_BARS, to: bars + 1 });
      } else {
        st.kchart.timeScale().fitContent();
      }
    } else if (Array.isArray(payload) && typeof payload[0] === "number") {
      st.kseries.update(mapCandle(payload));
    }
  }
}

function applyBookUpdate(st, [rate, period, count, amount]) {
  const idx = st.book.findIndex((e) => e[0] === rate && e[1] === period);
  if (count > 0) {
    if (idx >= 0) st.book[idx] = [rate, period, count, amount];
    else st.book.push([rate, period, count, amount]);
  } else if (idx >= 0) {
    st.book.splice(idx, 1);
  }
}

function resubscribeCandles(sym) {
  const st = market.states[sym];
  if (market.ws?.readyState !== WebSocket.OPEN) return;
  if (st.candleChanId != null) {
    market.ws.send(JSON.stringify({ event: "unsubscribe", chanId: st.candleChanId }));
    st.candleChanId = null;
  }
  market.ws.send(JSON.stringify({ event: "subscribe", channel: "candles",
                                  key: candleKey(sym, st.tf, st.pkey) }));
}

function switchTf(sym, tf) {
  const st = market.states[sym];
  if (!st || st.tf === tf) return;
  st.tf = tf;
  document.querySelectorAll(`#tfs-${sym} .tf`).forEach((b) =>
    b.classList.toggle("active", b.dataset.tf === tf));
  resubscribeCandles(sym);
}

function switchPeriod(sym, pkey) {
  const st = market.states[sym];
  if (!st || st.pkey === pkey) return;
  st.pkey = pkey;
  document.querySelectorAll(`#pds-${sym} .tf`).forEach((b) =>
    b.classList.toggle("active", b.dataset.pkey === pkey));
  st.kseries.setData([]);  // 清掉舊天期的 K 棒，等新快照
  resubscribeCandles(sym);
}

// ═══════════ 市場區渲染（每 2 秒，K 線除外）═══════════

function renderMarket() {
  let updated = false;
  for (const { sym } of SYMBOLS) {
    const st = market.states[sym];
    if (!st?.dirty) continue;
    st.dirty = false;
    updated = true;

    if (st.ticker) {
      $(`frr-${sym}`).textContent = pct(dailyToApy(st.ticker[0]));
      $(`ask-${sym}`).textContent = pct(dailyToApy(st.ticker[4]));
      $(`last-${sym}`).textContent = pct(dailyToApy(st.ticker[9]));
    }
    if (st.trades.length) {
      $(`iqm-${sym}`).textContent = pct(dailyToApy(iqm(st.trades.map((t) => t.rate))));
      const hourAgo = Date.now() - 3600_000;
      const hr = st.trades.filter((t) => t.mts >= hourAgo).map((t) => t.rate);
      $(`high-${sym}`).textContent = hr.length ? pct(dailyToApy(Math.max(...hr))) : "—";
    }
    if (st.book.length) drawBookChart(sym, st);
  }
  if (updated) {
    $("lastUpdate").textContent =
      "更新 " + new Date().toLocaleTimeString("zh-TW", { hour12: false });
  }
}

// 掛單簿深度依天期分組：各自畫累計曲線，看得出不同天期市場的供給結構
const BOOK_BUCKETS = [
  { name: "2天", test: (p) => p <= 2, color: "#4fc3f7" },
  { name: "3-30天", test: (p) => p > 2 && p <= 30, color: "#ffb74d" },
  { name: ">30天", test: (p) => p > 30, color: "#ab7df8" },
];

function drawBookChart(sym, st) {
  const all = BOOK_BUCKETS.map((b) => {
    const asks = st.book.filter((e) => e[3] > 0 && b.test(e[1]))
      .map((e) => ({ rate: e[0], amount: e[3] }))
      .sort((x, y) => x.rate - y.rate);
    let cum = 0;
    return {
      label: b.name,
      data: asks.map((a) => { cum += a.amount; return { x: dailyToApy(a.rate), y: cum }; }),
      borderColor: b.color, pointRadius: 0, stepped: true, borderWidth: 1.5,
    };
  });

  // 借款方（bids）：想借錢的人掛的需求單，從最高出價往低利率累計
  const bids = st.book.filter((e) => e[3] < 0)
    .map((e) => ({ rate: e[0], amount: -e[3] }))
    .sort((x, y) => y.rate - x.rate);
  let cumB = 0;
  all.push({
    label: "借款方",
    data: bids.map((b) => { cumB += b.amount; return { x: dailyToApy(b.rate), y: cumB }; }),
    borderColor: chartColors.bad, pointRadius: 0, stepped: true,
    borderWidth: 1.5, borderDash: [5, 3],
  });

  const datasets = all.filter((ds) => ds.data.length && st.bookSel.has(ds.label));

  if (st.bookChart) {
    st.bookChart.data.datasets = datasets;
    st.bookChart.update("none");
    return;
  }
  st.bookChart = new Chart($(`book-${sym}`), {
    type: "line",
    data: { datasets },
    options: {
      animation: false,
      plugins: { legend: { display: true }, tooltip: {
        callbacks: { label: (c) =>
          `${c.dataset.label}：年化 ${c.parsed.x.toFixed(2)}% 前累計 $${Math.round(c.parsed.y).toLocaleString()}` },
      }},
      scales: {
        x: { type: "linear", title: { display: true, text: "年化 %" },
             ticks: { callback: (v) => v.toFixed(1) + "%" } },
        y: { ticks: { callback: (v) => "$" + (v / 1000).toFixed(0) + "k" } },
      },
    },
  });
}

// ═══════════ 個人區（Supabase）═══════════

const TOKEN_KEY = "dash_token";

async function rpcDashboard(token) {
  if (!CFG.SUPABASE_URL || !CFG.SUPABASE_ANON_KEY) return { error: "未設定 Supabase" };
  try {
    const r = await fetch(`${CFG.SUPABASE_URL}/rest/v1/rpc/dashboard_data`, {
      method: "POST",
      headers: {
        apikey: CFG.SUPABASE_ANON_KEY,
        Authorization: `Bearer ${CFG.SUPABASE_ANON_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ p_token: token }),
    });
    if (!r.ok) return { error: `Supabase 回應 ${r.status}` };
    const data = await r.json();
    if (data === null) return { error: "密碼錯誤" };
    return { data };
  } catch (e) {
    return { error: "Supabase 連線失敗" };
  }
}

async function tryUnlock(token, silent = false) {
  const { data, error } = await rpcDashboard(token);
  if (error) {
    if (!silent) {
      $("lockError").textContent = error;
      $("lockError").classList.remove("hidden");
    }
    localStorage.removeItem(TOKEN_KEY);
    return false;
  }
  localStorage.setItem(TOKEN_KEY, token);
  $("lockPanel").classList.add("hidden");
  $("dashPanel").classList.remove("hidden");
  renderDashboard(data);
  return true;
}

let earningsChart, anchorChart;

function renderDashboard(d) {
  const statuses = d.statuses || [];
  const first = statuses[0] || {};
  $("botMode").textContent = (first.mode || "未知") + (first.paused ? "（暫停中）" : "");

  const newestTs = Math.max(0, ...statuses.map((s) => s.ts ? new Date(s.ts).getTime() : 0));
  if (newestTs) {
    const age = (Date.now() - newestTs) / 60000;
    const clock = new Date(newestTs).toLocaleTimeString("zh-TW", { hour12: false });
    $("botHeartbeat").textContent = age < 15
      ? `🟢 ${Math.round(age)} 分鐘前回報 · ${clock}`
      : `🔴 已 ${Math.round(age)} 分鐘沒回報（最後 ${clock}），機器人可能停了`;
  }

  // 總覽卡片：USD 與 UST 都是美元穩定幣，直接加總顯示（明細與其餘指標移到下方幣別明細表）
  const sum = (f) => statuses.reduce((a, s) => a + (f(s) || 0), 0);
  const totalLent = sum((s) => s.total_lent);
  const grandWallet = sum(walletTotal);
  const grandEstApy = grandWallet
    ? statuses.reduce((a, s) => a + (s.total_lent || 0) * (s.weighted_apy || 0), 0) / grandWallet
    : 0;
  $("dWallet").textContent = "$" + grandWallet.toLocaleString(undefined, { maximumFractionDigits: 2 });
  $("dLent").textContent = "$" + totalLent.toLocaleString();
  $("dEstApy").textContent = pct(grandEstApy);

  const earnings = d.earnings || [];
  const total30 = earnings.reduce((a, e) => a + (e.amount || 0), 0);
  $("dEarn30").textContent = "$" + total30.toFixed(2);

  renderSymbolTable(statuses);
  renderCredits(statuses);
  drawEarningsChart(earnings);
  drawDailyApyChart(earnings);
  drawAnchorChart(d.snapshots || []);
  renderOffers(statuses);
  renderSuggested(statuses);
  renderClosed(d.closed_credits || []);
  renderActions(d.recent_actions || []);
}

function fmtDate(iso) {
  return new Date(iso).toLocaleString("zh-TW",
    { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

function renderCredits(statuses) {
  const rows = statuses.flatMap((s) =>
    (s.credits || []).map((c) => ({ symbol: s.symbol, ...c })));
  const tbody = $("creditsTable").querySelector("tbody");
  tbody.innerHTML = rows.length
    ? rows.map((c) => {
        const remainPct = c.period ? Math.max(0, c.remaining_days / c.period) : 0;
        const bar = `<span class="mini-bar"><span style="width:${(1 - remainPct) * 100}%"></span></span>`;
        // 放滿預估報酬：放好放滿整個天期能拿到的淨利息（扣 15% 手續費）
        const full = (c.amount || 0) * (c.rate || 0) * (c.period || 0) * (1 - FUNDING_FEE);
        return `<tr><td>${c.symbol}</td><td>$${c.amount.toLocaleString()}</td>
          <td>${pct(c.apy ?? dailyToApy(c.rate))}</td><td>${c.period} 天</td>
          <td class="good">+$${full.toFixed(4)}</td>
          <td>${c.opened ? fmtDate(c.opened) : "—"}</td>
          <td>${fmtRemaining(c.opened, c.period)} ${bar}</td></tr>`;
      }).join("")
    : `<tr><td colspan="7" class="muted">目前沒有放貸中的部位</td></tr>`;
}

function renderSuggested(statuses) {
  const rows = statuses.flatMap((s) =>
    (s.suggested_offers || []).map((o) => ({ symbol: s.symbol, ...o })));
  $("suggestBox").classList.toggle("hidden", !rows.length);
  if (!rows.length) return;
  $("suggestTable").querySelector("tbody").innerHTML = rows.map((o) =>
    `<tr><td>${o.symbol}</td><td>$${o.amount.toLocaleString()}</td>
     <td>${(o.apy ?? dailyToApy(o.rate)).toFixed(2)}%</td>
     <td>${(o.rate * 100).toFixed(5)}%</td><td>${o.period} 天</td></tr>`).join("");
}

// 已結束放貸：時間篩選 + 分頁（資料存起來，按鈕只切視圖不重打 API）
const CLOSED_PER_PAGE = 10;
let closedAll = [];
let closedRangeDays = 7;  // 預設近 7 天
let closedReasons = new Set(["closed_early", "closed_matured"]);  // 預設兩種都顯示
let closedPage = 0;

function renderClosed(closed) {
  closedAll = closed || [];
  closedPage = 0;
  renderClosedView();
}

function renderClosedView() {
  const tbody = $("closedTable").querySelector("tbody");
  const cutoff = closedRangeDays ? Date.now() - closedRangeDays * 86400000 : 0;
  const filtered = closedAll.filter((a) =>
    new Date(a.ts).getTime() >= cutoff && closedReasons.has(a.action));

  const pages = Math.max(1, Math.ceil(filtered.length / CLOSED_PER_PAGE));
  closedPage = Math.min(closedPage, pages - 1);
  const page = filtered.slice(closedPage * CLOSED_PER_PAGE, (closedPage + 1) * CLOSED_PER_PAGE);

  tbody.innerHTML = page.length
    ? page.map((a) => {
        const d = a.detail || {};
        const early = a.action === "closed_early";
        const profit = closedProfit(d);
        const heldPct = d.period ? Math.min(100, (d.held_days || 0) / d.period * 100) : 0;
        const bar = `<span class="mini-bar"><span style="width:${heldPct}%"></span></span>`;
        return `<tr><td>${fmtDate(a.ts)}</td><td>${d.symbol || ""}</td>
          <td>$${(d.amount ?? 0).toLocaleString()}</td>
          <td>${(d.apy ?? 0).toFixed(2)}%</td>
          <td>${heldPct.toFixed(0)}% <span class="muted small">/ ${d.period}天</span> ${bar}</td>
          <td class="good">+$${profit.toFixed(4)}</td>
          <td><span class="badge ${early ? "warn" : "ok"}">${early ? "提前還款" : "到期歸還"}</span></td></tr>`;
      }).join("")
    : `<tr><td colspan="7" class="muted">這段期間沒有結束的單</td></tr>`;

  // 分頁列：總筆數 + 該期間淨獲利合計 + 上/下一頁
  const totalProfit = filtered.reduce((s, a) => s + closedProfit(a.detail || {}), 0);
  $("closedPager").innerHTML = filtered.length
    ? `<span class="page-info">共 ${filtered.length} 筆 · 淨獲利合計 +$${totalProfit.toFixed(4)}</span>
       <button class="ghost" id="closedPrev" ${closedPage <= 0 ? "disabled" : ""}>← 上一頁</button>
       <span class="page-info">${closedPage + 1} / ${pages}</span>
       <button class="ghost" id="closedNext" ${closedPage >= pages - 1 ? "disabled" : ""}>下一頁 →</button>`
    : "";
  const prev = $("closedPrev"), next = $("closedNext");
  if (prev) prev.onclick = () => { closedPage--; renderClosedView(); };
  if (next) next.onclick = () => { closedPage++; renderClosedView(); };
}

function renderSymbolTable(statuses) {
  const tbody = $("symbolTable").querySelector("tbody");
  const money = (v) => "$" + (v || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (!statuses.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">機器人還沒回報</td></tr>`;
    return;
  }
  const rows = statuses.map((s) => `<tr><td>${s.symbol}</td>
      <td>${money(walletTotal(s))}</td>
      <td>${money(s.total_lent)}</td>
      <td>${pct(s.weighted_apy ?? 0)}</td>
      <td>${pct(estApy(s))}</td>
      <td>${money(s.available)}</td>
      <td>${s.credits_count ?? 0} 筆</td>
      <td>${s.offers_count ?? 0} 筆</td></tr>`).join("");

  // 合計列：加權年化用放貸金額加權，總預估年化用整個錢包當分母
  const sum = (f) => statuses.reduce((a, s) => a + (f(s) || 0), 0);
  const tLent = sum((s) => s.total_lent);
  const tWallet = sum(walletTotal);
  const wApy = tLent ? sum((s) => (s.total_lent || 0) * (s.weighted_apy || 0)) / tLent : 0;
  const eApy = tWallet ? sum((s) => (s.total_lent || 0) * (s.weighted_apy || 0)) / tWallet : 0;
  const total = `<tr class="total-row"><td>合計</td>
      <td>${money(tWallet)}</td><td>${money(tLent)}</td>
      <td>${pct(wApy)}</td><td>${pct(eApy)}</td>
      <td>${money(sum((s) => s.available))}</td>
      <td>${sum((s) => s.credits_count)} 筆</td>
      <td>${sum((s) => s.offers_count)} 筆</td></tr>`;
  tbody.innerHTML = rows + total;
}

function drawEarningsChart(earnings) {
  const dates = [...new Set(earnings.map((e) => e.date))].sort();
  const currencies = [...new Set(earnings.map((e) => e.currency))];
  const datasets = currencies.map((cur) => ({
    label: cur,
    data: dates.map((d) =>
      earnings.find((e) => e.date === d && e.currency === cur)?.amount ?? 0),
    backgroundColor: SYMBOL_COLORS[cur] || chartColors.good,
  }));
  earningsChart?.destroy();
  earningsChart = new Chart($("earningsChart"), {
    type: "bar",
    data: { labels: dates.map((d) => d.slice(5)), datasets },
    options: {
      plugins: { legend: { display: currencies.length > 1 } },
      scales: { x: { stacked: true }, y: { stacked: true } },
    },
  });
}

let dailyApyChart;

function drawDailyApyChart(earnings) {
  // 每日實際年化 = 當日利息 ÷（當日入帳後錢包餘額 - 當日利息）× 365
  // 餘額含放貸中的錢，是不錯的資金規模近似；出入金當天分母會跳動 → 該日數據失真
  const dates = [...new Set(earnings.map((e) => e.date))].sort();
  const currencies = [...new Set(earnings.map((e) => e.currency))];
  const datasets = currencies.map((cur) => ({
    label: cur,
    data: dates.map((d) => {
      const e = earnings.find((x) => x.date === d && x.currency === cur);
      if (!e || !e.balance || e.balance <= e.amount) return null;
      return +(e.amount / (e.balance - e.amount) * 365 * 100).toFixed(2);
    }),
    borderColor: SYMBOL_COLORS[cur] || chartColors.line,
    pointRadius: 2, borderWidth: 1.5, tension: 0.2, spanGaps: true,
  }));
  dailyApyChart?.destroy();
  dailyApyChart = new Chart($("dailyApyChart"), {
    type: "line",
    data: { labels: dates.map((d) => d.slice(5)), datasets },
    options: {
      plugins: { legend: { display: currencies.length > 1 } },
      scales: { y: { ticks: { callback: (v) => v.toFixed(1) + "%" } } },
    },
  });
}

function drawAnchorChart(snaps) {
  const symbols = [...new Set(snaps.map((s) => s.symbol))];
  const datasets = symbols.map((sym) => ({
    label: sym,
    data: snaps.filter((s) => s.symbol === sym)
      .map((s) => ({ x: new Date(s.ts).getTime(), y: s.anchor_apy })),
    borderColor: SYMBOL_COLORS[sym] || chartColors.warn,
    pointRadius: 0, borderWidth: 1.5, tension: 0.2,
  }));
  anchorChart?.destroy();
  anchorChart = new Chart($("anchorChart"), {
    type: "line",
    data: { datasets },
    options: {
      plugins: { legend: { display: symbols.length > 1 } },
      scales: {
        x: { type: "linear", ticks: {
          maxTicksLimit: 6,
          callback: (v) => new Date(v).toLocaleTimeString("zh-TW",
            { hour: "2-digit", minute: "2-digit", hour12: false }),
        }},
        y: { ticks: { callback: (v) => v.toFixed(1) + "%" } },
      },
    },
  });
}

function renderOffers(statuses) {
  const rows = statuses.flatMap((s) =>
    (s.offers || []).map((o) => ({ symbol: s.symbol, ...o })));
  const tbody = $("offersTable").querySelector("tbody");
  tbody.innerHTML = rows.length
    ? rows.map((o) => `<tr><td>${o.symbol}</td><td>$${o.amount.toLocaleString()}</td>
        <td>${pct(dailyToApy(o.rate))}</td><td>${o.period} 天</td></tr>`).join("")
    : `<tr><td colspan="4" class="muted">目前沒有掛單</td></tr>`;
}

function renderActions(actions) {
  const tbody = $("actionsTable").querySelector("tbody");
  tbody.innerHTML = actions.length
    ? actions.map((a) => {
        const t = new Date(a.ts).toLocaleString("zh-TW", { hour12: false });
        const det = a.detail
          ? `${a.detail.symbol || ""} $${(a.detail.amount ?? 0).toLocaleString()} @ 年化 ${pct(dailyToApy(a.detail.rate ?? 0))}`
          : "";
        return `<tr><td>${t}</td><td>${a.action}</td><td>${det}</td></tr>`;
      }).join("")
    : `<tr><td colspan="3" class="muted">還沒有紀錄</td></tr>`;
}

// ═══════════ 初始化 ═══════════

$("unlockBtn").addEventListener("click", () => tryUnlock($("tokenInput").value.trim()));
$("tokenInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") tryUnlock($("tokenInput").value.trim());
});
$("lockBtn").addEventListener("click", () => {
  localStorage.removeItem(TOKEN_KEY);
  $("dashPanel").classList.add("hidden");
  $("lockPanel").classList.remove("hidden");
});

// 立即刷新：手動重抓一次資料（數據每 5 分鐘才自動拉，等不及就按這個）
$("refreshBtn").addEventListener("click", async () => {
  const t = localStorage.getItem(TOKEN_KEY);
  if (!t) return;
  const btn = $("refreshBtn");
  btn.disabled = true;
  btn.textContent = "⟳ 刷新中…";
  await tryUnlock(t, true);
  btn.textContent = "⟳ 刷新";
  btn.disabled = false;
});

// 已結束放貸的時間範圍切換（單選）
document.querySelectorAll("#closedRange .tf").forEach((btn) =>
  btn.addEventListener("click", () => {
    document.querySelectorAll("#closedRange .tf").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    closedRangeDays = Number(btn.dataset.days);
    closedPage = 0;
    renderClosedView();
  }));

// 結束原因篩選（複選，預設兩種都開；至少留一個避免全空）
document.querySelectorAll("#closedReason .tf").forEach((btn) =>
  btn.addEventListener("click", () => {
    const r = btn.dataset.reason;
    if (closedReasons.has(r) && closedReasons.size > 1) {
      closedReasons.delete(r);
      btn.classList.remove("active");
    } else {
      closedReasons.add(r);
      btn.classList.add("active");
    }
    closedPage = 0;
    renderClosedView();
  }));

buildMarketDOM();
startMarket();
setInterval(renderMarket, 2000);  // 卡片/深度圖最多每 2 秒重繪；K 線即時更新

const saved = localStorage.getItem(TOKEN_KEY);
if (saved) tryUnlock(saved, true);
setInterval(() => {
  const t = localStorage.getItem(TOKEN_KEY);
  if (t && !$("dashPanel").classList.contains("hidden")) tryUnlock(t, true);
}, 300_000);
