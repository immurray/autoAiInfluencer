"""配置加载与数据结构定义。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import os
import logging

_PLACEHOLDER_VALUES = {
    "xxx",
    "your_openai_key",
    "your-openai-key",
    "please_replace",
    "your_api_key",
}


logger = logging.getLogger(__name__)


def mask_sensitive_value(value: Optional[str]) -> str:
    """对敏感字符串做掩码处理，方便打印调试。"""

    if not value:
        return "未配置"
    if len(value) <= 4:
        return f"{value[0]}***"
    if len(value) <= 8:
        return f"{value[:2]}***{value[-2:]}"
    return f"{value[:4]}***{value[-4:]}"


@dataclass
class CaptionConfig:
    """文案生成相关配置。"""

    templates: List[str]
    prompt: str
    model: str


@dataclass
class TweetConfig:
    """推文内容拼装相关配置。"""

    prefix: str
    suffix: str
    max_length: int


@dataclass
class SchedulerConfig:
    """调度器相关配置。"""

    interval_minutes: int
    timezone: str
    initial_run: bool


@dataclass
class TwitterCredentials:
    """推特授权凭证。"""

    api_key: Optional[str]
    api_key_secret: Optional[str]
    access_token: Optional[str]
    access_token_secret: Optional[str]
    bearer_token: Optional[str]

    @property
    def is_configured(self) -> bool:
        """判断是否提供了完整的读写凭证。"""

        return all(
            [
                self.api_key,
                self.api_key_secret,
                self.access_token,
                self.access_token_secret,
            ]
        )


@dataclass
class AppConfig:
    """应用总配置。"""

    image_directory: Path
    database_path: Path
    log_path: Path
    dry_run: bool
    caption: CaptionConfig
    tweet: TweetConfig
    scheduler: SchedulerConfig
    twitter: TwitterCredentials
    openai_api_key: Optional[str]
    max_posts_per_cycle: int


_DEFAULT_TEMPLATES = [
    "今天的灵感来自這張圖：{filename}",
    "AI 小编上线，分享 {filename} 的精彩瞬间！",
]
_DEFAULT_PROMPT = (
    "请为一张社交媒体照片撰写不超过 100 字的中文推文文案，\n"
    "语气要友好、积极，并可适度使用 emoji。"
)


def _resolve_path(base: Path, value: str, fallback: str) -> Path:
    """将配置中的路径转化为绝对路径。"""

    raw = Path(value or fallback)
    return (raw if raw.is_absolute() else (base / raw)).resolve()


def build_app_config(data: Dict[str, Any], *, base_dir: Path) -> AppConfig:
    """根据配置字典生成 AppConfig 实例。"""

    image_directory = _resolve_path(base_dir, data.get("image_directory", ""), "images")
    database_path = _resolve_path(base_dir, data.get("database_path", ""), "data/auto_ai.db")
    log_path = _resolve_path(base_dir, data.get("log_path", ""), "data/bot.log")

    caption_data: Dict[str, Any] = data.get("caption", {})
    caption = CaptionConfig(
        templates=caption_data.get("templates") or _DEFAULT_TEMPLATES,
        prompt=caption_data.get("prompt", _DEFAULT_PROMPT),
        model=caption_data.get("model", "gpt-4o-mini"),
    )

    tweet_data: Dict[str, Any] = data.get("tweet", {})
    tweet = TweetConfig(
        prefix=tweet_data.get("prefix", ""),
        suffix=tweet_data.get("suffix", ""),
        max_length=int(tweet_data.get("max_length", 280)),
    )

    scheduler_data: Dict[str, Any] = data.get("scheduler", {})
    scheduler = SchedulerConfig(
        interval_minutes=int(scheduler_data.get("interval_minutes", data.get("post_interval_minutes", 60))),
        timezone=scheduler_data.get("timezone", "Asia/Shanghai"),
        initial_run=bool(scheduler_data.get("initial_run", True)),
    )

    dry_run = bool(data.get("dry_run", False))
    max_posts_per_cycle = int(data.get("max_posts_per_cycle", 1))

    twitter = TwitterCredentials(
        api_key=os.getenv("TWITTER_API_KEY"),
        api_key_secret=os.getenv("TWITTER_API_SECRET"),
        access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
        access_token_secret=os.getenv("TWITTER_ACCESS_TOKEN_SECRET"),
        bearer_token=os.getenv("TWITTER_BEARER_TOKEN"),
    )

    raw_openai_key = os.getenv("OPENAI_API_KEY")
    if raw_openai_key and raw_openai_key.strip().lower() in _PLACEHOLDER_VALUES:
        logger.warning("检测到 OPENAI_API_KEY 仍为占位符，请设置有效密钥后再运行。")
        raw_openai_key = ""
    openai_api_key = raw_openai_key.strip() if raw_openai_key else None
    # 打印关键提示，帮助确认环境变量是否生效
    masked_key = mask_sensitive_value(openai_api_key)
    print(f"[配置] OPENAI_API_KEY 读取结果：{masked_key}")

    return AppConfig(
        image_directory=image_directory,
        database_path=database_path,
        log_path=log_path,
        dry_run=dry_run,
        caption=caption,
        tweet=tweet,
        scheduler=scheduler,
        twitter=twitter,
        openai_api_key=openai_api_key,
        max_posts_per_cycle=max_posts_per_cycle,
    )


def load_config(path: Path) -> AppConfig:
    """从 JSON 文件与环境变量组合加载配置。"""

    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在：{path}")

    with path.open("r", encoding="utf-8") as fp:
        data: Dict[str, Any] = json.load(fp)

    return build_app_config(data, base_dir=path.parent)


__all__ = [
    "AppConfig",
    "CaptionConfig",
    "TweetConfig",
    "SchedulerConfig",
    "TwitterCredentials",
    "load_config",
    "build_app_config",
    "mask_sensitive_value",
]
