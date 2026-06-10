"""設定載入：config.yaml（策略參數）+ .env（金鑰）。"""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Env:
    bfx_key: str = ""
    bfx_secret: str = ""
    dry_run: bool = True
    tg_token: str = ""
    tg_chat_id: str = ""
    supabase_url: str = ""
    supabase_key: str = ""

    @property
    def has_bfx_auth(self) -> bool:
        return bool(self.bfx_key and self.bfx_secret)

    @property
    def has_telegram(self) -> bool:
        return bool(self.tg_token and self.tg_chat_id)

    @property
    def has_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)


@dataclass
class Config:
    env: Env = field(default_factory=Env)
    raw: dict = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return self.raw.get("symbol", "fUSD")

    @property
    def currency(self) -> str:
        return self.symbol[1:]  # fUSD -> USD

    @property
    def cycle_minutes(self) -> float:
        return float(self.raw.get("cycle_minutes", 5))

    @property
    def strategy(self) -> dict:
        return self.raw.get("strategy", {})

    @property
    def telegram(self) -> dict:
        return self.raw.get("telegram", {})

    @property
    def simulated_balance(self) -> float:
        return float(self.raw.get("dry_run", {}).get("simulated_balance", 1000))


def load_config(config_path: Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    path = config_path or ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    env = Env(
        bfx_key=os.getenv("BFX_API_KEY", "").strip(),
        bfx_secret=os.getenv("BFX_API_SECRET", "").strip(),
        dry_run=os.getenv("DRY_RUN", "true").strip().lower() != "false",
        tg_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        tg_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        supabase_url=os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
        supabase_key=os.getenv("SUPABASE_SERVICE_KEY", "").strip(),
    )
    return Config(env=env, raw=raw)
