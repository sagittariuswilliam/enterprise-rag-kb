"""日志配置"""

import logging
import sys

from app.config import settings

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(level: str | None = None) -> None:
    """初始化全局日志（幂等）"""
    global _configured
    if _configured:
        return

    log_level = (level or settings.log_level).upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    root.addHandler(handler)

    # 降噪
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """获取带命名空间的 logger"""
    setup_logging()
    return logging.getLogger(name)
