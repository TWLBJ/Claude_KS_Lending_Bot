"""手動煙霧測試：打真實 Bitfinex 公開 API，確認解析正確。
用法：python tests/smoke_public_api.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lendbot.bfx_client import BfxClient

c = BfxClient()
t = c.funding_ticker("fUSD")
print(f"FRR={t.frr:.6%}/day (APR {t.frr * 365:.2%})  best_ask={t.ask:.6%}  last={t.last:.6%}")

book = c.funding_book("fUSD", length=25)
asks = [b for b in book if b.amount > 0]
bids = [b for b in book if b.amount < 0]
print(f"book: {len(asks)} asks / {len(bids)} bids, best ask = {min(a.rate for a in asks):.6%}")

trades = c.funding_trades("fUSD", limit=10)
print("recent trades:", [(f"{x.rate:.5%}", f"{x.period}d") for x in trades[:5]])
