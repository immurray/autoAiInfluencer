"""与推特 API 交互的发布组件。"""

from __future__ import annotations

import json
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import tweepy
from PIL import Image

from .config import TwitterCredentials


@dataclass
class PostResult:
    """推文发布后的结果。"""

    tweet_id: Optional[str]
    text: str
    dry_run: bool


class TweetPostError(RuntimeError):
    """封装推文发布阶段产生的错误，便于向上层传递人类可读的信息。"""


class TweetPoster:
    """负责将生成好的内容发布至 X(Twitter)。"""

    def __init__(self, credentials: TwitterCredentials, dry_run: bool) -> None:
        self._credentials = credentials
        self._dry_run = dry_run or not credentials.is_configured
        self._logger = logging.getLogger(__name__)
        self._client_v2: Optional[tweepy.Client] = None
        self._client_v1: Optional[tweepy.API] = None

        if not self._dry_run:
            auth = tweepy.OAuth1UserHandler(
                credentials.api_key,
                credentials.api_key_secret,
                credentials.access_token,
                credentials.access_token_secret,
            )
            self._client_v1 = tweepy.API(auth)
            self._client_v2 = tweepy.Client(
                consumer_key=credentials.api_key,
                consumer_secret=credentials.api_key_secret,
                access_token=credentials.access_token,
                access_token_secret=credentials.access_token_secret,
                bearer_token=credentials.bearer_token,
            )
        else:
            self._logger.info("未提供完整的推特凭证，系统将以 dry-run 模式运行。")

    @property
    def dry_run(self) -> bool:
        """是否处于 dry-run 模式。"""

        return self._dry_run

    def post(self, image_path: Path, text: str) -> PostResult:
        """发布推文或模拟发布。"""

        if self._dry_run:
            self._logger.info("[Dry-Run] 模拟发布推文：%s", text)
            return PostResult(tweet_id=None, text=text, dry_run=True)

        assert self._client_v1 is not None
        assert self._client_v2 is not None

        upload_path = self._ensure_supported_media(image_path)

        media_type = self._detect_media_type(upload_path)

        try:
            with upload_path.open("rb") as file_obj:
                media = self._client_v1.media_upload(
                    filename=str(upload_path),
                    file=file_obj,
                    media_type=media_type,
                )
            response = self._client_v2.create_tweet(text=text, media_ids=[media.media_id])
        except tweepy.errors.Forbidden as exc:
            detail = self._extract_twitter_error_detail(exc)
            hint = (
                "X API 返回 403 Forbidden，通常表示当前应用或访问令牌缺少写权限或媒体上传权限。"
                "请在 X Developer Portal 中确认应用权限已启用 Read 与 Write，"
                "并重新生成 Access Token / Secret。"
            )
            raise TweetPostError(f"{hint} 详细信息：{detail}") from exc
        except tweepy.TweepyException as exc:
            detail = self._extract_twitter_error_detail(exc)
            raise TweetPostError(f"调用 X API 失败：{detail}") from exc
        except Exception as exc:  # pylint: disable=broad-except
            raise TweetPostError(f"发布推文时发生未知错误：{exc}") from exc

        tweet_id = str(response.data.get("id")) if response.data else None
        self._logger.info("推文已发布，ID：%s", tweet_id)
        return PostResult(tweet_id=tweet_id, text=text, dry_run=False)

    @staticmethod
    def _extract_twitter_error_detail(exc: Exception) -> str:
        """提取 Tweepy 异常中的详细错误信息，帮助排查权限或配置问题。"""

        response = getattr(exc, "response", None)
        if response is None:
            return str(exc)

        try:
            payload = response.json()
        except Exception:  # pragma: no cover - 回退到字符串表示
            return str(exc)

        if isinstance(payload, dict):
            errors = payload.get("errors")
            details = []
            if isinstance(errors, list):
                for item in errors:
                    if not isinstance(item, dict):
                        continue
                    code = item.get("code") or item.get("title")
                    message = item.get("message") or item.get("detail")
                    if code and message:
                        details.append(f"{code}: {message}")
                    elif message:
                        details.append(str(message))
            detail_text = payload.get("detail")
            if detail_text:
                details.append(str(detail_text))
            if details:
                return "；".join(details)
            try:
                return json.dumps(payload, ensure_ascii=False)
            except Exception:  # pragma: no cover
                return str(payload)

        return str(exc)

    def _ensure_supported_media(self, image_path: Path) -> Path:
        """确保上传的图片为 X 平台支持的格式，必要时自动转码。"""

        try:
            with Image.open(image_path) as image:
                fmt = (image.format or "").upper()
                if fmt in {"PNG", "JPEG", "JPG", "GIF"}:
                    return image_path

                target = image_path.with_name(f"{image_path.stem}_twitter.png")
                self._logger.info("检测到图片格式 %s，自动转码为 PNG：%s", fmt or "unknown", target.name)

                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA")
                image.save(target, format="PNG")
                return target
        except Exception as exc:  # pylint: disable=broad-except
            raise TweetPostError(f"无法处理待上传图片：{exc}") from exc

    def _detect_media_type(self, image_path: Path) -> str:
        """推断图片的 MIME 类型，确保上传时显式声明，避免服务端识别失败。"""

        mime, _ = mimetypes.guess_type(str(image_path))
        if mime:
            return mime

        try:
            with Image.open(image_path) as image:
                fmt = (image.format or "").upper()
        except Exception as exc:  # pylint: disable=broad-except
            raise TweetPostError(f"无法识别图片 MIME 类型：{exc}") from exc

        if fmt:
            mime_from_pil = Image.MIME.get(fmt)
            if mime_from_pil:
                return mime_from_pil

        raise TweetPostError("无法识别图片的具体 MIME 类型，请检查文件格式是否受支持。")


__all__ = ["TweetPoster", "PostResult", "TweetPostError"]
