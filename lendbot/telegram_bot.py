"""Telegram 推播 + 指令處理。

推播：engine 呼叫 notify()。
指令：背景執行緒 long-polling getUpdates，只回應設定的 chat_id。
沒設定 token 時全部變 no-op。
"""
from __future__ import annotations

import threading
import time
from typing import Callable

import requests

from .logger import get_logger

log = get_logger("telegram")
TIMEOUT = 35


class TelegramBot:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.enabled = bool(token and chat_id)
        self.base = f"https://api.telegram.org/bot{token}" if token else ""
        self.chat_id = chat_id
        self._offset = 0
        # 指令 -> 回覆文字的函式，由 engine 註冊
        self.commands: dict[str, Callable[[], str]] = {}

    # ── 推播 ──

    def notify(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            r = requests.post(f"{self.base}/sendMessage", json={
                "chat_id": self.chat_id, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }, timeout=10)
            if r.status_code != 200:
                log.warning("sendMessage 失敗 %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except requests.RequestException as e:
            log.warning("sendMessage 連線失敗: %s", e)
            return False

    # ── 指令輪詢 ──

    def start_polling(self):
        if not self.enabled:
            return
        t = threading.Thread(target=self._poll_loop, daemon=True, name="tg-poll")
        t.start()
        log.info("Telegram 指令輪詢已啟動")

    def _poll_loop(self):
        while True:
            try:
                r = requests.get(f"{self.base}/getUpdates", params={
                    "offset": self._offset + 1, "timeout": 30,
                }, timeout=TIMEOUT)
                if r.status_code != 200:
                    time.sleep(5)
                    continue
                for upd in r.json().get("result", []):
                    self._offset = max(self._offset, upd["update_id"])
                    self._handle(upd)
            except requests.RequestException:
                time.sleep(5)
            except Exception as e:  # 輪詢執行緒不能死
                log.error("telegram poll 例外: %s", e)
                time.sleep(5)

    def _handle(self, upd: dict):
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if chat_id != str(self.chat_id) or not text.startswith("/"):
            return  # 只理會自己的 chat
        cmd = text.split()[0].split("@")[0].lower()
        handler = self.commands.get(cmd)
        if handler:
            try:
                self.notify(handler())
            except Exception as e:
                self.notify(f"指令執行錯誤：{e}")
        else:
            self.notify("未知指令，輸入 /help 看可用指令")
