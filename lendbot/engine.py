"""核心循環：抓市場 → 策略決策 → 撤單/掛單 → 記錄/推播。

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
        """近期最高成交利率 >= 掛單利率 → 視為成交。"""
        for o in list(self.offers):
            if view.recent_high >= o.rate > 0:
                self.offers.remove(o)
                self.credits.append(Credit(id=o.id, symbol=o.symbol,
                                           amount=o.amount, rate=o.rate,
                                           period=o.period, mts_opening=now_mts))


class Engine:
    def __init__(self, cfg: Config, client: BfxClient, store: Store, tg: TelegramBot):
        self.cfg = cfg
        self.client = client
        self.store = store
        self.tg = tg
        self.scfg = cfg.strategy

        self.paused = False
        self.first_cycle = True
        self.known_credit_ids: set[int] = set()
        self.last_view: MarketView | None = None
        self.last_spike_notify = 0.0
        self.last_earnings_sync = 0.0
        self.last_report_date = ""
        self.cycle_count = 0
        self.errors_in_row = 0

        # 模式判定
        self.has_auth = cfg.env.has_bfx_auth
        self.dry_run = cfg.env.dry_run or not self.has_auth
        self.sim = None if self.has_auth else SimAccount(balance=cfg.simulated_balance)

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
                       f"幣別：{self.cfg.symbol}｜循環：{self.cfg.cycle_minutes} 分鐘")
        log.info("啟動：%s, symbol=%s", self.mode_name, self.cfg.symbol)
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
        now_mts = int(time.time() * 1000)
        ts = now_iso()
        sym = self.cfg.symbol

        # 1) 市場數據與分析
        ticker = self.client.funding_ticker(sym)
        book = self.client.funding_book(sym, length=100)
        trades = self.client.funding_trades(sym, limit=int(self.scfg.get("trades_lookback", 120)))
        view = analyze_market(ticker, book, trades, self.scfg, now_mts)
        self.last_view = view
        log.info("市場：FRR=%s IQM=%s 深度=%s 錨點=%s%s",
                 fmt_apy(view.frr), fmt_apy(view.trade_iqm),
                 fmt_apy(view.depth_rate), fmt_apy(view.anchor),
                 " 🔥SPIKE" if view.spike else "")
        self.store.save_market_snapshot(sym, view, ts)

        # 2) spike 警報（30 分鐘冷卻）
        if view.spike and self.cfg.telegram.get("notify_spike", True):
            if time.time() - self.last_spike_notify > 1800:
                self.last_spike_notify = time.time()
                self.tg.notify(f"🔥 利率飆漲！近 15 分最高成交年化 {fmt_apy(view.recent_high)}"
                               f"（平常 {fmt_apy(view.trade_iqm)}）")

        # 3) 帳戶狀態
        available, offers, credits = self._fetch_account(view, now_mts)

        # 4) 成交偵測（第一輪只建基準不推播）
        self._detect_fills(credits)

        # 5) 撤掉過時掛單
        if not self.paused:
            freed = self._cancel_stale(offers, view, now_mts, ts)
            if freed > 0 and self.has_auth and not self.dry_run:
                time.sleep(2)  # 等餘額釋放
                available = self.client.funding_available(self.cfg.currency)
            else:
                available += freed if not self.has_auth else 0

        # 6) 階梯掛單
        if not self.paused:
            remaining_offers = [o for o in offers if not should_cancel(o, view, self.scfg, now_mts)]
            self._place_ladder(available, remaining_offers, view, now_mts, ts)

        # 7) 模擬成交（僅模擬模式）
        if self.sim is not None:
            self.sim.try_fill(view, now_mts)

        # 8) 部位快照 + 機器人狀態
        self._save_status(view, available, credits, offers, ts)

        # 9) 收益同步（每小時）+ 每日總結 + 舊資料清理（每天）
        if self.has_auth and time.time() - self.last_earnings_sync > 3600:
            self._sync_earnings()
        self._maybe_daily_report()
        if self.cycle_count % max(1, int(1440 / self.cfg.cycle_minutes)) == 1:
            self.store.prune_old()

    # ════════ 帳戶 ════════

    def _fetch_account(self, view: MarketView, now_mts: int):
        if self.sim is not None:
            return self.sim.balance, list(self.sim.offers), list(self.sim.credits)
        available = self.client.funding_available(self.cfg.currency)
        offers = self.client.active_offers(self.cfg.symbol)
        credits = self.client.active_credits(self.cfg.symbol)
        return available, offers, credits

    def _detect_fills(self, credits: list[Credit]):
        current_ids = {c.id for c in credits}
        if self.first_cycle:
            self.known_credit_ids = current_ids
            self.first_cycle = False
            return
        new_ids = current_ids - self.known_credit_ids
        self.known_credit_ids = current_ids
        if not new_ids:
            return
        for c in credits:
            if c.id in new_ids:
                self.store.log_action("fill", {
                    "id": c.id, "amount": c.amount, "rate": c.rate,
                    "period": c.period, "apy": round(daily_to_apy(c.rate) * 100, 2),
                }, now_iso())
                if self.cfg.telegram.get("notify_fills", True):
                    self.tg.notify(f"✅ 放貸成交！\n金額：{c.amount:,.2f} {self.cfg.currency}\n"
                                   f"利率：{c.rate:.6%}/天（年化 {fmt_apy(c.rate)}）\n"
                                   f"天期：{c.period} 天")

    # ════════ 動作 ════════

    def _cancel_stale(self, offers: list[Offer], view: MarketView,
                      now_mts: int, ts: str) -> float:
        freed = 0.0
        for o in offers:
            if not should_cancel(o, view, self.scfg, now_mts):
                continue
            log.info("撤單 #%s：%.2f @ %s（錨點已降到 %s）",
                     o.id, o.amount, fmt_apy(o.rate), fmt_apy(view.anchor))
            if self.dry_run:
                if self.sim is not None:
                    self.sim.cancel(o.id)
                    freed += o.amount
                self.store.log_action("cancel(dry)", {"id": o.id, "rate": o.rate,
                                                      "amount": o.amount}, ts)
                continue
            try:
                self.client.cancel_offer(o.id)
                freed += o.amount
                self.store.log_action("cancel", {"id": o.id, "rate": o.rate,
                                                 "amount": o.amount}, ts)
            except BfxError as e:
                log.warning("撤單失敗 #%s: %s", o.id, e)
        return freed

    def _place_ladder(self, available: float, existing: list[Offer],
                      view: MarketView, now_mts: int, ts: str):
        plans = build_ladder(available, view, self.scfg)
        if not plans:
            return
        for p in plans:
            desc = f"{p.amount:,.2f} @ {fmt_apy(p.rate)} / {p.period}天"
            if self.dry_run and self.sim is None:
                # 觀察模式：只記錄不下單
                log.info("[觀察] 會掛：%s", desc)
                self.store.log_action("submit(dry)", {"amount": p.amount, "rate": p.rate,
                                                      "period": p.period}, ts)
                continue
            if self.sim is not None:
                self.sim.submit(self.cfg.symbol, p, now_mts)
                log.info("[模擬] 掛單：%s", desc)
                self.store.log_action("submit(sim)", {"amount": p.amount, "rate": p.rate,
                                                      "period": p.period}, ts)
                continue
            try:
                self.client.submit_offer(self.cfg.symbol, p.amount, p.rate, p.period)
                log.info("掛單：%s", desc)
                self.store.log_action("submit", {"amount": p.amount, "rate": p.rate,
                                                 "period": p.period}, ts)
            except BfxError as e:
                log.warning("掛單失敗 %s: %s", desc, e)
                self.tg.notify(f"⚠️ 掛單失敗：{desc}\n{e}")

    def _save_status(self, view: MarketView, available: float,
                     credits: list[Credit], offers: list[Offer], ts: str):
        total = sum(c.amount for c in credits)
        wrate = (sum(c.amount * c.rate for c in credits) / total) if total else 0.0
        if credits:
            self.store.save_credits_snapshot(
                self.cfg.symbol, total, wrate, len(credits),
                [{"amount": c.amount, "rate": c.rate, "period": c.period} for c in credits], ts)
        self.store.update_bot_status({
            "ts": ts, "mode": self.mode_name, "paused": self.paused,
            "available": round(available, 2), "total_lent": round(total, 2),
            "weighted_apy": round(daily_to_apy(wrate) * 100, 2) if wrate else 0,
            "credits_count": len(credits), "offers_count": len(offers),
            "offers": [{"amount": o.amount, "rate": o.rate, "period": o.period}
                       for o in offers],
            "anchor_apy": round(daily_to_apy(view.anchor) * 100, 2),
            "frr_apy": round(view.frr * 365 * 100, 2),
            "spike": view.spike,
        })

    # ════════ 收益 ════════

    def _sync_earnings(self):
        """從 Bitfinex ledger（category 28）同步每日利息收益到 DB。"""
        self.last_earnings_sync = time.time()
        try:
            start = int((datetime.now(TZ) - timedelta(days=35)).timestamp() * 1000)
            entries = self.client.funding_earnings(self.cfg.currency, start_mts=start)
        except BfxError as e:
            log.warning("收益同步失敗: %s", e)
            return
        daily: dict[str, float] = {}
        latest_balance: dict[str, float] = {}
        for e in sorted(entries, key=lambda x: x.mts):
            d = datetime.fromtimestamp(e.mts / 1000, TZ).strftime("%Y-%m-%d")
            daily[d] = daily.get(d, 0.0) + e.amount
            latest_balance[d] = e.balance
        for d, amt in daily.items():
            self.store.save_earning(d, self.cfg.currency, amt, latest_balance.get(d))
        log.info("收益同步完成：%d 天", len(daily))

    def _earnings_summary(self) -> str:
        if not self.has_auth:
            return "模擬模式沒有真實收益紀錄"
        try:
            start = int((datetime.now(TZ) - timedelta(days=31)).timestamp() * 1000)
            entries = self.client.funding_earnings(self.cfg.currency, start_mts=start)
        except BfxError as e:
            return f"查詢失敗：{e}"
        now = datetime.now(TZ)
        sums = {1: 0.0, 7: 0.0, 30: 0.0}
        for e in entries:
            age_days = (now - datetime.fromtimestamp(e.mts / 1000, TZ)).days
            for k in sums:
                if age_days < k:
                    sums[k] += e.amount
        return (f"💰 收益（{self.cfg.currency}）\n今日：{sums[1]:.4f}\n"
                f"近 7 日：{sums[7]:.4f}\n近 30 日：{sums[30]:.4f}")

    def _maybe_daily_report(self):
        hour = int(self.cfg.telegram.get("daily_report_hour", 9))
        now = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        if now.hour == hour and self.last_report_date != today:
            self.last_report_date = today
            self.tg.notify("📊 每日報告\n" + self._earnings_summary() + "\n\n" + self._status_text())

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
        v = self.last_view
        lines = [f"🤖 {self.mode_name}{'（已暫停）' if self.paused else ''}"]
        if self.sim is not None:
            total = sum(c.amount for c in self.sim.credits)
            lines.append(f"模擬餘額：{self.sim.balance:,.2f}｜模擬放貸中：{total:,.2f}"
                         f"（{len(self.sim.credits)} 筆）｜掛單 {len(self.sim.offers)} 筆")
        elif self.last_view:
            try:
                available = self.client.funding_available(self.cfg.currency)
                credits = self.client.active_credits(self.cfg.symbol)
                offers = self.client.active_offers(self.cfg.symbol)
                total = sum(c.amount for c in credits)
                wrate = (sum(c.amount * c.rate for c in credits) / total) if total else 0
                lines.append(f"可用：{available:,.2f} {self.cfg.currency}")
                lines.append(f"放貸中：{total:,.2f}（{len(credits)} 筆，加權年化 {fmt_apy(wrate)}）")
                lines.append(f"掛單中：{len(offers)} 筆")
            except BfxError as e:
                lines.append(f"帳戶查詢失敗：{e}")
        if v:
            lines.append(f"市場錨點年化：{fmt_apy(v.anchor)}｜FRR：{fmt_apy(v.frr)}")
        lines.append(f"循環數：{self.cycle_count}")
        return "\n".join(lines)

    def _rates_text(self) -> str:
        v = self.last_view
        if not v:
            return "還沒有市場數據，請稍候"
        return (f"📈 {self.cfg.symbol} 市場利率（年化）\n"
                f"FRR：{fmt_apy(v.frr)}\n最佳掛單：{fmt_apy(v.best_ask)}\n"
                f"成交 IQM：{fmt_apy(v.trade_iqm)}\n深度利率：{fmt_apy(v.depth_rate)}\n"
                f"近期最高成交：{fmt_apy(v.recent_high)}\n"
                f"錨點：{fmt_apy(v.anchor)}{'｜🔥 SPIKE 中' if v.spike else ''}")

    def _cmd_pause(self) -> str:
        self.paused = True
        return "⏸ 已暫停掛單（既有掛單與放貸不受影響），/resume 恢復"

    def _cmd_resume(self) -> str:
        self.paused = False
        return "▶️ 已恢復自動掛單"
