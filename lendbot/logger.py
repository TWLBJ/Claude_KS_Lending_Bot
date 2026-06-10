"""統一 logger：stdout（Zeabur 會收集）。"""
import logging
import sys


if hasattr(sys.stdout, "reconfigure"):  # Windows 主控台預設 cp950，強制 UTF-8
    sys.stdout.reconfigure(encoding="utf-8")


def get_logger(name: str = "lendbot") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
