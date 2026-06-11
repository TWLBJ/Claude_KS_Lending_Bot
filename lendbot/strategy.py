"""策略引擎：全部是純函式（輸入→輸出），不打 API、不碰 DB，方便單元測試。

利率單位約定：全程使用 Bitfinex 的「日利率」（如 0.0002 = 0.02%/天），
只在顯示與天期判斷時換算年化。
"""
from __future__ import annotations

from dataclasses import dataclass

from .bfx_client import BookEntry, FundingTicker, FundingTrade, Offer


# ── 利率換算 ──────────────────────────────────────────────

def daily_to_apy(rate: float) -> float:
    """日利率 → 年化（複利）。0.0002 → 約 0.0758 (7.58%)"""
    return (1 + rate) ** 365 - 1


def apy_to_daily(apy: float) -> float:
    """年化（複利）→ 日利率。"""
    return (1 + apy) ** (1 / 365) - 1


def iqm(values: list[float]) -> float:
    """四分位距內平均（Interquartile Mean）：去掉最低/最高各 25% 後取平均。
    比平均值抗極端值，比中位數平滑。樣本 < 4 時退化為一般平均。"""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n < 4:
        return sum(s) / n
    q = n // 4
    mid = s[q:n - q]
    return sum(mid) / len(mid)


# ── 市場分析 ──────────────────────────────────────────────

@dataclass
class MarketView:
    frr: float           # Flash Return Rate（參考用）
    best_ask: float      # 掛單簿最低放貸利率（隊伍最前面）
    depth_rate: float    # 前 N 美元深度處的利率
    trade_iqm: float     # 近期成交 IQM（主要錨點）
    recent_high: float   # spike 視窗內最高成交利率
    spike: bool          # 是否偵測到利率飆漲
    anchor: float        # 最終錨點利率（階梯的基準）
    rate_floor: float = 0.0  # 24 小時行情保底（避免短暫低迷掛太低）


def analyze_market(ticker: FundingTicker, book: list[BookEntry],
                   trades: list[FundingTrade], scfg: dict,
                   now_mts: int, recent_closes: list[float] | None = None) -> MarketView:
    # 1) 掛單簿：累計 ask 深度到 book_depth_usd，該處利率 = 要排進隊伍前段的利率
    depth_target = float(scfg.get("book_depth_usd", 300_000))
    asks = sorted((b for b in book if b.amount > 0), key=lambda b: b.rate)
    cum, depth_rate = 0.0, (asks[-1].rate if asks else 0.0)
    for a in asks:
        cum += a.amount
        if cum >= depth_target:
            depth_rate = a.rate
            break
    best_ask = asks[0].rate if asks else 0.0

    # 2) 成交 IQM 錨點
    lookback = int(scfg.get("trades_lookback", 120))
    rates = [t.rate for t in trades[:lookback]]
    anchor_iqm = iqm(rates)

    # 3) spike 偵測：近 N 分鐘最高成交 vs IQM
    window_mts = now_mts - int(scfg.get("spike_window_minutes", 15)) * 60_000
    recent = [t.rate for t in trades if t.mts >= window_mts]
    recent_high = max(recent, default=0.0)
    spike = bool(anchor_iqm) and recent_high > anchor_iqm * float(scfg.get("spike_mult", 1.8))

    # 4) 行情保底：近 24 小時 1h K 收盤的第 P 百分位。
    #    成交 IQM 只涵蓋幾分鐘，市場短暫低迷時會把階梯整組拉低，
    #    低利成交一卡就是 2 天 —— 用較長期的行情撐住下限（掛太高頂多晚點成交）。
    rate_floor = 0.0
    if recent_closes:
        s = sorted(recent_closes)
        k = int(len(s) * float(scfg.get("floor_percentile", 25)) / 100)
        rate_floor = s[min(k, len(s) - 1)]

    # 5) 錨點 = max(成交IQM, 深度利率, 行情保底, 最低利率底線)
    min_rate = apy_to_daily(float(scfg.get("min_rate_apy", 3.0)) / 100)
    anchor = max(anchor_iqm, depth_rate, rate_floor, min_rate)

    return MarketView(frr=ticker.frr, best_ask=best_ask, depth_rate=depth_rate,
                      trade_iqm=anchor_iqm, recent_high=recent_high,
                      spike=spike, anchor=anchor, rate_floor=rate_floor)


# ── 天期選擇 ──────────────────────────────────────────────

def choose_period(rate: float, scfg: dict) -> int:
    """利率年化越高 → 鎖越長天期。periods 設定由年化門檻大到小判斷。"""
    apy_pct = daily_to_apy(rate) * 100
    periods = sorted(scfg.get("periods", [{"apy": 0, "days": 2}]),
                     key=lambda p: -float(p["apy"]))
    for p in periods:
        if apy_pct >= float(p["apy"]):
            return int(p["days"])
    return 2


# ── 階梯掛單 ──────────────────────────────────────────────

@dataclass
class OfferPlan:
    amount: float
    rate: float          # 日利率
    period: int

    @property
    def apy_pct(self) -> float:
        return daily_to_apy(self.rate) * 100


def build_ladder(available: float, view: MarketView, scfg: dict) -> list[OfferPlan]:
    """把可用資金按 ladder 設定拆成多檔。不足最小掛單額的檔位往前一檔合併。
    偵測到 spike 時改用 spike_ladder（加重高利率檔位）。"""
    min_offer = float(scfg.get("min_offer_usd", 150))
    if available < min_offer:
        return []

    ladder = scfg.get("ladder", [{"weight": 1.0, "mult": 1.0}])
    if view.spike and scfg.get("spike_ladder"):
        ladder = scfg["spike_ladder"]
    rungs: list[OfferPlan] = []
    for i, rung in enumerate(ladder):
        amount = available * float(rung["weight"])
        rate = view.anchor * float(rung["mult"])
        # spike 時最後一檔改追近期最高成交利率
        if view.spike and i == len(ladder) - 1:
            rate = max(rate, view.recent_high * float(scfg.get("spike_discount", 0.95)))
        rungs.append(OfferPlan(amount=round(amount, 2), rate=round(rate, 8),
                               period=choose_period(rate, scfg)))

    # 太小的檔位由低利率往高利率合併（優先保住容易成交的低檔）
    merged: list[OfferPlan] = []
    carry = 0.0
    for plan in rungs:
        amt = plan.amount + carry
        if amt < min_offer:
            carry = amt
            continue
        merged.append(OfferPlan(amount=round(amt, 2), rate=plan.rate, period=plan.period))
        carry = 0.0
    if carry >= min_offer and merged:
        last = merged[-1]
        merged[-1] = OfferPlan(amount=round(last.amount + carry, 2),
                               rate=last.rate, period=last.period)
    if not merged and available >= min_offer:
        merged = [OfferPlan(amount=round(available, 2), rate=rungs[0].rate,
                            period=rungs[0].period)]
    return merged


# ── 重掛判斷 ──────────────────────────────────────────────

def should_cancel(offer: Offer, view: MarketView, scfg: dict, now_mts: int) -> bool:
    """掛太久沒成交且利率明顯高於目前錨點 → 撤單重掛，減少資金閒置。"""
    age_minutes = (now_mts - offer.mts_created) / 60_000
    if age_minutes < float(scfg.get("stale_minutes", 10)):
        return False
    threshold = 1 + float(scfg.get("cancel_threshold", 0.05))
    return offer.rate > view.anchor * threshold
