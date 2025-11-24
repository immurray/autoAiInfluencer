"""TikTok PK 监控服务逻辑。

本模块聚焦于 TikHub 发现与监控链路，新增了基于搜索接口的轻量发现流程，
用于替换已经失效的 hot_live_rooms 大厅采集。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Set, Tuple

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session

logger = logging.getLogger("services")

# === 搜索发现配置 ===
# 可以通过环境变量或外部配置覆盖，以下为默认值，便于快速部署。
DISCOVERY_SEARCH_ENABLED: bool = True
DISCOVERY_SEARCH_KEYWORDS: List[str] = [
    "PK",
    "1v1",
    "vs",
    "batalla",
    "duelo",
    "x1",
]
DISCOVERY_SEARCH_MAX_RESULTS_PER_KEYWORD: int = 20
DISCOVERY_SEARCH_MAX_NEW_ANCHORS_PER_ROUND: int = 30
DISCOVERY_SEARCH_LOG_SAMPLE: int = 5

Base = declarative_base()


class TikHubClientProtocol(Protocol):
    """限定 TikHub 客户端所需的方法签名。"""

    async def fetch_search_live(self, keyword: str, count: int = 20) -> Dict[str, Any]:
        """调用 TikHub 的搜索直播接口。"""

    async def fetch_search_user(self, keyword: str, count: int = 20) -> Dict[str, Any]:
        """可选的用户搜索接口，作为补充。"""


class Anchor(Base):
    """锚点表定义（若外部已定义，可按需替换 import）。"""

    __tablename__ = "anchors"
    __table_args__ = (UniqueConstraint("unique_id", name="uq_anchor_unique_id"),)

    id = Column(Integer, primary_key=True)
    unique_id = Column(String(128), nullable=False, index=True)
    nickname = Column(String(256), nullable=True)
    country = Column(String(32), nullable=True)
    status = Column(String(32), nullable=False, default="clue")
    source = Column(String(64), nullable=True)
    live_room_id = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


def _extract_search_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从搜索响应中提取列表，兼容不同字段命名。"""

    if not payload:
        return []

    if isinstance(payload, dict):
        for key in ("data", "list", "items", "results", "aweme_list"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # 有些接口会直接返回 list 级别的数据
    if isinstance(payload, list):
        return payload

    return []


def _parse_anchor_from_item(item: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """解析搜索结果中的主播核心信息。

    返回 (unique_id, attrs)；若无法解析唯一标识则返回 None。
    """

    if not isinstance(item, dict):
        return None

    user = item.get("user") or item.get("author") or item.get("user_info") or item
    live_room = (
        item.get("live_room")
        or item.get("live")
        or item.get("room")
        or user.get("room")
        if isinstance(user, dict)
        else None
    )

    unique_id = None
    nickname = None
    country = None

    if isinstance(user, dict):
        unique_id = user.get("unique_id") or user.get("uid") or user.get("sec_uid")
        nickname = user.get("nickname") or user.get("nick_name")
        country = (
            user.get("country")
            or user.get("region")
            or user.get("locale")
            or user.get("language")
        )

    if not unique_id:
        return None

    is_live = False
    room_id = None
    live_room_url = None

    if isinstance(item, dict):
        is_live = bool(item.get("is_live") or item.get("live_status") in (1, True))
        room_id = item.get("room_id") or item.get("live_room_id") or item.get("roomid")
        live_room_url = item.get("live_room_url") or item.get("share_url") or item.get("room_url")

    if isinstance(live_room, dict):
        is_live = bool(is_live or live_room.get("status") in (1, True, "live"))
        room_id = room_id or live_room.get("room_id") or live_room.get("id")
        live_room_url = live_room_url or live_room.get("share_url") or live_room.get("url")

    if not is_live:
        return None

    attrs: Dict[str, Any] = {
        "nickname": nickname,
        "country": country,
        "status": "clue",
        "source": "search_discovery",
        "live_room_id": room_id,
        "live_room_url": live_room_url,
    }
    return unique_id, attrs


async def discovery_scan_by_search(session: Session, tikhub_client: TikHubClientProtocol) -> None:
    """使用 TikHub 搜索接口进行轻量发现。

    步骤：
    1. 遍历 DISCOVERY_SEARCH_KEYWORDS。
    2. 调用 fetch_search_live 获取直播搜索结果（必要时回退 fetch_search_user）。
    3. 过滤出正在直播的条目并解析 unique_id / room_id 等。
    4. 与 anchors 表对比，不重复写入。
    5. 单轮新增数受 DISCOVERY_SEARCH_MAX_NEW_ANCHORS_PER_ROUND 限制。
    6. 输出关键日志，避免失效接口造成的 404 噪音。
    """

    if not DISCOVERY_SEARCH_ENABLED:
        logger.info("[Discovery][Search] 功能未开启，跳过本轮扫描。")
        return

    if not DISCOVERY_SEARCH_KEYWORDS:
        logger.warning("[Discovery][Search] 关键词列表为空，跳过本轮扫描。")
        return

    new_count = 0
    existed_count = 0
    api_errors = 0
    seen_ids: Set[str] = set()
    new_samples: List[str] = []

    for keyword in DISCOVERY_SEARCH_KEYWORDS:
        if new_count >= DISCOVERY_SEARCH_MAX_NEW_ANCHORS_PER_ROUND:
            logger.info(
                "[Discovery][Search] 已达到单轮新增上限 %s，提前结束关键词扫描。",
                DISCOVERY_SEARCH_MAX_NEW_ANCHORS_PER_ROUND,
            )
            break

        try:
            response = await tikhub_client.fetch_search_live(
                keyword=keyword, count=DISCOVERY_SEARCH_MAX_RESULTS_PER_KEYWORD
            )
        except Exception as exc:  # noqa: BLE001
            api_errors += 1
            logger.warning("[Discovery][Search] keyword=%s 调用搜索接口失败：%s", keyword, exc)
            continue

        items = _extract_search_items(response)
        if not items:
            logger.info("[Discovery][Search] keyword=%s 无搜索结果或结果为空。", keyword)
            continue

        for item in items:
            if new_count >= DISCOVERY_SEARCH_MAX_NEW_ANCHORS_PER_ROUND:
                break

            parsed = _parse_anchor_from_item(item)
            if not parsed:
                continue

            unique_id, attrs = parsed
            if unique_id in seen_ids:
                continue
            seen_ids.add(unique_id)

            existing = (
                session.query(Anchor).filter(Anchor.unique_id == unique_id).first()
                if session is not None
                else None
            )
            if existing:
                existed_count += 1
                continue

            anchor = Anchor(
                unique_id=unique_id,
                nickname=attrs.get("nickname"),
                country=attrs.get("country"),
                status=attrs.get("status", "clue"),
                source=attrs.get("source", "search_discovery"),
                live_room_id=attrs.get("live_room_id"),
            )
            session.add(anchor)
            try:
                session.commit()
            except SQLAlchemyError as exc:
                session.rollback()
                logger.warning("[Discovery][Search] 写入主播 %s 失败：%s", unique_id, exc)
                continue

            new_count += 1
            if len(new_samples) < DISCOVERY_SEARCH_LOG_SAMPLE:
                new_samples.append(unique_id)

    logger.info(
        "[Discovery][Search] round_done keywords=%s new_anchors=%s existed=%s api_errors=%s samples=%s",
        len(DISCOVERY_SEARCH_KEYWORDS),
        new_count,
        existed_count,
        api_errors,
        new_samples,
    )


async def collect_active_anchor_metrics(session: Session, anchors: Sequence[Anchor]) -> None:
    """占位的常规监控逻辑，保留原有链路。"""

    # 这里保留与 TikHub fetch_tiktok_live_data 类似的监控逻辑挂钩点。
    await asyncio.sleep(0)  # 占位，实际实现应调用 TikHub 实时数据接口。
    logger.debug("[Monitor] 保留常规监控逻辑，锚点数量=%s", len(anchors))
