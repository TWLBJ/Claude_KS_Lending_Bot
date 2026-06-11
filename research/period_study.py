"""研究：成交利率高低 vs 成交天期的關係。
目的：回答「高年化成交時，借款人都借幾天？」→ 決定 spike 時掛什麼天期最容易成交。
用法：python research/period_study.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lendbot.bfx_client import BfxClient
from lendbot.strategy import daily_to_apy

client = BfxClient()

for sym in ("fUSD", "fUST"):
    # 連抓多頁，湊近 10000 筆成交（約涵蓋數天）
    trades = []
    end = None
    for _ in range(10):
        params = {"limit": 1000}
        batch = client._get_public(f"trades/{sym}/hist",
                                   {"limit": 1000, **({"end": end} if end else {})})
        if not batch:
            break
        trades += batch
        end = batch[-1][1] - 1
        time.sleep(0.3)

    # [ID, MTS, AMOUNT, RATE, PERIOD]
    rows = [(t[3], int(t[4]), abs(t[2])) for t in trades]  # (rate, period, amount)
    rows.sort(key=lambda r: r[0])
    n = len(rows)
    span_days = (trades[0][1] - trades[-1][1]) / 86400000

    def bucket_stats(subset, name):
        total_amt = sum(r[2] for r in subset) or 1
        dist = {"2天": 0.0, "3-7天": 0.0, "8-30天": 0.0, ">30天": 0.0}
        for rate, period, amt in subset:
            if period <= 2:
                dist["2天"] += amt
            elif period <= 7:
                dist["3-7天"] += amt
            elif period <= 30:
                dist["8-30天"] += amt
            else:
                dist[">30天"] += amt
        apy_lo = daily_to_apy(subset[0][0]) * 100
        apy_hi = daily_to_apy(subset[-1][0]) * 100
        d = "｜".join(f"{k} {v / total_amt * 100:.0f}%" for k, v in dist.items())
        print(f"  {name}（年化 {apy_lo:.1f}%~{apy_hi:.1f}%）：{d}")

    print(f"\n=== {sym}：近 {n} 筆成交（約 {span_days:.1f} 天）依利率分組的「金額加權」天期分佈 ===")
    q = n // 4
    bucket_stats(rows[:q], "最低 25%")
    bucket_stats(rows[q:2 * q], "25-50%  ")
    bucket_stats(rows[2 * q:3 * q], "50-75%  ")
    bucket_stats(rows[3 * q:-max(1, n // 20)], "75-95%  ")
    bucket_stats(rows[-max(1, n // 20):], "最高 5% ")
