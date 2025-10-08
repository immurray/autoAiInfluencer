"""AI 流水线调度模块。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from auto_ai_influencer.poster import TweetPoster

from .config import AIPipelineConfig
from .database import Database
from .pipeline.caption_provider import CaptionProvider
from .pipeline.image_provider import ImageProvider

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore


class PipelineScheduler:
    """负责在固定时间执行 AI 流水线任务。"""

    def __init__(
        self,
        *,
        config: AIPipelineConfig,
        image_provider: ImageProvider,
        caption_provider: CaptionProvider,
        poster: TweetPoster,
        database: Database,
    ) -> None:
        self._config = config
        self._image_provider = image_provider
        self._caption_provider = caption_provider
        self._poster = poster
        self._database = database
        self._logger = logging.getLogger(__name__)
        self._scheduler: Optional[AsyncIOScheduler] = None

    async def start(self) -> None:
        """启动调度器。"""

        if not self._config.enable:
            self._logger.info("配置未启用 AI 流水线，跳过调度器初始化。")
            return

        timezone = ZoneInfo(self._config.timezone)
        self._scheduler = AsyncIOScheduler(timezone=timezone)
        for slot in self._normalized_slots(self._config.post_slots):
            trigger = CronTrigger(hour=slot[0], minute=slot[1], timezone=timezone)
            self._scheduler.add_job(self._run_job, trigger=trigger, misfire_grace_time=3600)
            self._logger.info("已注册自动发布时段：%02d:%02d", slot[0], slot[1])

        self._scheduler.start()
        self._logger.info("AI 流水线调度器启动成功。")

    async def shutdown(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._logger.info("正在停止 AI 流水线调度器…")
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    async def run_once(self) -> None:
        """手动触发一次流水线任务。"""

        await self._run_pipeline(reason="manual")

    async def _run_job(self) -> None:
        await self._run_pipeline(reason="scheduler")

    async def _run_pipeline(self, *, reason: str) -> None:
        self._logger.info("开始执行 AI 流水线，触发来源：%s", reason)
        try:
            image_result = await asyncio.to_thread(self._image_provider.get_image)
            caption_result = await asyncio.to_thread(
                self._caption_provider.get_caption,
                image_result.path,
                self._config.caption_style,
            )
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.exception("准备素材失败：%s", exc)
            self._database.record_post(
                image_path=self._config.default_image,
                caption="",
                style=self._config.caption_style,
                post_time=self._now_iso(),
                result={"stage": "prepare", "reason": str(exc)},
                dry_run=True,
                error=str(exc),
            )
            return

        try:
            post_result = await asyncio.to_thread(
                self._poster.post,
                image_result.path,
                caption_result.text,
            )
            result_payload = {
                "tweet_id": post_result.tweet_id,
                "provider": caption_result.provider,
                "dry_run": post_result.dry_run,
                "image_source": image_result.source,
            }
            self._database.record_post(
                image_path=image_result.path,
                caption=caption_result.text,
                style=caption_result.metadata.get("style") if caption_result.metadata else None,
                post_time=self._now_iso(),
                result=result_payload,
                dry_run=post_result.dry_run,
                error=None,
            )
            self._logger.info("流水线执行完成，dry-run=%s", post_result.dry_run)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.exception("发布到 X 失败：%s", exc)
            self._database.record_post(
                image_path=image_result.path,
                caption=caption_result.text,
                style=caption_result.metadata.get("style") if caption_result.metadata else None,
                post_time=self._now_iso(),
                result={"stage": "publish", "image_source": image_result.source},
                dry_run=True,
                error=str(exc),
            )

    def _normalized_slots(self, slots: List[str]) -> List[tuple[int, int]]:
        normalized: List[tuple[int, int]] = []
        for slot in slots:
            try:
                hour_str, minute_str = slot.split(":", maxsplit=1)
                hour = max(0, min(23, int(hour_str)))
                minute = max(0, min(59, int(minute_str)))
                normalized.append((hour, minute))
            except Exception:  # pylint: disable=broad-except
                self._logger.warning("忽略非法时间配置：%s", slot)
        return normalized

    @staticmethod
    def _now_iso() -> str:
        return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    def get_overview(self) -> dict:
        """生成调度器的运行概览，方便外部查询。"""

        scheduler = self._scheduler
        jobs: List[dict] = []
        if scheduler and scheduler.running:
            for job in scheduler.get_jobs():
                jobs.append(
                    {
                        "id": job.id,
                        "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                        "trigger": str(job.trigger),
                    }
                )

        return {
            "enable": self._config.enable,
            "timezone": self._config.timezone,
            "post_slots": self._config.post_slots,
            "running": bool(scheduler and scheduler.running),
            "jobs": jobs,
        }


__all__ = ["PipelineScheduler"]
