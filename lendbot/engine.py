"""核心循環：對每個幣別（fUSD/fUST...）抓市場 → 策略決策 → 撤單/掛單 → 記錄/推播。

三種模式：
1. 真實模式（有 key、DRY_RUN=false）：真正下單
2. 觀察模式（有 key、DRY_RUN=true）：讀真實帳戶，只記錄「本來會做什麼」
3. 模擬模式（無 key）：模擬餘額 + 模擬成交，本機開發測試用
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .bfx_client import BfxClient, BfxError, Credit, Offer
from .config import Config
from .logger import get_logger
from .store import Store
from .strategy import (MarketView, OfferPlan, analyze_market, build_ladder,
                       daily_to_apy, should_cancel)
from .telegram_bot import TelegramBot

log = get_logger("engine")
TZ = timezone(timedelta(hours=8))  # 台灣時間


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fmt_apy(daily_rate: float) -> str:
    return f"{daily_to_apy(daily_rate) * 100:.2f}%"


@dataclass
class SimAccount:
    """模擬帳戶（無 API key 時）：掛單若利率 <= 近期最高成交利率就視為成交。"""
    balance: float
    offers: list[Offer] = field(default_factory=list)
    credits: list[Credit] = field(default_factory=list)
    _next_id: int = 1

    def submit(self, symbol: str, plan: OfferPlan, now_mts: int):
        self.offers.append(Offer(id=self._next_id, symbol=symbol,
                                 mts_created=now_mts, amount=plan.amount,
                                 rate=plan.rate, period=plan.period))
        self._next_id += 1
        self.balance -= plan.amount

    def cancel(self, offer_id: int):
        for o in list(self.offers):
            if o.id == offer_id:
                self.offers.remove(o)
                self.balance += o.amount

    def try_fill(self, view: MarketView, now_mts: int):
        for o in list(self.offers):
            if view.recent_high >= o.rate > 0:
                self.offers.remove(o)
                self.credits.append(Credit(id=o.id, symbol=o.symbol,
                                           amount=o.amount, rate=o.rate,
                                           period=o.period, mts_opening=now_mts))


@dataclass
class SymbolState:
    """每個幣別自己的狀態。"""
    sim: SimAccount | None = None
    known_credits: dict[int, Credit] = field(default_factory=dict)
    first_cycle: bool = True
    last_view: MarketView | None = None
    last_spike_notify: float = 0.0
    last_plans: list[OfferPlan] = field(default_factory=list)
    suggestion_fp: str = ""          # 上次推播的建議掛單指紋（避免洗版）
    last_suggestion_time: float = 0.0


class Engine:
    def __init__(self, cfg: Config, client: BfxClient, store: Store, tg: TelegramBot):
        self.cfg = cfg
        self.client = client
        self.store = store
        self.tg = tg
        self.scfg = cfg.strategy

        self.paused = False
        self.last_earnings_sync = 0.0
        self.last_report_date = ""
        self.last_rebalance_notify = 0.0
        self.last_rebalance_check = 0.0
        self.ma_apy: dict[str, float] = {}  # 各幣別 30 日 MA 年化（%），給 /rates 與日報用
        self.cycle_count = 0
        self.errors_in_row = 0

        # 模式判定
        self.has_auth = cfg.env.has_bfx_auth
        self.dry_run = cfg.env.dry_run or not self.has_auth
        self.states: dict[str, SymbolState] = {
            sym: SymbolState(sim=None if self.has_auth
                             else SimAccount(balance=cfg.simulated_balance))
            for sym in cfg.symbols
        }

        self._register_commands()

    @property
    def mode_name(self) -> str:
        if not self.has_auth:
            return "模擬模式（無 API key）"
        return "觀察模式（DRY_RUN）" if self.dry_run else "真實模式"

    # ════════ 主循環 ════════

    def run_forever(self):
        self.tg.start_polling()
        self.tg.notify(f"🤖 放貸機器人啟動\n模式：{self.mode_name}\n"
                       f"幣別：{'、'.join(self.cfg.symbols)}｜循環：{self.cfg.cycle_minutes} 分鐘")
        log.info("啟動：%s, symbols=%s", self.mode_name, self.cfg.symbols)
        while True:
            try:
                self.run_cycle()
                self.errors_in_row = 0
            except Exception as e:
                self.errors_in_row += 1
                log.exception("循環錯誤（連續 %d 次）", self.errors_in_row)
                if self.errors_in_row in (3, 10):  # 別洗版，3 次與 10 次才推播
                    self.tg.notify(f"⚠️ 機器人連續 {self.errors_in_row} 次循環失敗：{e}")
            time.sleep(self.cfg.cycle_minutes * 60)

    def run_cycle(self):
        self.cycle_count += 1
        for sym, st in self.states.items():
            try:
                self._process_symbol(sym, st)
            except Exception:
                log.exception("%s 處理失敗，跳過本輪", sym)

        self._maybe_rebalance_alert()

        # 收益同步（每小時）+ 每日總結 + 舊資料清理（每天）
        if self.has_auth and time.time() - self.last_earnings_sync > 3600:
            self._sync_earnings()
        self._maybe_daily_report()
        if self.cycle_count % max(1, int(1440 / self.cfg.cycle_minutes)) == 1:
            self.store.prune_old()

    # ════════ 單一幣別處理 ════════

    def _process_symbol(self, sym: str, st: SymbolState):
        now_mts = int(time.time() * 1000)
        ts = now_iso()
        currency = sym[1:]

        # 1) 市場數據與分析
        ticker = self.client.funding_ticker(sym)
        book = self.client.funding_book(sym, length=100)
        trades = self.client.funding_trades(sym, limit=int(self.scfg.get("trades_lookback", 120)))
        view = analyze_market(ticker, book, trades, self.scfg, now_mts)
        st.last_view = view
        log.info("%s：FRR=%s IQM=%s 深度=%s 錨點=%s%s",
                 sym, fmt_apy(view.frr), fmt_apy(view.trade_iqm),
                 fmt_apy(view.depth_rate), fmt_apy(view.anchor),
                 " 🔥SPIKE" if view.spike else "")
        self.store.save_market_snapshot(sym, view, ts)

        # 2) spike 警報（每幣別 30 分鐘冷卻）
        if view.spike and self.cfg.telegram.get("notify_spike", True):
            if time.time() - st.last_spike_notify > 1800:
                st.last_spike_notify = time.time()
                self.tg.notify(f"🔥 {sym} 利率飆漲！近 15 分最高成交年化 "
                               f"{fmt_apy(view.recent_high)}（平常 {fmt_apy(view.trade_iqm)}）")

        # 3) 帳戶狀態
        if st.sim is not None:
            available, offers, credits = st.sim.balance, list(st.sim.offers), list(st.sim.credits)
        else:
            available = self.client.funding_available(currency)
            offers = self.client.active_offers(sym)
            credits = self.client.active_credits(sym)

        # 4) 成交/結束偵測（第一輪只建基準不推播）
        self._track_credits(sym, st, credits, now_mts)

        # 5) 撤掉過時掛單 + 6) 階梯掛單
        if not self.paused:
            freed = self._cancel_stale(sym, st, offers, view, now_mts, ts)
            if freed > 0 and self.has_auth and not self.dry_run:
                time.sleep(2)  # 等餘額釋放
                available = self.client.funding_available(currency)
            elif st.sim is not None:
                available = st.sim.balance
            self._place_ladder(sym, st, available, view, now_mts, ts)

        # 7) 模擬成交（僅模擬模式）/ 建議掛單推播（僅觀察模式）
        if st.sim is not None:
            st.sim.try_fill(view, now_mts)
        elif self.dry_run and not self.paused:
            self._maybe_suggest(sym, st)

        # 8) 部位快照 + 機器人狀態
        self._save_status(sym, view, available, credits, offers, ts)

    def _track_credits(self, sym: str, st: SymbolState, credits: list[Credit],
                       now_mts: int):
        """偵測新成交（出現）與放貸結束（消失）。
        結束原因判斷：實際持有 >= 天期的 98% 視為到期，否則是借款人提前還款。"""
        current = {c.id: c for c in credits}
        if st.first_cycle:
            st.known_credits = current
            st.first_cycle = False
            return
        new_ids = current.keys() - st.known_credits.keys()
        closed_ids = st.known_credits.keys() - current.keys()
        st_known, st.known_credits = st.known_credits, current

        for cid in new_ids:
            c = current[cid]
            self.store.log_action("fill", {
                "symbol": sym, "id": c.id, "amount": c.amount, "rate": c.rate,
                "period": c.period, "apy": round(daily_to_apy(c.rate) * 100, 2),
            }, now_iso())
            if self.cfg.telegram.get("notify_fills", True):
                self.tg.notify(f"✅ {sym} 放貸成交！\n金額：{c.amount:,.2f} {sym[1:]}\n"
                               f"利率：{c.rate:.6%}/天（年化 {fmt_apy(c.rate)}）\n"
                               f"天期：{c.period} 天")

        for cid in closed_ids:
            c = st_known[cid]
            held_days = (now_mts - c.mts_opening) / 86_400_000
            matured = held_days >= c.period * 0.98
            action = "closed_matured" if matured else "closed_early"
            reason = "到期歸還" if matured else "借款人提前還款"
            self.store.log_action(action, {
                "symbol": sym, "id": c.id, "amount": c.amount, "rate": c.rate,
                "apy": round(daily_to_apy(c.rate) * 100, 2), "period": c.period,
                "held_days": round(held_days, 2),
                "opened": datetime.fromtimestamp(c.mts_opening / 1000,
                                                 timezone.utc).isoformat(),
            }, now_iso())
            if self.cfg.telegram.get("notify_closes", True):
                self.tg.notify(f"💸 {sym} 放貸結束（{reason}）\n"
                               f"金額：{c.amount:,.2f} {sym[1:]}｜年化 {fmt_apy(c.rate)}\n"
                               f"持有 {held_days:.1f} / {c.period} 天")

    def _cancel_stale(self, sym: str, st: SymbolState, offers: list[Offer],
                      view: MarketView, now_mts: int, ts: str) -> float:
        freed = 0.0
        for o in offers:
            if not should_cancel(o, view, self.scfg, now_mts):
                continue
            log.info("%s 撤單 #%s：%.2f @ %s（錨點已降到 %s）",
                     sym, o.id, o.amount, fmt_apy(o.rate), fmt_apy(view.anchor))
            if self.dry_run:
                if st.sim is not None:
                    st.sim.cancel(o.id)
                    freed += o.amount
                self.store.log_action("cancel(dry)", {"symbol": sym, "id": o.id,
                                                      "rate": o.rate, "amount": o.amount}, ts)
                continue
            try:
                self.client.cancel_offer(o.id)
                freed += o.amount
                self.store.log_action("cancel", {"symbol": sym, "id": o.id,
                                                 "rate": o.rate, "amount": o.amount}, ts)
            except BfxError as e:
                log.warning("%s 撤單失敗 #%s: %s", sym, o.id, e)
        return freed

    def _place_ladder(self, sym: str, st: SymbolState, available: float,
                      view: MarketView, now_mts: int, ts: str):
        plans = build_ladder(available, view, self.scfg)
        st.last_plans = plans
        for p in plans:
            desc = f"{p.amount:,.2f} @ {fmt_apy(p.rate)} / {p.period}天"
            if self.dry_run and st.sim is None:
                log.info("[觀察] %s 會掛：%s", sym, desc)
                self.store.log_action("submit(dry)", {"symbol": sym, "amount": p.amount,
                                                      "rate": p.rate, "period": p.period}, ts)
                continue
            if st.sim is not None:
                st.sim.submit(sym, p, now_mts)
                log.info("[模擬] %s 掛單：%s", sym, desc)
                self.store.log_action("submit(sim)", {"symbol": sym, "amount": p.amount,
                                                      "rate": p.rate, "period": p.period}, ts)
                continue
            try:
                self.client.submit_offer(sym, p.amount, p.rate, p.period)
                log.info("%s 掛單：%s", sym, desc)
                self.store.log_action("submit", {"symbol": sym, "amount": p.amount,
                                                 "rate": p.rate, "period": p.period}, ts)
            except BfxError as e:
                log.warning("%s 掛單失敗 %s: %s", sym, desc, e)
                self.tg.notify(f"⚠️ {sym} 掛單失敗：{desc}\n{e}")

    def _maybe_suggest(self, sym: str, st: SymbolState):
        """觀察模式：有可用資金時，把機器人「本來會掛的單」推播給使用者手動操作。
        同樣的建議不重發；內容有感變化且距上次 >30 分鐘才再推。"""
        plans = st.last_plans
        if not plans:
            st.suggestion_fp = ""
            return
        fp = "|".join(f"{round(p.amount / 10)}@{round(p.apy_pct * 2) / 2}/{p.period}"
                      for p in plans)
        if fp == st.suggestion_fp or time.time() - st.last_suggestion_time < 1800:
            return
        st.suggestion_fp = fp
        st.last_suggestion_time = time.time()
        lines = [f"💡 {sym} 建議掛單（觀察模式，請手動到 APP 操作）："]
        for i, p in enumerate(plans, 1):
            lines.append(f"{i}. {p.amount:,.2f} {sym[1:]} @ 年化 {p.apy_pct:.2f}% / {p.period} 天")
        lines.append("（利率請換算回日利率掛 LIMIT 單；或之後切真實模式讓機器人自己掛）")
        self.tg.notify("\n".join(lines))

    def _save_status(self, sym: str, view: MarketView, available: float,
                     credits: list[Credit], offers: list[Offer], ts: str):
        total = sum(c.amount for c in credits)
        wrate = (sum(c.amount * c.rate for c in credits) / total) if total else 0.0
        if credits:
            self.store.save_credits_snapshot(
                sym, total, wrate, len(credits),
                [{"amount": c.amount, "rate": c.rate, "period": c.period} for c in credits], ts)
        now_ms = time.time() * 1000
        self.store.update_bot_status(sym, {
            "ts": ts, "mode": self.mode_name, "paused": self.paused,
            "available": round(available, 2), "total_lent": round(total, 2),
            "weighted_apy": round(daily_to_apy(wrate) * 100, 2) if wrate else 0,
            "credits_count": len(credits), "offers_count": len(offers),
            "offers": [{"amount": o.amount, "rate": o.rate, "period": o.period}
                       for o in offers],
            "credits": [{
                "amount": c.amount, "rate": c.rate, "period": c.period,
                "apy": round(daily_to_apy(c.rate) * 100, 2),
                "opened": datetime.fromtimestamp(c.mts_opening / 1000,
                                                 timezone.utc).isoformat(),
                "remaining_days": round(max(0.0, c.period
                                            - (now_ms - c.mts_opening) / 86_400_000), 1),
            } for c in sorted(credits, key=lambda x: -x.amount)],
            "suggested_offers": [{
                "amount": p.amount, "rate": p.rate,
                "apy": round(p.apy_pct, 2), "period": p.period,
            } for p in self.states[sym].last_plans] if (self.dry_run and
                                                        self.states[sym].sim is None) else [],
            "anchor_apy": round(daily_to_apy(view.anchor) * 100, 2),
            "frr_apy": round(view.frr * 365 * 100, 2),
            "spike": view.spike,
        })

    # ════════ 利差提醒（長期 MA，依 research/RESULTS.md 回測結論）════════

    def _maybe_rebalance_alert(self):
        """30 日 MA 利差連續 N 天超過門檻才提醒（每 6 小時檢查一次，計算完全無狀態）。

        回測顯示頻繁切換會被 0.2% 轉換成本吃掉（最差年化 -1.89%），
        所以門檻高、確認期長、冷卻久，3 年大約只該觸發 1 次。
        """
        rcfg = self.cfg.rebalance
        if len(self.cfg.symbols) < 2 or time.time() - self.last_rebalance_check < 6 * 3600:
            return
        self.last_rebalance_check = time.time()

        ma_days = int(rcfg.get("ma_days", 30))
        confirm = int(rcfg.get("confirm_days", 7))
        threshold = float(rcfg.get("min_diff_apy", 2.0))
        cost_pct = float(rcfg.get("switch_cost_pct", 0.2))

        # 抓兩幣別的日 K，對齊日期
        series: dict[str, dict[int, float]] = {}
        try:
            for sym in self.cfg.symbols[:2]:
                candles = self.client.funding_candles(sym, tf="1D",
                                                      limit=ma_days + confirm + 5)
                series[sym] = {c["mts"]: c["close"] for c in candles}
        except BfxError as e:
            log.warning("利差檢查抓 K 線失敗: %s", e)
            return
        sym_a, sym_b = self.cfg.symbols[:2]
        days = sorted(set(series[sym_a]) & set(series[sym_b]))
        if len(days) < ma_days + confirm:
            return

        def ma_apy(sym: str, end_idx: int) -> float:
            window = [series[sym][d] for d in days[end_idx - ma_days + 1:end_idx + 1]]
            return daily_to_apy(sum(window) / len(window)) * 100

        # 記錄最新 MA 給 /rates 與日報
        last = len(days) - 1
        self.ma_apy = {s: round(ma_apy(s, last), 2) for s in (sym_a, sym_b)}

        # 連續 confirm 天、同方向、利差都超過門檻才算成立
        diffs = [ma_apy(sym_a, last - k) - ma_apy(sym_b, last - k) for k in range(confirm)]
        if all(d > threshold for d in diffs):
            hi, lo = sym_a, sym_b
        elif all(d < -threshold for d in diffs):
            hi, lo = sym_b, sym_a
        else:
            return

        cooldown = float(rcfg.get("cooldown_days", 14)) * 86400
        if time.time() - self.last_rebalance_notify < cooldown:
            return
        self.last_rebalance_notify = time.time()
        diff_now = abs(diffs[0])
        breakeven = (cost_pct / 100) / (diff_now / 100 / 365)  # 幾天回本
        self.tg.notify(
            f"⚖️ 長期利差提醒：{hi} 的 30 日 MA 年化 {self.ma_apy[hi]:.2f}% 已連續 "
            f"{confirm} 天比 {lo}（{self.ma_apy[lo]:.2f}%）高超過 {threshold} 個百分點\n"
            f"若把資金從 {lo[1:]} 換到 {hi[1:]}：轉換成本約 {cost_pct}%，"
            f"以目前利差約 {breakeven:.0f} 天回本\n"
            f"（依回測，這種訊號 3 年只該出現約 1 次，值得認真考慮）")

    # ════════ 收益 ════════

    def _sync_earnings(self):
        """從 Bitfinex ledger（category 28）同步每日利息收益到 DB。"""
        self.last_earnings_sync = time.time()
        for sym in self.cfg.symbols:
            currency = sym[1:]
            try:
                start = int((datetime.now(TZ) - timedelta(days=35)).timestamp() * 1000)
                entries = self.client.funding_earnings(currency, start_mts=start)
            except BfxError as e:
                log.warning("%s 收益同步失敗: %s", currency, e)
                continue
            daily: dict[str, float] = {}
            latest_balance: dict[str, float] = {}
            for e in sorted(entries, key=lambda x: x.mts):
                d = datetime.fromtimestamp(e.mts / 1000, TZ).strftime("%Y-%m-%d")
                daily[d] = daily.get(d, 0.0) + e.amount
                latest_balance[d] = e.balance
            for d, amt in daily.items():
                self.store.save_earning(d, currency, amt, latest_balance.get(d))
            log.info("%s 收益同步完成：%d 天", currency, len(daily))

    def _earnings_summary(self) -> str:
        if not self.has_auth:
            return "模擬模式沒有真實收益紀錄"
        now = datetime.now(TZ)
        lines = ["💰 收益"]
        for sym in self.cfg.symbols:
            currency = sym[1:]
            try:
                start = int((now - timedelta(days=31)).timestamp() * 1000)
                entries = self.client.funding_earnings(currency, start_mts=start)
            except BfxError as e:
                lines.append(f"{currency}：查詢失敗 {e}")
                continue
            sums = {1: 0.0, 7: 0.0, 30: 0.0}
            for e in entries:
                age_days = (now - datetime.fromtimestamp(e.mts / 1000, TZ)).days
                for k in sums:
                    if age_days < k:
                        sums[k] += e.amount
            lines.append(f"{currency}｜今日 {sums[1]:.4f}｜7日 {sums[7]:.4f}｜30日 {sums[30]:.4f}")
        return "\n".join(lines)

    def _maybe_daily_report(self):
        hour = int(self.cfg.telegram.get("daily_report_hour", 9))
        now = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        if now.hour == hour and self.last_report_date != today:
            self.last_report_date = today
            ma_line = ""
            if self.ma_apy:
                ma_line = "\n30日MA年化：" + "｜".join(f"{s} {a:.2f}%"
                                                    for s, a in self.ma_apy.items())
            self.tg.notify("📊 每日報告\n" + self._earnings_summary() + ma_line
                           + "\n\n" + self._status_text())

    # ════════ Telegram 指令 ════════

    def _register_commands(self):
        self.tg.commands.update({
            "/status": self._status_text,
            "/rates": self._rates_text,
            "/earnings": self._earnings_summary,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
            "/help": lambda: ("/status 狀態總覽\n/rates 市場利率\n/earnings 收益\n"
                              "/pause 暫停掛單\n/resume 恢復掛單"),
        })

    def _status_text(self) -> str:
        lines = [f"🤖 {self.mode_name}{'（已暫停）' if self.paused else ''}"]
        for sym, st in self.states.items():
            currency = sym[1:]
            if st.sim is not None:
                total = sum(c.amount for c in st.sim.credits)
                lines.append(f"{sym}｜模擬餘額 {st.sim.balance:,.2f}｜放貸中 {total:,.2f}"
                             f"（{len(st.sim.credits)} 筆）｜掛單 {len(st.sim.offers)} 筆")
            else:
                try:
                    available = self.client.funding_available(currency)
                    credits = self.client.active_credits(sym)
                    offers = self.client.active_offers(sym)
                    total = sum(c.amount for c in credits)
                    wrate = (sum(c.amount * c.rate for c in credits) / total) if total else 0
                    lines.append(f"{sym}｜可用 {available:,.2f}｜放貸中 {total:,.2f}"
                                 f"（{len(credits)} 筆，年化 {fmt_apy(wrate)}）｜掛單 {len(offers)} 筆")
                except BfxError as e:
                    lines.append(f"{sym}｜帳戶查詢失敗：{e}")
            if st.last_view:
                lines.append(f"　└ 市場錨點 {fmt_apy(st.last_view.anchor)}")
        lines.append(f"循環數：{self.cycle_count}")
        return "\n".join(lines)

    def _rates_text(self) -> str:
        lines = ["📈 市場利率（年化）"]
        for sym, st in self.states.items():
            v = st.last_view
            if not v:
                lines.append(f"{sym}：還沒有數據")
                continue
            lines.append(f"{sym}｜FRR {fmt_apy(v.frr)}｜IQM {fmt_apy(v.trade_iqm)}｜"
                         f"錨點 {fmt_apy(v.anchor)}｜近高 {fmt_apy(v.recent_high)}"
                         f"{'｜🔥SPIKE' if v.spike else ''}")
        if self.ma_apy:
            ma_str = "｜".join(f"{s} {a:.2f}%" for s, a in self.ma_apy.items())
            lines.append(f"30日MA年化：{ma_str}")
        return "\n".join(lines)

    def _cmd_pause(self) -> str:
        self.paused = True
        return "⏸ 已暫停掛單（既有掛單與放貸不受影響），/resume 恢復"

    def _cmd_resume(self) -> str:
        self.paused = False
        return "▶️ 已恢復自動掛單"
