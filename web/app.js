/* Bitfinex 放貸 Dashboard
 * 市場區：Bitfinex 公開 WebSocket（REST 沒開 CORS，WS 不受限且即時）
 * 個人區：Supabase RPC dashboard_data(token)，token 存 localStorage
 */
"use strict";

const WS_URL = "wss://api-pub.bitfinex.com/ws/2";
const CFG = window.APP_CONFIG || {};
const $ = (id) => document.getElementById(id);

const dailyToApy = (r) => (Math.pow(1 + r, 365) - 1) * 100;
const pct = (apy) => apy.toFixed(2) + "%";
const chartColors = {
  grid: "#2c3644", text: "#8b98a9",
  line: "#4fc3f7", good: "#4caf80", warn: "#ffb74d",
};

Chart.defaults.color = chartColors.text;
Chart.defaults.borderColor = chartColors.grid;

let tradesChart, bookChart, earningsChart, anchorChart;

// ═══════════ 市場區（WebSocket）═══════════

function iqm(values) {
  if (!values.length) return 0;
  const s = [...values].sort((a, b) => a - b);
  if (s.length < 4) return s.reduce((a, b) => a + b, 0) / s.length;
  const q = Math.floor(s.length / 4);
  const mid = s.slice(q, s.length - q);
  return mid.reduce((a, b) => a + b, 0) / mid.length;
}

const market = {
  ws: null,
  chan: {},          // chanId -> "ticker" | "trades" | "book"
  trades: [],        // [{mts, rate}] 新到舊
  ticker: null,      // 原始 ticker 陣列
  book: [],          // 掛單簿快照 [[rate, period, count, amount], ...]
  dirty: false,
};

function startMarket(sym) {
  if (market.ws) {
    market.ws.onclose = null;
    market.ws.close();
  }
  Object.assign(market, { chan: {}, trades: [], ticker: null, book: [], dirty: false });

  const ws = new WebSocket(WS_URL);
  market.ws = ws;

  ws.onopen = () => {
    ws.send(JSON.stringify({ event: "subscribe", channel: "ticker", symbol: sym }));
    ws.send(JSON.stringify({ event: "subscribe", channel: "trades", symbol: sym }));
    ws.send(JSON.stringify({ event: "subscribe", channel: "book", symbol: sym,
                             prec: "P0", len: "100" }));
  };

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (!Array.isArray(msg)) {
      if (msg.event === "subscribed") market.chan[msg.chanId] = msg.channel;
      return;
    }
    const [chanId, payload, extra] = msg;
    if (payload === "hb") return;
    const channel = market.chan[chanId];

    if (channel === "ticker" && Array.isArray(payload)) {
      market.ticker = payload;
      market.dirty = true;
    } else if (channel === "trades") {
      if (Array.isArray(payload) && Array.isArray(payload[0])) {
        // 快照：[[ID, MTS, AMOUNT, RATE, PERIOD], ...]
        market.trades = payload.map((t) => ({ mts: t[1], rate: t[3] }));
      } else if (payload === "fte" && Array.isArray(extra)) {
        market.trades.unshift({ mts: extra[1], rate: extra[3] });
        market.trades = market.trades.slice(0, 250);
      }
      market.dirty = true;
    } else if (channel === "book") {
      if (Array.isArray(payload) && Array.isArray(payload[0])) {
        market.book = payload;            // 快照
      } else if (Array.isArray(payload)) {
        applyBookUpdate(payload);          // 增量更新 [RATE, PERIOD, COUNT, AMOUNT]
      }
      market.dirty = true;
    }
  };

  ws.onclose = () => setTimeout(() => startMarket($("symbolSelect").value), 3000);
  ws.onerror = () => { $("lastUpdate").textContent = "連線中斷，重連中…"; };
}

function applyBookUpdate([rate, period, count, amount]) {
  const idx = market.book.findIndex((e) => e[0] === rate && e[1] === period);
  if (count > 0) {
    if (idx >= 0) market.book[idx] = [rate, period, count, amount];
    else market.book.push([rate, period, count, amount]);
  } else if (idx >= 0) {
    market.book.splice(idx, 1);            // count=0 = 移除該價位
  }
}

function renderMarket() {
  if (!market.dirty) return;
  market.dirty = false;

  if (market.ticker) {
    // [FRR, BID, BID_PERIOD, BID_SIZE, ASK, ASK_PERIOD, ASK_SIZE, Δ, Δ%, LAST, ...]
    $("mFrr").textContent = pct(dailyToApy(market.ticker[0]));
    $("mAsk").textContent = pct(dailyToApy(market.ticker[4]));
    $("mLast").textContent = pct(dailyToApy(market.ticker[9]));
  }
  if (market.trades.length) {
    $("mIqm").textContent = pct(dailyToApy(iqm(market.trades.map((t) => t.rate))));
    const hourAgo = Date.now() - 3600_000;
    const hr = market.trades.filter((t) => t.mts >= hourAgo).map((t) => t.rate);
    $("mHigh").textContent = hr.length ? pct(dailyToApy(Math.max(...hr))) : "—";
    drawTradesChart(market.trades);
  }
  if (market.book.length) drawBookChart(market.book);
  $("lastUpdate").textContent =
    "更新 " + new Date().toLocaleTimeString("zh-TW", { hour12: false });
}

function drawTradesChart(trades) {
  const pts = trades.slice().reverse().map((t) => ({ x: t.mts, y: dailyToApy(t.rate) }));
  if (tradesChart) {
    tradesChart.data.datasets[0].data = pts;
    tradesChart.update("none");
    return;
  }
  tradesChart = new Chart($("tradesChart"), {
    type: "line",
    data: { datasets: [{ data: pts, borderColor: chartColors.line,
                         pointRadius: 0, borderWidth: 1.5, tension: 0.2 }] },
    options: {
      animation: false,
      plugins: { legend: { display: false } },
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

function drawBookChart(book) {
  // funding book：amount > 0 = 放貸方掛單（ask）
  const asks = book.filter((e) => e[3] > 0)
    .map((e) => ({ rate: e[0], amount: e[3] }))
    .sort((a, b) => a.rate - b.rate);
  let cum = 0;
  const pts = asks.map((a) => { cum += a.amount; return { x: dailyToApy(a.rate), y: cum }; });
  if (bookChart) {
    bookChart.data.datasets[0].data = pts;
    bookChart.update("none");
    return;
  }
  bookChart = new Chart($("bookChart"), {
    type: "line",
    data: { datasets: [{ data: pts, borderColor: chartColors.good,
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

function renderDashboard(d) {
  const s = d.status || {};
  $("botMode").textContent = (s.mode || "未知") + (s.paused ? "（暫停中）" : "");
  if (s.ts) {
    const age = (Date.now() - new Date(s.ts).getTime()) / 60000;
    $("botHeartbeat").textContent = age < 15
      ? `🟢 ${Math.round(age)} 分鐘前回報`
      : `🔴 已 ${Math.round(age)} 分鐘沒回報，機器人可能停了`;
  }
  const cur = "USD";
  $("dLent").textContent = (s.total_lent ?? 0).toLocaleString() + " " + cur;
  $("dApy").textContent = pct(s.weighted_apy ?? 0);
  $("dAvail").textContent = (s.available ?? 0).toLocaleString() + " " + cur;
  $("dCredits").textContent = s.credits_count ?? 0;
  $("dOffers").textContent = s.offers_count ?? 0;

  const earnings = d.earnings || [];
  const total30 = earnings.reduce((a, e) => a + (e.amount || 0), 0);
  $("dEarn30").textContent = total30.toFixed(2) + " " + cur;

  drawEarningsChart(earnings);
  drawAnchorChart(d.snapshots || []);
  renderOffers(s.offers || []);
  renderActions(d.recent_actions || []);
}

function drawEarningsChart(earnings) {
  earningsChart?.destroy();
  earningsChart = new Chart($("earningsChart"), {
    type: "bar",
    data: {
      labels: earnings.map((e) => e.date.slice(5)),
      datasets: [{ data: earnings.map((e) => e.amount),
                   backgroundColor: chartColors.good }],
    },
    options: { plugins: { legend: { display: false } } },
  });
}

function drawAnchorChart(snaps) {
  const pts = snaps.map((s) => ({ x: new Date(s.ts).getTime(), y: s.anchor_apy }));
  anchorChart?.destroy();
  anchorChart = new Chart($("anchorChart"), {
    type: "line",
    data: { datasets: [{ data: pts, borderColor: chartColors.warn,
                         pointRadius: 0, borderWidth: 1.5, tension: 0.2 }] },
    options: {
      plugins: { legend: { display: false } },
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

function renderOffers(offers) {
  const tbody = $("offersTable").querySelector("tbody");
  tbody.innerHTML = offers.length
    ? offers.map((o) => `<tr><td>$${o.amount.toLocaleString()}</td>
        <td>${pct(dailyToApy(o.rate))}</td><td>${o.period} 天</td></tr>`).join("")
    : `<tr><td colspan="3" class="muted">目前沒有掛單</td></tr>`;
}

function renderActions(actions) {
  const tbody = $("actionsTable").querySelector("tbody");
  tbody.innerHTML = actions.length
    ? actions.map((a) => {
        const t = new Date(a.ts).toLocaleString("zh-TW", { hour12: false });
        const det = a.detail
          ? `$${(a.detail.amount ?? 0).toLocaleString()} @ 年化 ${pct(dailyToApy(a.detail.rate ?? 0))}`
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
$("symbolSelect").addEventListener("change", () => startMarket($("symbolSelect").value));

startMarket("fUSD");
setInterval(renderMarket, 2000);  // 圖表最多每 2 秒重繪，避免高頻更新吃資源

const saved = localStorage.getItem(TOKEN_KEY);
if (saved) tryUnlock(saved, true);
// 個人區每 5 分鐘自動刷新
setInterval(() => {
  const t = localStorage.getItem(TOKEN_KEY);
  if (t && !$("dashPanel").classList.contains("hidden")) tryUnlock(t, true);
}, 300_000);
