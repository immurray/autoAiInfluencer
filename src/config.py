"""配置加载工具，负责兼容基础配置与 AI 流水线配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import os
import logging

from dotenv import load_dotenv

from auto_ai_influencer.config import AppConfig, load_config


_PLACEHOLDER_VALUES = {
    "xxx",
    "your_openai_key",
    "your-openai-key",
    "please_replace",
    "your_api_key",
}


logger = logging.getLogger(__name__)


@dataclass
class AIPipelineConfig:
    """AI 流水线相关配置。"""

    enable: bool = False
    post_slots: List[str] = field(default_factory=list)
    image_source: str = "local"
    prompt_template: str = ""
    caption_style: str = "default"
    openai_api_key: Optional[str] = None
    replicate_model: Optional[str] = None
    replicate_token: Optional[str] = None
    leonardo_model: Optional[str] = None
    leonardo_token: Optional[str] = None
    ready_directory: Path = Path("data/ready_to_post")
    caption_log_directory: Path = Path("logs")
    timezone: str = "Asia/Shanghai"
    default_image: Path = Path("data/ready_to_post/default_test.png")

    @property
    def is_cloud_enabled(self) -> bool:
        """判断是否允许调用云端图片生成。"""

        return self.image_source.lower() in {"replicate", "leonardo"}


def _resolve_path(base: Path, value: Optional[str], fallback: str) -> Path:
    """将配置中的路径转换为绝对路径。"""

    raw = Path(value or fallback)
    return (raw if raw.is_absolute() else (base / raw)).resolve()


def _load_raw_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def load_settings(config_path: Path) -> tuple[AppConfig, AIPipelineConfig, Dict[str, Any]]:
    """加载基础配置与 AI 流水线配置。"""

    # 加载通用 .env 与配置文件同目录下的 .env
    load_dotenv()
    env_path = (config_path.parent / ".env").resolve()
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)

    app_config = load_config(config_path)
    raw_data = _load_raw_config(config_path)

    ai_data: Dict[str, Any] = raw_data.get("ai_pipeline", {})
    base_dir = config_path.parent

    ready_directory = _resolve_path(base_dir, ai_data.get("ready_directory"), "data/ready_to_post")
    caption_log_directory = _resolve_path(base_dir, ai_data.get("caption_log_directory"), "logs")
    default_image = _resolve_path(base_dir, ai_data.get("default_image"), "data/ready_to_post/default_test.png")

    placeholder_secrets: set[str] = set()

    def _normalize_secret(value: Optional[str], *, name: str) -> Optional[str]:
        """对密钥字符串做清理，并过滤常见的占位符。"""

        if value is None:
            return None

        cleaned = value.strip()
        if not cleaned:
            return None

        lowered = cleaned.lower()
        if lowered in _PLACEHOLDER_VALUES:
            placeholder_secrets.add(name)
            logger.warning("检测到 %s 仍为占位符，请在配置或环境变量中填写有效密钥。", name)
            return None

        return cleaned

    openai_api_key = _normalize_secret(
        ai_data.get("openai_api_key")
        or os.getenv("AI_PIPELINE_OPENAI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or app_config.openai_api_key,
        name="openai_api_key",
    )
    replicate_token = _normalize_secret(
        ai_data.get("replicate_token") or os.getenv("REPLICATE_API_TOKEN"),
        name="replicate_token",
    )
    leonardo_token = _normalize_secret(
        ai_data.get("leonardo_token") or os.getenv("LEONARDO_API_TOKEN"),
        name="leonardo_token",
    )

    raw_slots = ai_data.get("post_slots", [])
    if isinstance(raw_slots, str):
        slots = [item.strip() for item in raw_slots.split(",") if item.strip()]
    else:
        slots = list(raw_slots)

    ai_config = AIPipelineConfig(
        enable=bool(ai_data.get("enable", False)),
        post_slots=slots,
        image_source=str(ai_data.get("image_source", "local")).lower(),
        prompt_template=ai_data.get("prompt_template", ""),
        caption_style=ai_data.get("caption_style", "default"),
        openai_api_key=openai_api_key,
        replicate_model=ai_data.get("replicate_model"),
        replicate_token=replicate_token,
        leonardo_model=ai_data.get("leonardo_model"),
        leonardo_token=leonardo_token,
        ready_directory=ready_directory,
        caption_log_directory=caption_log_directory,
        timezone=ai_data.get("timezone", raw_data.get("scheduler", {}).get("timezone", "Asia/Shanghai")),
        default_image=default_image,
    )

    missing_secrets: list[str] = []
    if ai_config.enable:
        if not ai_config.openai_api_key:
            missing_secrets.append("openai_api_key")
        if ai_config.is_cloud_enabled:
            if ai_config.image_source == "replicate" and not ai_config.replicate_token:
                missing_secrets.append("replicate_token")
            if ai_config.image_source == "leonardo" and not ai_config.leonardo_token:
                missing_secrets.append("leonardo_token")

    if missing_secrets:
        readable = "、".join(sorted(set(missing_secrets)))
        extra_hint = ""
        if placeholder_secrets:
            extra_hint = (
                "（检测到仍使用占位符："
                + "、".join(sorted(placeholder_secrets))
                + "）"
            )
        raise RuntimeError(
            "AI 流水线已启用，但缺少以下密钥："
            f"{readable}{extra_hint}。请在 config.json 或环境变量中填写有效值后重试。"
        )

    ready_directory.mkdir(parents=True, exist_ok=True)
    caption_log_directory.mkdir(parents=True, exist_ok=True)

    return app_config, ai_config, raw_data


__all__ = ["AIPipelineConfig", "load_settings"]
