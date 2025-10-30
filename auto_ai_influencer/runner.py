"""整体运行调度逻辑。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Sequence
import logging

from .caption import CaptionGenerator
from .config import AppConfig
from .image_source import ImageSource
from .poster import PosterProtocol, PostResult
from .storage import Database, PostRecord


class BotRunner:
    """负责串联图片读取、文案生成与发布流程。"""

    def __init__(
        self,
        config: AppConfig,
        image_source: ImageSource,
        caption_generator: CaptionGenerator,
        posters: Sequence[PosterProtocol],
        database: Database,
    ) -> None:
        self._config = config
        self._image_source = image_source
        self._caption_generator = caption_generator
        self._posters: Sequence[PosterProtocol] = tuple(posters)
        self._database = database
        self._logger = logging.getLogger(__name__)

    def run_once(self) -> None:
        """执行一次完整的发布流程。"""

        used = self._database.get_posted_images()
        posted_count = 0
        while posted_count < self._config.max_posts_per_cycle:
            image_path = self._image_source.next_image(used)
            if image_path is None:
                break

            try:
                caption_result = self._caption_generator.generate(image_path)
                results = self._post_to_all(image_path, caption_result.text)
                if not results:
                    self._logger.warning("本轮未找到可用的发布渠道，流程提前结束。")
                    break

                for result in results:
                    record = PostRecord(
                        image_path=image_path,
                        caption=result.text,
                        posted_at=datetime.utcnow(),
                        platform=result.platform,
                        external_id=result.post_id,
                        dry_run=result.dry_run,
                    )
                    self._database.record_post(record)

                used.add(image_path)
                posted_count += 1
                self._logger.info(
                    "完成第 %d 轮发布，涉及平台：%s", posted_count, ", ".join(r.platform for r in results)
                )
            except Exception as exc:  # pylint: disable=broad-except
                self._logger.exception("发布流程出错：%s", exc)
                self._database.record_error("post", str(exc), exc)
                break

        if posted_count == 0:
            self._logger.info("本次未发布任何推文。")

    def _assemble_tweet(self, caption: str) -> str:
        """根据配置拼装最终推文。"""

        parts = [self._config.tweet.prefix.strip(), caption.strip(), self._config.tweet.suffix.strip()]
        text = " ".join([part for part in parts if part])
        if len(text) <= self._config.tweet.max_length:
            return text
        truncated = text[: self._config.tweet.max_length - 1].rstrip()
        self._logger.warning("推文超长，将自动截断。原长 %d", len(text))
        return truncated + "…"

    def _assemble_xiaohongshu(self, caption: str) -> str:
        """拼装小红书笔记正文。"""

        config = self._config.xiaohongshu
        parts = [config.prefix.strip(), caption.strip(), config.suffix.strip()]
        text = "\n".join([part for part in parts if part])
        if config.max_length > 0 and len(text) > config.max_length:
            truncated = text[: config.max_length - 1].rstrip()
            self._logger.warning("小红书笔记超长，将自动截断。原长 %d", len(text))
            return truncated + "…"
        return text

    def _post_to_all(self, image_path: Path, caption: str) -> list[PostResult]:
        """依次向所有渠道发布内容。"""

        if not self._posters:
            return []

        results: list[PostResult] = []
        for poster in self._posters:
            text = self._build_text_for_platform(poster.platform, caption)
            try:
                result = poster.post(image_path, text)
            except Exception as exc:  # pylint: disable=broad-except
                self._logger.exception("发布到 %s 失败：%s", poster.platform, exc)
                self._database.record_error(f"post:{poster.platform}", str(exc), exc)
                continue
            results.append(result)
        return results

    def _build_text_for_platform(self, platform: str, caption: str) -> str:
        """针对不同平台生成最终文案。"""

        if platform == "twitter":
            return self._assemble_tweet(caption)
        if platform == "xiaohongshu":
            return self._assemble_xiaohongshu(caption)
        return caption.strip()


__all__ = ["BotRunner"]
