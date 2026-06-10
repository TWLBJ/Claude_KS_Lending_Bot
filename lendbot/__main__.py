"""入口：python -m lendbot [--once]

--once：只跑一個循環就結束（測試用）
"""
import sys

from .bfx_client import BfxClient
from .config import load_config
from .engine import Engine
from .logger import get_logger
from .store import Store
from .telegram_bot import TelegramBot

log = get_logger()


def main():
    cfg = load_config()
    client = BfxClient(cfg.env.bfx_key, cfg.env.bfx_secret)
    store = Store(cfg.env.supabase_url, cfg.env.supabase_key)
    tg = TelegramBot(cfg.env.tg_token, cfg.env.tg_chat_id)
    engine = Engine(cfg, client, store, tg)

    log.info("Supabase：%s｜Telegram：%s",
             "已連接" if store.enabled else "未設定",
             "已連接" if tg.enabled else "未設定")

    if "--once" in sys.argv:
        engine.run_cycle()
        log.info("單循環測試完成")
        return
    engine.run_forever()


if __name__ == "__main__":
    main()
