"""研究：日 K 影線上 14%+ 的成交到底是什麼？
方法：用 5m K 線找出近 4 天的 spike 時段 → 只抓那些時段的成交 → 分析天期/金額/利率。
順便測試 candles API 支援哪些「單一天期」key（網頁 K 棒篩選用）。
用法：python research/spike_trades_study.py
"""
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lendbot.bfx_client import BfxClient
from lendbot.strategy import apy_to_daily, daily_to_apy

TZ = timezone(timedelta(hours=8))
client = BfxClient()
THRESH = apy_to_daily(0.14)  # 年化 14% 的日利率

print(f"門檻：日利率 {THRESH:.6%}（年化 14%）\n")

# ── 1) 用 5m K 線找 spike 時段 ──
candles = client.funding_candles("fUSD", tf="5m", limit=1152, sort=-1)  # 近 4 天
spikes = [c for c in candles if c["high"] >= THRESH]
print(f"近 4 天 5m K 棒中，最高價 >= 14% 年化的時段：{len(spikes)} 個")

# ── 2) 抓最近 8 個 spike 時段的成交明細 ──
all_hits = []
for c in spikes[:8]:
    time.sleep(2)
    trades = client._get_public("trades/fUSD/hist", {
        "start": c["mts"], "end": c["mts"] + 300_000, "limit": 1000, "sort": 1})
    hits = [(t[3], int(t[4]), abs(t[2])) for t in trades if t[3] >= THRESH]
    all_hits += hits
    when = datetime.fromtimestamp(c["mts"] / 1000, TZ).strftime("%m/%d %H:%M")
    if hits:
        vol = sum(h[2] for h in hits)
        rates = sorted(set(round(daily_to_apy(h[0]) * 100, 2) for h in hits))
        print(f"  {when}｜{len(hits)} 筆 >=14%，量 ${vol:,.0f}，年化範圍 {rates[0]}%~{rates[-1]}%")
    else:
        print(f"  {when}｜該 5 分鐘內抓不到 >=14% 的成交（量可能極小）")

# ── 3) 彙總：這些高利成交的天期與利率集中度 ──
if all_hits:
    total_vol = sum(h[2] for h in all_hits)
    print(f"\n>=14% 成交合計：{len(all_hits)} 筆，${total_vol:,.0f}")
    dist: dict[str, float] = {}
    for rate, period, amt in all_hits:
        key = "2天" if period <= 2 else "3-7天" if period <= 7 else "8-30天" if period <= 30 else f">30天"
        dist[key] = dist.get(key, 0) + amt
    for k, v in sorted(dist.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v / total_vol * 100:.0f}%（${v:,.0f}）")
    # 利率集中度：是不是都印在同一個值（FRR 的特徵）
    from collections import Counter
    rate_counter = Counter(round(h[0], 7) for h in all_hits)
    top_rate, top_n = rate_counter.most_common(1)[0]
    print(f"  最常見利率：{daily_to_apy(top_rate) * 100:.2f}% 年化，"
          f"占筆數 {top_n / len(all_hits) * 100:.0f}%（高度集中 = FRR 印價特徵）")

# ── 4) 測試單一天期的 K 線 key（網頁篩選用）──
print("\n=== candles key 支援測試 ===")
for key in ("trade:1h:fUSD:p2", "trade:1h:fUSD:p7", "trade:1h:fUSD:p30",
            "trade:1h:fUSD:p120", "trade:1h:fUSD:a119:p2:p120"):
    time.sleep(1.5)
    try:
        d = client._get_public(f"candles/{key}/hist", {"limit": 200})
        n_nonempty = len(d)
        print(f"  {key}: OK，{n_nonempty} 根")
    except Exception as e:
        print(f"  {key}: 失敗 {str(e)[:80]}")
