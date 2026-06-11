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

const dailyToApy = (r) => (Math.pow(1 + r, 365) - 1) * 100;
const pct = (apy) => apy.toFixed(2) + "%";
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
        <div class="card"><div class="label">最佳掛單</div><div class="value" id="ask-${sym}">—</div></div>
        <div class="card"><div class="label">近 1 小時最高</div><div class="value" id="high-${sym}">—</div></div>
      </div>
      <div class="sym-charts">
        <div class="chart-box">
          <div class="chart-head">
            <h3>成交利率 K 線（年化 %）</h3>
            <div class="tf-btns" id="tfs-${sym}">
              ${TFS.map((tf) => `<button class="tf ${tf === DEFAULT_TF ? "active" : ""}"
                 data-sym="${sym}" data-tf="${tf}">${tf}</button>`).join("")}
            </div>
          </div>
          <div class="kchart" id="kchart-${sym}"></div>
        </div>
        <div class="chart-box">
          <h3>掛單簿深度（放貸方）</h3>
          <canvas id="book-${sym}"></canvas>
        </div>
      </div>
    </div>`).join("");

  document.querySelectorAll(".tf").forEach((btn) =>
    btn.addEventListener("click", () => switchTf(btn.dataset.sym, btn.dataset.tf)));
}

// ═══════════ 市場區 WebSocket ═══════════

const market = {
  ws: null,
  chan: {},     // chanId -> { sym, channel }
  states: {},   // sym -> { tf, ticker, trades[], book[], candleChanId, kchart, kseries, bookChart, dirty }
};

function candleKey(sym, tf) {
  return `trade:${tf}:${sym}:a30:p2:p30`;  // 聚合 2-30 天期
}

function initKChart(sym) {
  const el = $(`kchart-${sym}`);
  const chart = LightweightCharts.createChart(el, {
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: chartColors.text },
    grid: { vertLines: { color: chartColors.grid }, horzLines: { color: chartColors.grid } },
    timeScale: { timeVisible: true, borderColor: chartColors.grid },
    // 對數刻度：放貸利率偶爾飆漲數十倍（如年化 1000%+），線性刻度會把平時區間壓扁
    rightPriceScale: { borderColor: chartColors.grid,
                       mode: LightweightCharts.PriceScaleMode.Logarithmic },
    localization: { priceFormatter: (v) => v.toFixed(2) + "%" },
  });
  const series = chart.addCandlestickSeries({
    upColor: chartColors.good, downColor: chartColors.bad,
    wickUpColor: chartColors.good, wickDownColor: chartColors.bad,
    borderVisible: false,
    priceFormat: { type: "custom", formatter: (v) => v.toFixed(2) + "%", minMove: 0.01 },
  });
  return { chart, series };
}

// Bitfinex candle 陣列順序：[MTS, OPEN, CLOSE, HIGH, LOW, VOLUME]（close 在 high 前！）
function mapCandle(c) {
  return { time: c[0] / 1000, open: dailyToApy(c[1]), close: dailyToApy(c[2]),
           high: dailyToApy(c[3]), low: dailyToApy(c[4]) };
}

function startMarket() {
  if (market.ws) { market.ws.onclose = null; market.ws.close(); }
  market.chan = {};
  for (const { sym } of SYMBOLS) {
    const prev = market.states[sym];
    market.states[sym] = {
      tf: prev?.tf || DEFAULT_TF, ticker: null, trades: [], book: [],
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
                               key: candleKey(sym, market.states[sym].tf) }));
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
      st.kchart.timeScale().fitContent();
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

function switchTf(sym, tf) {
  const st = market.states[sym];
  if (!st || st.tf === tf) return;
  st.tf = tf;
  document.querySelectorAll(`#tfs-${sym} .tf`).forEach((b) =>
    b.classList.toggle("active", b.dataset.tf === tf));
  if (market.ws?.readyState === WebSocket.OPEN) {
    if (st.candleChanId != null) {
      market.ws.send(JSON.stringify({ event: "unsubscribe", chanId: st.candleChanId }));
      st.candleChanId = null;
    }
    market.ws.send(JSON.stringify({ event: "subscribe", channel: "candles",
                                    key: candleKey(sym, tf) }));
  }
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

function drawBookChart(sym, st) {
  const asks = st.book.filter((e) => e[3] > 0)
    .map((e) => ({ rate: e[0], amount: e[3] }))
    .sort((a, b) => a.rate - b.rate);
  let cum = 0;
  const pts = asks.map((a) => { cum += a.amount; return { x: dailyToApy(a.rate), y: cum }; });
  if (st.bookChart) {
    st.bookChart.data.datasets[0].data = pts;
    st.bookChart.update("none");
    return;
  }
  st.bookChart = new Chart($(`book-${sym}`), {
    type: "line",
    data: { datasets: [{ data: pts, borderColor: SYMBOL_COLORS[sym] || chartColors.good,
                         fill: true, backgroundColor: "rgba(76,175,128,.15)",
                         pointRadius: 0, stepped: true }] },
    options: {
      animation: false,
      plugins: { legend: { display: false }, tooltip: {
        callbacks: { label: (c) =>
          `年化 ${c.parsed.x.toFixed(2)}% 前累計 $${Math.round(c.parsed.y).toLocaleString()}` },
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
    $("botHeartbeat").textContent = age < 15
      ? `🟢 ${Math.round(age)} 分鐘前回報`
      : `🔴 已 ${Math.round(age)} 分鐘沒回報，機器人可能停了`;
  }

  // 總覽卡片：USD 與 UST 都是美元穩定幣，直接加總顯示
  const sum = (f) => statuses.reduce((a, s) => a + (f(s) || 0), 0);
  const totalLent = sum((s) => s.total_lent);
  const weightedApy = totalLent
    ? statuses.reduce((a, s) => a + (s.total_lent || 0) * (s.weighted_apy || 0), 0) / totalLent
    : 0;
  $("dLent").textContent = "$" + totalLent.toLocaleString();
  $("dApy").textContent = pct(weightedApy);
  $("dAvail").textContent = "$" + sum((s) => s.available).toLocaleString();
  $("dCredits").textContent = sum((s) => s.credits_count);
  $("dOffers").textContent = sum((s) => s.offers_count);

  const earnings = d.earnings || [];
  const total30 = earnings.reduce((a, e) => a + (e.amount || 0), 0);
  $("dEarn30").textContent = "$" + total30.toFixed(2);

  renderSymbolTable(statuses);
  renderCredits(statuses);
  drawEarningsChart(earnings);
  drawAnchorChart(d.snapshots || []);
  renderOffers(statuses);
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
        const remainPct = c.period ? c.remaining_days / c.period : 0;
        const bar = `<span class="mini-bar"><span style="width:${(1 - remainPct) * 100}%"></span></span>`;
        return `<tr><td>${c.symbol}</td><td>$${c.amount.toLocaleString()}</td>
          <td>${pct(c.apy ?? dailyToApy(c.rate))}</td><td>${c.period} 天</td>
          <td>${c.opened ? fmtDate(c.opened) : "—"}</td>
          <td>${c.remaining_days} 天 ${bar}</td></tr>`;
      }).join("")
    : `<tr><td colspan="6" class="muted">目前沒有放貸中的部位</td></tr>`;
}

function renderClosed(closed) {
  const tbody = $("closedTable").querySelector("tbody");
  tbody.innerHTML = closed.length
    ? closed.map((a) => {
        const d = a.detail || {};
        const early = a.action === "closed_early";
        return `<tr><td>${fmtDate(a.ts)}</td><td>${d.symbol || ""}</td>
          <td>$${(d.amount ?? 0).toLocaleString()}</td>
          <td>${(d.apy ?? 0).toFixed(2)}%</td>
          <td>${(d.held_days ?? 0).toFixed(1)} / ${d.period} 天</td>
          <td><span class="badge ${early ? "warn" : "ok"}">${early ? "提前還款" : "到期歸還"}</span></td></tr>`;
      }).join("")
    : `<tr><td colspan="6" class="muted">還沒有結束的單（機器人啟動後才開始追蹤）</td></tr>`;
}

function renderSymbolTable(statuses) {
  const tbody = $("symbolTable").querySelector("tbody");
  tbody.innerHTML = statuses.length
    ? statuses.map((s) => `<tr><td>${s.symbol}</td>
        <td>$${(s.total_lent ?? 0).toLocaleString()}</td>
        <td>${pct(s.weighted_apy ?? 0)}</td>
        <td>$${(s.available ?? 0).toLocaleString()}</td>
        <td>${s.offers_count ?? 0} 筆</td></tr>`).join("")
    : `<tr><td colspan="5" class="muted">機器人還沒回報</td></tr>`;
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

buildMarketDOM();
startMarket();
setInterval(renderMarket, 2000);  // 卡片/深度圖最多每 2 秒重繪；K 線即時更新

const saved = localStorage.getItem(TOKEN_KEY);
if (saved) tryUnlock(saved, true);
setInterval(() => {
  const t = localStorage.getItem(TOKEN_KEY);
  if (t && !$("dashPanel").classList.contains("hidden")) tryUnlock(t, true);
}, 300_000);
