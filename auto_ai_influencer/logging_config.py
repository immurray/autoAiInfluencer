"""统一的日志配置。"""

from __future__ import annotations

from pathlib import Path
import logging


def setup_logging(log_path: Path) -> None:
    """初始化日志，输出到控制台与文件。"""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handlers = []

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    handlers.append(console)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    handlers.append(file_handler)

    logging.basicConfig(level=logging.INFO, handlers=handlers)


__all__ = ["setup_logging"]
