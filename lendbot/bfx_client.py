"""Bitfinex REST API v2 客戶端。

公開 API：api-pub.bitfinex.com（不用金鑰）
私有 API：api.bitfinex.com（HMAC-SHA384 簽名）

回傳格式皆為 Bitfinex 的陣列格式，這裡轉成 dataclass 方便使用。
官方文件：https://docs.bitfinex.com/docs/rest-general
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass

import requests

from .logger import get_logger

log = get_logger("bfx")

PUB_BASE = "https://api-pub.bitfinex.com/v2"
AUTH_BASE = "https://api.bitfinex.com"
TIMEOUT = 15


class BfxError(Exception):
    pass


# ── 資料結構 ──────────────────────────────────────────────

@dataclass
class FundingTicker:
    frr: float            # Flash Return Rate（日利率）
    bid: float            # 最高借入需求利率
    ask: float            # 最低放貸掛單利率
    last: float           # 最近成交利率
    high: float
    low: float


@dataclass
class BookEntry:
    rate: float           # 日利率
    period: int           # 天期
    count: int
    amount: float         # funding book：>0 = ask（放貸方）、<0 = bid（借款方）


@dataclass
class FundingTrade:
    mts: int              # 成交時間（毫秒）
    amount: float
    rate: float           # 日利率
    period: int


@dataclass
class Offer:
    """我的掛單中訂單。"""
    id: int
    symbol: str
    mts_created: int
    amount: float
    rate: float
    period: int


@dataclass
class Credit:
    """放貸中部位（已借出）。"""
    id: int
    symbol: str
    amount: float
    rate: float
    period: int
    mts_opening: int


@dataclass
class ClosedCredit:
    """已結束的放貸（歷史）。"""
    id: int
    symbol: str
    amount: float
    rate: float
    period: int
    mts_opening: int
    mts_close: int


@dataclass
class LedgerEntry:
    """帳本紀錄（category 28 = 放貸利息收入）。"""
    id: int
    currency: str
    mts: int
    amount: float
    balance: float
    description: str


# ── 客戶端 ──────────────────────────────────────────────

class BfxClient:
    def __init__(self, api_key: str = "", api_secret: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = requests.Session()

    # ---------- 公開 API ----------

    def _get_public(self, path: str, params: dict | None = None):
        for attempt in range(3):
            r = self.session.get(f"{PUB_BASE}/{path}", params=params, timeout=TIMEOUT)
            if r.status_code == 429:  # 限流：退避後重試
                log.warning("public %s 限流，%d 秒後重試", path, 20 * (attempt + 1))
                time.sleep(20 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise BfxError(f"public {path} -> {r.status_code}: {r.text[:200]}")
            return r.json()
        raise BfxError(f"public {path} 連續限流，放棄")

    def funding_ticker(self, symbol: str) -> FundingTicker:
        d = self._get_public(f"ticker/{symbol}")
        return FundingTicker(frr=d[0], bid=d[1], ask=d[4],
                             last=d[9], high=d[11], low=d[12])

    def funding_book(self, symbol: str, precision: str = "P0", length: int = 100) -> list[BookEntry]:
        d = self._get_public(f"book/{symbol}/{precision}", {"len": length})
        return [BookEntry(rate=e[0], period=int(e[1]), count=int(e[2]), amount=e[3]) for e in d]

    def funding_trades(self, symbol: str, limit: int = 120) -> list[FundingTrade]:
        d = self._get_public(f"trades/{symbol}/hist", {"limit": limit})
        return [FundingTrade(mts=int(t[1]), amount=t[2], rate=t[3], period=int(t[4])) for t in d]

    def funding_candles(self, symbol: str, tf: str = "1D", limit: int = 1100,
                        start_mts: int | None = None, sort: int = 1) -> list[dict]:
        """歷史利率 K 線（聚合 2-30 天期）。回傳由舊到新：
        [{mts, open, close, high, low, volume}, ...]，利率為日利率。"""
        key = f"trade:{tf}:{symbol}:a30:p2:p30"
        params: dict = {"limit": limit, "sort": sort}
        if start_mts:
            params["start"] = start_mts
        d = self._get_public(f"candles/{key}/hist", params)
        return [{"mts": int(c[0]), "open": c[1], "close": c[2],
                 "high": c[3], "low": c[4], "volume": c[5]} for c in d]

    # ---------- 私有 API（HMAC 簽名）----------

    def _post_auth(self, path: str, body: dict | None = None):
        if not (self.api_key and self.api_secret):
            raise BfxError("缺少 API key/secret，無法呼叫私有 API")
        raw_body = json.dumps(body or {})
        nonce = str(int(time.time() * 1_000_000))
        sig_payload = f"/api/v2/{path}{nonce}{raw_body}"
        signature = hmac.new(self.api_secret.encode(), sig_payload.encode(),
                             hashlib.sha384).hexdigest()
        headers = {
            "bfx-nonce": nonce,
            "bfx-apikey": self.api_key,
            "bfx-signature": signature,
            "content-type": "application/json",
        }
        r = self.session.post(f"{AUTH_BASE}/v2/{path}", headers=headers,
                              data=raw_body, timeout=TIMEOUT)
        if r.status_code != 200:
            raise BfxError(f"auth {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def funding_available(self, currency: str) -> float:
        """funding 錢包可用餘額。"""
        wallets = self._post_auth("auth/r/wallets")
        for w in wallets:
            # [WALLET_TYPE, CURRENCY, BALANCE, UNSETTLED_INTEREST, AVAILABLE_BALANCE, ...]
            if w[0] == "funding" and w[1] == currency:
                return float(w[4] if w[4] is not None else w[2])
        return 0.0

    def active_offers(self, symbol: str) -> list[Offer]:
        d = self._post_auth(f"auth/r/funding/offers/{symbol}")
        return [Offer(id=int(o[0]), symbol=o[1], mts_created=int(o[2]),
                      amount=float(o[4]), rate=float(o[14]), period=int(o[15]))
                for o in d]

    def active_credits(self, symbol: str) -> list[Credit]:
        d = self._post_auth(f"auth/r/funding/credits/{symbol}")
        return [Credit(id=int(c[0]), symbol=c[1], amount=float(c[5]),
                       rate=float(c[11]), period=int(c[12]), mts_opening=int(c[13]))
                for c in d]

    def credits_history(self, symbol: str, limit: int = 25) -> list[ClosedCredit]:
        """已結束的放貸歷史（解掉輪詢盲區：成交後快速歸還的單也查得到）。"""
        d = self._post_auth(f"auth/r/funding/credits/{symbol}/hist", {"limit": limit})
        return [ClosedCredit(id=int(c[0]), symbol=c[1], amount=float(c[5]),
                             rate=float(c[11]), period=int(c[12]),
                             mts_opening=int(c[13] or c[3]), mts_close=int(c[4] or 0))
                for c in d]

    def submit_offer(self, symbol: str, amount: float, rate: float, period: int) -> dict:
        body = {"type": "LIMIT", "symbol": symbol,
                "amount": f"{amount:.6f}", "rate": f"{rate:.8f}", "period": period}
        d = self._post_auth("auth/w/funding/offer/submit", body)
        # 回傳 notification：[MTS, TYPE, MESSAGE_ID, null, OFFER_ARRAY, CODE, STATUS, TEXT]
        status, text = d[6], d[7]
        if status != "SUCCESS":
            raise BfxError(f"掛單失敗：{status} {text}")
        return {"status": status, "text": text}

    def cancel_offer(self, offer_id: int) -> dict:
        d = self._post_auth("auth/w/funding/offer/cancel", {"id": offer_id})
        status, text = d[6], d[7]
        if status != "SUCCESS":
            raise BfxError(f"撤單失敗：{status} {text}")
        return {"status": status, "text": text}

    def funding_earnings(self, currency: str, start_mts: int | None = None,
                         limit: int = 500) -> list[LedgerEntry]:
        """放貸利息收入紀錄（ledger category 28 = Margin Funding Payment）。"""
        body: dict = {"category": 28, "limit": limit}
        if start_mts:
            body["start"] = start_mts
        d = self._post_auth(f"auth/r/ledgers/{currency}/hist", body)
        return [LedgerEntry(id=int(e[0]), currency=e[1], mts=int(e[3]),
                            amount=float(e[5]), balance=float(e[6]),
                            description=str(e[8] or ""))
                for e in d]
