"""与推特 API 交互的发布组件。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import logging

import tweepy

from .config import TwitterCredentials


@dataclass
class PostResult:
    """推文发布后的结果。"""

    tweet_id: Optional[str]
    text: str
    dry_run: bool


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

        try:
            media = self._client_v1.media_upload(filename=str(image_path))
            response = self._client_v2.create_tweet(text=text, media_ids=[media.media_id])
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.exception("推文发布失败：%s", exc)
            raise

        tweet_id = str(response.data.get("id")) if response.data else None
        self._logger.info("推文已发布，ID：%s", tweet_id)
        return PostResult(tweet_id=tweet_id, text=text, dry_run=False)


__all__ = ["TweetPoster", "PostResult"]
