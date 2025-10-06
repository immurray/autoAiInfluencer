"""本地图片轮询逻辑。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Set
import logging


class ImageSource:
    """负责在指定目录中查找待发布图片。"""

    _SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

    def __init__(self, root: Path) -> None:
        self._root = root
        self._logger = logging.getLogger(__name__)

    def list_images(self) -> Iterable[Path]:
        """列出目录下所有受支持的图片路径。"""

        if not self._root.exists():
            self._logger.warning("图片目录不存在：%s", self._root)
            return []
        return sorted(
            [p for p in self._root.iterdir() if p.is_file() and p.suffix.lower() in self._SUPPORTED_SUFFIXES]
        )

    def next_image(self, used_paths: Set[Path]) -> Optional[Path]:
        """返回一张尚未使用过的图片。"""

        for path in self.list_images():
            if path not in used_paths:
                return path
        self._logger.info("没有新的图片可用于发布。")
        return None


__all__ = ["ImageSource"]
