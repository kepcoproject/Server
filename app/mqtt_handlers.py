import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

from . import analytics
from .config import get_settings
from .database import SessionLocal
from .models import Device, DeviceStatusEnum, SensorReading
from .schemas import SensorDataPayload, StatusPayload
from .status_cache import status_cache
from .utils import utcnow
from .webhooks import (
    EVENT_DEVICE_OFFLINE,
    EVENT_DEVICE_ONLINE,
    EVENT_ENERGY_WASTE,
    webhook_dispatcher,
)

logger = logging.getLogger("smart_energy.mqtt_handlers")

settings = get_settings()

_last_alert_at: Dict[str, float] = {}
_alert_lock = threading.Lock()


@dataclass
class TopicInfo:
    building: str
    floor: str
    room_id: str
    message_type: str


def parse_topic(topic: str) -> Optional[TopicInfo]:
    parts = topic.split("/")
    if len(parts) != 5:
        logger.warning("예상하지 못한 토픽 형식: %s", topic)
        return None

    _prefix, building, floor, room_id, message_type = parts
    if message_type not in ("data", "status"):
        logger.warning("알 수 없는 메시지 타입: %s (topic=%s)", message_type, topic)
        return None

    return TopicInfo(building=building, floor=floor, room_id=room_id, message_type=message_type)


def _placeholder_id(info: TopicInfo) -> str:
    """status가 data보다 먼저 도착했을 때 임시로 쓰는 노드 ID."""
    return f"{info.building}-{info.floor}-{info.room_id}"


def _absorb_placeholder(db: Session, device: Device, info: TopicInfo) -> None:
    """
    status가 data보다 먼저 오면 임시 노드가 만들어진다. 나중에 그 공간의 진짜 device_id를
    알게 된 시점에 임시 노드가 들고 있던 상태와 측정값을 넘겨받고 지운다.

    그대로 두면 대시보드에 존재하지 않는 노드가 계속 보이고, 진짜 노드는 status 메시지를
    영영 못 받아 unknown에 머문다.
    """
    placeholder_id = _placeholder_id(info)
    if device.device_id == placeholder_id:
        return
    placeholder = db.get(Device, placeholder_id)
    if placeholder is None:
        return

    status_value = getattr(placeholder.status, "value", placeholder.status)
    changed_at = placeholder.last_status_change

    # 임시 노드 쪽 상태가 더 최신일 때만 진짜 노드로 옮긴다.
    if changed_at is not None and (
        device.last_status_change is None or changed_at > device.last_status_change
    ):
        device.status = placeholder.status
        device.last_status_change = changed_at
        if status_value:
            status_cache.set_status(device.device_id, status_value, changed_at)

    db.query(SensorReading).filter(SensorReading.device_id == placeholder_id).update(
        {"device_id": device.device_id}, synchronize_session=False
    )
    db.delete(placeholder)
    status_cache.drop(placeholder_id)
    logger.info("임시 노드 흡수: %s -> %s", placeholder_id, device.device_id)


def _get_or_create_device(db: Session, device_id: str, info: TopicInfo) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        device = Device(
            device_id=device_id,
            building=info.building,
            floor=info.floor,
            room_id=info.room_id,
            status=DeviceStatusEnum.unknown,
        )
        db.add(device)
    else:
        device.building = info.building
        device.floor = info.floor
        device.room_id = info.room_id
    return device


def handle_data_message(topic: str, raw_payload: bytes) -> None:
    info = parse_topic(topic)
    if info is None or info.message_type != "data":
        return

    try:
        payload_dict = json.loads(raw_payload.decode("utf-8"))
        payload = SensorDataPayload(**payload_dict)
    except (json.JSONDecodeError, ValidationError, UnicodeDecodeError) as exc:
        logger.error("data 페이로드 파싱 실패 (topic=%s, raw=%r): %s", topic, raw_payload[:200], exc)
        return

    db = SessionLocal()
    try:
        device = _get_or_create_device(db, payload.device_id, info)
        db.flush()  # 새 노드를 먼저 INSERT해, 아래 측정값 재지정의 FK 대상이 존재하게 한다
        _absorb_placeholder(db, device, info)
        try:
            measured_at = datetime.fromtimestamp(payload.timestamp, tz=timezone.utc).replace(
                tzinfo=None
            )
        except (OverflowError, OSError, ValueError):
            measured_at = utcnow()
        device.last_seen = measured_at

        reading = SensorReading(
            device_id=payload.device_id,
            building=info.building,
            floor=info.floor,
            room_id=info.room_id,
            occupancy=payload.metrics.occupancy,
            power=payload.metrics.power,
            temp=payload.metrics.temp,
            device_timestamp=payload.timestamp,
        )
        db.add(reading)
        db.commit()
        logger.debug(
            "저장 완료: device=%s room=%s/%s/%s", payload.device_id, info.building, info.floor, info.room_id
        )
        _update_analytics(db, payload, info, measured_at)
        _maybe_emit_energy_waste(db, payload, info, measured_at)
    except Exception:
        db.rollback()
        logger.exception("data 메시지 DB 저장 중 오류 (topic=%s)", topic)
    finally:
        db.close()


def _update_analytics(
    db: Session, payload: SensorDataPayload, info: TopicInfo, measured_at: datetime
) -> None:
    """
    재실 확률 테이블 갱신. 센서 데이터 저장이 끝난 뒤 별도 트랜잭션으로 돌려서,
    분석 쪽 오류가 수집 자체를 막지 않도록 한다.
    """
    if not settings.analytics_enabled or payload.metrics.occupancy is None:
        return
    try:
        analytics.update_occupancy_probability(
            db,
            info.building,
            info.floor,
            info.room_id,
            payload.metrics.occupancy,
            measured_at,
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "재실 확률 갱신 실패 (room=%s/%s/%s)", info.building, info.floor, info.room_id
        )


def _maybe_emit_energy_waste(
    db: Session, payload: SensorDataPayload, info: TopicInfo, measured_at: datetime
) -> None:
    occupancy = payload.metrics.occupancy
    power = payload.metrics.power
    if occupancy is not False or power is None or power < settings.webhook_power_threshold:
        return

    # 평소 이 시간대에 사람이 있는 공간이면 잠깐 자리를 비운 것으로 보고 알리지 않는다(오탐 방지).
    probability = None
    recommendation = analytics.RECOMMEND_WARMING_UP
    if settings.analytics_enabled:
        try:
            probability = analytics.get_probability_now(
                db, info.building, info.floor, info.room_id, measured_at
            )
            recommendation = analytics.recommend(occupancy, probability)
        except Exception:
            logger.exception(
                "절전 추천 판단 실패 (room=%s/%s/%s)", info.building, info.floor, info.room_id
            )
        if recommendation == analytics.RECOMMEND_NORMAL:
            logger.debug(
                "절전 알림 보류: %s/%s/%s 는 이 시간대 재실 확률이 높음(%.2f)",
                info.building,
                info.floor,
                info.room_id,
                probability if probability is not None else -1,
            )
            return

    room_key = f"{info.building}/{info.floor}/{info.room_id}"
    now = time.monotonic()
    with _alert_lock:
        last = _last_alert_at.get(room_key)
        if last is not None and now - last < settings.webhook_alert_cooldown:
            return
        _last_alert_at[room_key] = now

    webhook_dispatcher.emit(
        EVENT_ENERGY_WASTE,
        {
            "device_id": payload.device_id,
            "building": info.building,
            "floor": info.floor,
            "room_id": info.room_id,
            "occupancy": occupancy,
            "power": power,
            "temp": payload.metrics.temp,
            "threshold": settings.webhook_power_threshold,
            "occupancy_probability": round(probability, 3) if probability is not None else None,
            "recommendation": recommendation,
            "device_timestamp": payload.timestamp,
            "message": f"{room_key} 공간이 비어 있는데 전력 {power}W가 소모되고 있습니다.",
        },
    )


def handle_status_message(topic: str, raw_payload: bytes) -> None:
    info = parse_topic(topic)
    if info is None or info.message_type != "status":
        return

    try:
        payload_dict = json.loads(raw_payload.decode("utf-8"))
        payload = StatusPayload(**payload_dict)
    except (json.JSONDecodeError, ValidationError, UnicodeDecodeError) as exc:
        logger.error("status 페이로드 파싱 실패 (topic=%s, raw=%r): %s", topic, raw_payload[:200], exc)
        return

    now = utcnow()
    placeholder_id = _placeholder_id(info)
    db = SessionLocal()
    try:
        # 진짜 노드가 이미 등록돼 있으면 임시 노드보다 먼저 고른다(정렬로 결정론 확보).
        device = (
            db.query(Device)
            .filter_by(building=info.building, floor=info.floor, room_id=info.room_id)
            .order_by((Device.device_id == placeholder_id).asc(), Device.device_id.asc())
            .first()
        )
        if device is None:
            device = Device(
                device_id=placeholder_id,
                building=info.building,
                floor=info.floor,
                room_id=info.room_id,
            )
            db.add(device)

        previous_status = getattr(device.status, "value", device.status) or "unknown"

        device.status = DeviceStatusEnum(payload.status)
        device.last_status_change = now
        db.commit()

        status_cache.set_status(device.device_id, payload.status, now)
        logger.info("상태 갱신: device=%s -> %s", device.device_id, payload.status)

        if previous_status != payload.status:
            webhook_dispatcher.emit(
                EVENT_DEVICE_ONLINE if payload.status == "online" else EVENT_DEVICE_OFFLINE,
                {
                    "device_id": device.device_id,
                    "building": info.building,
                    "floor": info.floor,
                    "room_id": info.room_id,
                    "status": payload.status,
                    "previous_status": previous_status,
                    "changed_at": now.isoformat() + "Z",
                },
            )
    except Exception:
        db.rollback()
        logger.exception("status 메시지 처리 중 오류 (topic=%s)", topic)
    finally:
        db.close()
