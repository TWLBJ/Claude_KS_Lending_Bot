"""策略引擎單元測試：pytest tests/"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lendbot.bfx_client import BookEntry, FundingTicker, FundingTrade, Offer
from lendbot.strategy import (MarketView, analyze_market, apy_to_daily,
                              build_ladder, choose_period, daily_to_apy, iqm,
                              should_cancel)

NOW = 1_750_000_000_000  # 假的現在時間（毫秒）

SCFG = {
    "book_depth_usd": 1000,
    "trades_lookback": 120,
    "min_rate_apy": 3.0,
    "ladder": [
        {"weight": 0.50, "mult": 1.00},
        {"weight": 0.30, "mult": 1.15},
        {"weight": 0.20, "mult": 1.45},
    ],
    "spike_window_minutes": 15,
    "spike_mult": 1.8,
    "spike_discount": 0.95,
    "periods": [
        {"apy": 18, "days": 120},
        {"apy": 12, "days": 30},
        {"apy": 8, "days": 7},
        {"apy": 0, "days": 2},
    ],
    "stale_minutes": 10,
    "cancel_threshold": 0.05,
    "min_offer_usd": 150,
}


def make_ticker(frr=0.0003):
    return FundingTicker(frr=frr, bid=0.0002, ask=0.00025, last=0.00025,
                         high=0.0004, low=0.0002)


def make_trades(rate=0.00025, n=20, mts=NOW - 60_000):
    return [FundingTrade(mts=mts, amount=1000, rate=rate, period=2) for _ in range(n)]


def make_book():
    # asks（amount > 0）：500 @0.00020、800 @0.00022（累計 1300 > 深度目標 1000）
    return [
        BookEntry(rate=0.00020, period=2, count=3, amount=500),
        BookEntry(rate=0.00022, period=2, count=5, amount=800),
        BookEntry(rate=0.00030, period=2, count=2, amount=9000),
        BookEntry(rate=0.00019, period=2, count=1, amount=-700),  # bid，應忽略
    ]


# ── 利率換算 ──

def test_rate_conversion_roundtrip():
    r = 0.0002
    assert abs(apy_to_daily(daily_to_apy(r)) - r) < 1e-12


def test_apy_example():
    # 0.02%/天 複利一年約 7.57%
    assert 0.07 < daily_to_apy(0.0002) < 0.08


# ── IQM ──

def test_iqm_robust_to_outliers():
    vals = [0.0002] * 8 + [0.01]  # 一筆極端高利率
    assert iqm(vals) < 0.0003     # IQM 不被極端值帶跑

def test_iqm_small_sample():
    assert abs(iqm([0.1, 0.2]) - 0.15) < 1e-12

def test_iqm_empty():
    assert iqm([]) == 0.0


# ── 市場分析 ──

def test_depth_rate_and_anchor():
    view = analyze_market(make_ticker(), make_book(), make_trades(0.00025), SCFG, NOW)
    assert view.best_ask == 0.00020
    assert view.depth_rate == 0.00022          # 累計到 1000 USD 落在第二檔
    assert abs(view.trade_iqm - 0.00025) < 1e-9
    assert view.anchor == max(view.trade_iqm, view.depth_rate)
    assert not view.spike


def test_min_rate_floor():
    # 成交利率極低時，錨點被 min_rate_apy(3%) 撐住
    low_trades = make_trades(rate=0.00001)
    book = [BookEntry(rate=0.00001, period=2, count=1, amount=5000)]
    view = analyze_market(make_ticker(), book, low_trades, SCFG, NOW)
    assert view.anchor >= apy_to_daily(0.03) - 1e-12


def test_spike_detection():
    trades = make_trades(0.0002, n=30) + [
        FundingTrade(mts=NOW - 5 * 60_000, amount=5000, rate=0.0006, period=30)
    ]
    view = analyze_market(make_ticker(), make_book(), trades, SCFG, NOW)
    assert view.spike
    assert view.recent_high == 0.0006


def test_old_spike_ignored():
    # spike 視窗（15 分）以外的高利率成交不算 spike
    trades = make_trades(0.0002, n=30) + [
        FundingTrade(mts=NOW - 60 * 60_000, amount=5000, rate=0.0006, period=30)
    ]
    view = analyze_market(make_ticker(), make_book(), trades, SCFG, NOW)
    assert not view.spike


# ── 天期 ──

def test_choose_period_thresholds():
    assert choose_period(apy_to_daily(0.05), SCFG) == 2
    assert choose_period(apy_to_daily(0.09), SCFG) == 7
    assert choose_period(apy_to_daily(0.13), SCFG) == 30
    assert choose_period(apy_to_daily(0.25), SCFG) == 120


# ── 階梯掛單 ──

def view_with(anchor=0.0003, spike=False, recent_high=0.0):
    return MarketView(frr=0.0003, best_ask=0.0002, depth_rate=0.0002,
                      trade_iqm=anchor, recent_high=recent_high,
                      spike=spike, anchor=anchor)


def test_ladder_basic():
    plans = build_ladder(1000, view_with(0.0003), SCFG)
    assert len(plans) == 3
    assert [p.amount for p in plans] == [500.0, 300.0, 200.0]
    assert plans[0].rate == 0.0003
    assert abs(plans[1].rate - 0.0003 * 1.15) < 1e-9
    assert abs(plans[2].rate - 0.0003 * 1.45) < 1e-9
    assert sum(p.amount for p in plans) <= 1000


def test_ladder_below_minimum():
    assert build_ladder(100, view_with(), SCFG) == []


def test_ladder_merges_small_rungs():
    # 300 USD：50%=150 OK，30%=90 與 20%=60 太小 → 全部往後合併成第二筆
    plans = build_ladder(300, view_with(), SCFG)
    assert all(p.amount >= 150 for p in plans)
    assert abs(sum(p.amount for p in plans) - 300) < 0.01


def test_ladder_spike_chases_recent_high():
    view = view_with(0.0003, spike=True, recent_high=0.001)
    plans = build_ladder(1000, view, SCFG)
    assert abs(plans[-1].rate - 0.001 * 0.95) < 1e-9


def test_ladder_spike_uses_spike_ladder():
    cfg = {**SCFG, "spike_ladder": [
        {"weight": 0.30, "mult": 1.00},
        {"weight": 0.30, "mult": 1.20},
        {"weight": 0.40, "mult": 1.50},
    ]}
    view = view_with(0.0003, spike=True, recent_high=0.001)
    plans = build_ladder(1000, view, cfg)
    assert [p.amount for p in plans] == [300.0, 300.0, 400.0]  # 加重高利檔
    # 非 spike 時仍用一般階梯
    plans2 = build_ladder(1000, view_with(0.0003), cfg)
    assert [p.amount for p in plans2] == [500.0, 300.0, 200.0]


def test_ladder_high_rate_uses_long_period():
    view = view_with(apy_to_daily(0.20))  # 年化 20%
    plans = build_ladder(1000, view, SCFG)
    assert plans[0].period == 120


# ── 重掛 ──

def test_should_cancel_stale_and_overpriced():
    offer = Offer(id=1, symbol="fUSD", mts_created=NOW - 20 * 60_000,
                  amount=200, rate=0.0004, period=2)
    assert should_cancel(offer, view_with(0.0003), SCFG, NOW)


def test_should_not_cancel_fresh_offer():
    offer = Offer(id=1, symbol="fUSD", mts_created=NOW - 3 * 60_000,
                  amount=200, rate=0.0004, period=2)
    assert not should_cancel(offer, view_with(0.0003), SCFG, NOW)


def test_should_not_cancel_competitive_offer():
    offer = Offer(id=1, symbol="fUSD", mts_created=NOW - 60 * 60_000,
                  amount=200, rate=0.0003, period=2)
    assert not should_cancel(offer, view_with(0.0003), SCFG, NOW)
