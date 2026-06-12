"""撈最近的機器人動作 + 目前帳戶狀態（維運工具）。
用法：python tools/recent_actions.py [筆數]
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lendbot.bfx_client import BfxClient
from lendbot.config import load_config
from lendbot.strategy import daily_to_apy

TZ = timezone(timedelta(hours=8))
cfg = load_config()
limit = int(sys.argv[1]) if len(sys.argv) > 1 else 30

headers = {"apikey": cfg.env.supabase_key,
           "Authorization": f"Bearer {cfg.env.supabase_key}"}
r = requests.get(f"{cfg.env.supabase_url}/rest/v1/actions_log",
                 params={"order": "ts.desc", "limit": limit}, headers=headers, timeout=10)

print("=== 最近動作（台灣時間）===")
for a in reversed(r.json()):
    t = datetime.fromisoformat(a["ts"].replace("Z", "+00:00")).astimezone(TZ)
    d = a.get("detail") or {}
    apy = d.get("apy") or (daily_to_apy(d["rate"]) * 100 if d.get("rate") else 0)
    extra = f"｜持有 {d['held_days']} 天" if "held_days" in d else ""
    print(f"{t:%m/%d %H:%M}｜{a['action']:<16}｜{d.get('symbol', ''):<5} "
          f"${d.get('amount', 0):>9,.2f} @ {apy:5.2f}% / {d.get('period', '?')}天{extra}")

print("\n=== 目前帳戶 ===")
client = BfxClient(cfg.env.bfx_key, cfg.env.bfx_secret)
for sym in cfg.symbols:
    cur = sym[1:]
    avail = client.funding_available(cur)
    offers = client.active_offers(sym)
    credits = client.active_credits(sym)
    print(f"{sym}｜可用 ${avail:,.2f}｜掛單 {len(offers)} 筆｜放貸中 {len(credits)} 筆")
    for o in offers:
        print(f"   掛單 ${o.amount:,.2f} @ {daily_to_apy(o.rate)*100:.2f}% / {o.period}天"
              f"（掛於 {datetime.fromtimestamp(o.mts_created/1000, TZ):%H:%M}）")
    for c in credits:
        print(f"   放貸 ${c.amount:,.2f} @ {daily_to_apy(c.rate)*100:.2f}% / {c.period}天")
