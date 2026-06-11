"""period_study 的 fUST 單獨版（避開限流）。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lendbot.bfx_client import BfxClient
from lendbot.strategy import daily_to_apy

client = BfxClient()
sym = "fUST"
trades = []
end = None
for _ in range(6):
    batch = client._get_public(f"trades/{sym}/hist",
                               {"limit": 1000, **({"end": end} if end else {})})
    if not batch:
        break
    trades += batch
    end = batch[-1][1] - 1
    time.sleep(2.5)

rows = sorted([(t[3], int(t[4]), abs(t[2])) for t in trades])
n = len(rows)
span = (trades[0][1] - trades[-1][1]) / 86400000

def bucket(subset, name):
    total = sum(r[2] for r in subset) or 1
    dist = {"2天": 0.0, "3-7天": 0.0, "8-30天": 0.0, ">30天": 0.0}
    for rate, period, amt in subset:
        key = "2天" if period <= 2 else "3-7天" if period <= 7 else "8-30天" if period <= 30 else ">30天"
        dist[key] += amt
    lo = daily_to_apy(subset[0][0]) * 100
    hi = daily_to_apy(subset[-1][0]) * 100
    print(f"  {name}（年化 {lo:.1f}%~{hi:.1f}%）：" +
          "｜".join(f"{k} {v / total * 100:.0f}%" for k, v in dist.items()))

print(f"=== {sym}：近 {n} 筆成交（約 {span:.1f} 天）金額加權天期分佈 ===")
q = n // 4
bucket(rows[:q], "最低 25%")
bucket(rows[q:2 * q], "25-50%  ")
bucket(rows[2 * q:3 * q], "50-75%  ")
bucket(rows[3 * q:-max(1, n // 20)], "75-95%  ")
bucket(rows[-max(1, n // 20):], "最高 5% ")
