"""Supabase 寫入層：直接用 PostgREST API（不依賴 supabase-py，少一層依賴）。

沒設定 SUPABASE_URL/KEY 時全部變 no-op，本機純測試也能跑。
寫入失敗只記 log 不中斷主循環（DB 掛了不能影響放貸）。
"""
from __future__ import annotations

import requests

from .logger import get_logger

log = get_logger("store")
TIMEOUT = 10


class Store:
    def __init__(self, url: str = "", service_key: str = ""):
        self.enabled = bool(url and service_key)
        self.base = f"{url}/rest/v1" if url else ""
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, table: str, *, json=None, params=None,
                 extra_headers: dict | None = None) -> bool:
        if not self.enabled:
            return False
        headers = {**self.headers, **(extra_headers or {})}
        try:
            r = requests.request(method, f"{self.base}/{table}", headers=headers,
                                 json=json, params=params, timeout=TIMEOUT)
            if r.status_code >= 300:
                log.warning("supabase %s %s -> %s: %s", method, table,
                            r.status_code, r.text[:200])
                return False
            return True
        except requests.RequestException as e:
            log.warning("supabase %s %s 連線失敗: %s", method, table, e)
            return False

    def insert(self, table: str, row: dict) -> bool:
        return self._request("POST", table, json=row)

    def upsert(self, table: str, row: dict, on_conflict: str) -> bool:
        return self._request(
            "POST", table, json=row, params={"on_conflict": on_conflict},
            extra_headers={"Prefer": "resolution=merge-duplicates"})

    # ── 業務方法 ──

    def save_market_snapshot(self, symbol: str, view, ts_iso: str) -> bool:
        from .strategy import daily_to_apy
        return self.insert("market_snapshots", {
            "ts": ts_iso, "symbol": symbol,
            "frr": view.frr, "best_ask": view.best_ask,
            "depth_rate": view.depth_rate, "trade_iqm": view.trade_iqm,
            "recent_high": view.recent_high, "spike": view.spike,
            "anchor": view.anchor,
            "anchor_apy": round(daily_to_apy(view.anchor) * 100, 4),
        })

    def log_action(self, action: str, detail: dict, ts_iso: str) -> bool:
        return self.insert("actions_log", {"ts": ts_iso, "action": action, "detail": detail})

    def save_credits_snapshot(self, symbol: str, total: float, weighted_rate: float,
                              count: int, details: list, ts_iso: str) -> bool:
        from .strategy import daily_to_apy
        return self.insert("credits_snapshots", {
            "ts": ts_iso, "symbol": symbol, "total_lent": round(total, 2),
            "weighted_rate": weighted_rate,
            "weighted_apy": round(daily_to_apy(weighted_rate) * 100, 4) if weighted_rate else 0,
            "count": count, "details": details,
        })

    def save_earning(self, date_str: str, currency: str, amount: float,
                     balance: float | None = None) -> bool:
        row = {"date": date_str, "currency": currency, "amount": round(amount, 6)}
        if balance is not None:
            row["balance"] = round(balance, 2)
        return self.upsert("earnings", row, on_conflict="date,currency")

    def update_bot_status(self, status: dict) -> bool:
        return self.upsert("bot_status", {"id": 1, **status}, on_conflict="id")

    def prune_old(self, days_snapshots: int = 30, days_actions: int = 90) -> None:
        """清掉過舊資料，避免免費額度爆掉。"""
        from datetime import datetime, timedelta, timezone
        cut_snap = (datetime.now(timezone.utc) - timedelta(days=days_snapshots)).isoformat()
        cut_act = (datetime.now(timezone.utc) - timedelta(days=days_actions)).isoformat()
        self._request("DELETE", "market_snapshots", params={"ts": f"lt.{cut_snap}"})
        self._request("DELETE", "credits_snapshots", params={"ts": f"lt.{cut_snap}"})
        self._request("DELETE", "actions_log", params={"ts": f"lt.{cut_act}"})
