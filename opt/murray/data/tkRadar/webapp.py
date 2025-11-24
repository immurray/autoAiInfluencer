"""FastAPI 应用入口，调度 TikHub 搜索发现任务。

- 保留原有监控链路（在 services.collect_active_anchor_metrics 中占位）。
- 使用 TikHub 搜索接口替换 hot_live_rooms 大厅发现，避免 404 噪音。
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .services import Base, DISCOVERY_SEARCH_ENABLED, discovery_scan_by_search

logger = logging.getLogger("webapp")

# 默认 TikHub 配置，可通过环境变量覆盖
DEFAULT_TIKHUB_BASE_URL = "https://api.tikhub.app/api/v1/tiktok/web"
DEFAULT_DB_URL = "sqlite:///./tkradar.db"


class TikHubClient:
    """最小化的 TikHub 客户端封装，仅覆盖搜索接口。"""

    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)

    async def fetch_search_live(self, keyword: str, count: int = 20) -> dict:
        url = f"{self._base_url}/fetch_search_live"
        headers = {"Authorization": self._api_key} if self._api_key else None
        params = {"keyword": keyword, "count": count}
        resp = await self._client.post(url, headers=headers, json=params)
        resp.raise_for_status()
        return resp.json()

    async def fetch_search_user(self, keyword: str, count: int = 20) -> dict:
        url = f"{self._base_url}/fetch_search_user"
        headers = {"Authorization": self._api_key} if self._api_key else None
        params = {"keyword": keyword, "count": count}
        resp = await self._client.post(url, headers=headers, json=params)
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()


def _build_session_factory() -> sessionmaker:
    db_url = os.getenv("TKRADAR_DB_URL", DEFAULT_DB_URL)
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


async def _run_discovery_job(session_factory: sessionmaker, client: TikHubClient) -> None:
    """调度器入口：每 30 分钟执行一次搜索发现。"""

    if not DISCOVERY_SEARCH_ENABLED:
        logger.info("[Discovery][Search] 功能关闭，跳过调度任务。")
        return

    session: Optional[Session] = None
    try:
        session = session_factory()
        await discovery_scan_by_search(session, client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Discovery][Search] 调度任务异常：%s", exc)
    finally:
        if session is not None:
            session.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    session_factory = _build_session_factory()
    api_key = os.getenv("TIKHUB_API_KEY", "")
    base_url = os.getenv("TIKHUB_BASE_URL", DEFAULT_TIKHUB_BASE_URL)
    tikhub_client = TikHubClient(base_url=base_url, api_key=api_key)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _run_discovery_job,
        "interval",
        minutes=30,
        id="discovery_scan_search",
        max_instances=1,
        kwargs={"session_factory": session_factory, "client": tikhub_client},
    )
    scheduler.start()
    logger.info("调度器已启动，搜索发现任务每 30 分钟运行一次。")

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await tikhub_client.aclose()
        logger.info("调度器与 TikHub 客户端已关闭。")


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def ping() -> dict:
    """健康检查接口。"""

    return {"status": "ok", "discovery_enabled": DISCOVERY_SEARCH_ENABLED}
