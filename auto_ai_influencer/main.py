"""命令行入口，负责启动调度器。"""

from __future__ import annotations

import argparse
from pathlib import Path
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from .caption import CaptionGenerator
from .config import AppConfig, load_config, mask_sensitive_value
from .image_source import ImageSource
from .logging_config import setup_logging
from .poster import TweetPoster, XiaohongshuPoster, PosterProtocol
from .runner import BotRunner
from .storage import Database


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""

    parser = argparse.ArgumentParser(description="AI 虚拟人账号自动运营系统")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只执行一次任务后退出")
    return parser


def _load_environment(config_path: Path) -> None:
    """加载环境变量，兼容工作目录与配置同级的 .env 文件。"""

    # 先尝试加载当前工作目录或其父级中的 .env
    load_dotenv()

    # 如果配置文件所在目录有单独的 .env，则额外加载但不覆盖已有值
    env_path = (config_path.parent / ".env").resolve()
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)


def create_components(config_path: Path) -> tuple[AppConfig, BotRunner]:
    """根据配置创建运行所需的核心组件。"""

    _load_environment(config_path)
    config = load_config(config_path)
    setup_logging(config.log_path)

    logger = logging.getLogger(__name__)
    masked_key = mask_sensitive_value(config.openai_api_key)
    if config.openai_api_key:
        logger.info("OPENAI_API_KEY 已成功加载，掩码后为：%s", masked_key)
    else:
        logger.warning("未检测到 OPENAI_API_KEY，将回退至模板文案。")

    image_source = ImageSource(config.image_directory)
    caption_generator = CaptionGenerator(config.caption, config.openai_api_key)
    posters: list[PosterProtocol] = [TweetPoster(config.twitter, config.dry_run)]
    if config.xiaohongshu.enable:
        posters.append(XiaohongshuPoster(config.xiaohongshu, config.dry_run))
    database = Database(config.database_path)

    runner = BotRunner(config, image_source, caption_generator, posters, database)
    return config, runner


def run_scheduler(config: AppConfig, runner: BotRunner) -> None:
    """根据配置启动 APScheduler。"""

    scheduler = BlockingScheduler(timezone=config.scheduler.timezone)
    scheduler.add_job(
        runner.run_once,
        "interval",
        minutes=config.scheduler.interval_minutes,
        max_instances=1,
        coalesce=True,
    )

    if config.scheduler.initial_run:
        runner.run_once()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("调度器已停止。")


def main() -> None:
    """程序入口。"""

    parser = build_parser()
    args = parser.parse_args()
    config_path = Path(args.config)

    config, runner = create_components(config_path)

    if args.once:
        runner.run_once()
    else:
        run_scheduler(config, runner)


if __name__ == "__main__":
    main()
