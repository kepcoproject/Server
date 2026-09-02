"""
절전 추천의 자동 실행.

사용자가 추천을 "적용"하면 지금까지는 상태만 저장되고 아무 일도 일어나지 않았다.
이 모듈이 그 빈칸을 메운다. 적용된 추천의 시간대가 되면 실제로 제어 명령을 만들어
노드로 내려보낸다.

    추천 생성 → 사용자가 적용 → [여기] 시간대가 되면 명령 발행 → 노드가 실행

안전 장치가 둘 있다.

1. 시간대만 보고 끄지 않는다. 그 순간 실제로 비어 있는지(재실 false) 확인한다.
   스케줄만 믿고 끄면 사람이 있는데 불이 꺼진다.
2. 같은 공간에 같은 시간대 명령을 중복으로 내지 않는다.

MQTT가 붙어 있으면 즉시 내려가고, 아니면 노드가 HTTP로 가져가는 경로가 남아 있다.
"""
import logging
import threading
import uuid
from datetime import timedelta
from typing import Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

from . import analytics
from .config import get_settings
from .database import SessionLocal
from .models import (
    ControlCommand,
    OccupancyProbability,
    RecommendationState,
    SensorReading,
    Space,
)
from .mqtt_client import mqtt_service
from .utils import utcnow

logger = logging.getLogger("smart_energy.auto_control")

settings = get_settings()


def _matches_now(cell: OccupancyProbability, now_local) -> bool:
    """추천 셀의 (요일, 시간대)가 지금인지."""
    return cell.weekday == now_local.weekday() and cell.hour == now_local.hour


def _already_commanded(db: Session, space_id: str, since) -> bool:
    """이번 시간대에 이미 자동 명령을 냈는지."""
    return (
        db.query(ControlCommand)
        .filter(
            ControlCommand.space_id == space_id,
            ControlCommand.trigger == "AUTO",
            ControlCommand.created_at >= since,
        )
        .first()
        is not None
    )


def _room_is_empty(db: Session, space: Space) -> Optional[bool]:
    """
    가장 최근 측정 기준으로 비어 있는지.
    측정값이 아예 없으면 None — 판단 근거가 없으므로 끄지 않는다.
    """
    reading = (
        db.query(SensorReading)
        .filter_by(building=space.building, floor=space.floor, room_id=space.room_id)
        .order_by(desc(SensorReading.device_timestamp))
        .first()
    )
    if reading is None:
        return None
    return reading.occupancy is False


def run_once(db: Session) -> int:
    """
    적용된 추천을 훑어 지금 실행할 것이 있으면 명령을 만든다.
    만든 명령 수를 돌려준다. 테스트에서 직접 호출할 수 있도록 분리해 두었다.
    """
    now = utcnow()
    now_local = now + timedelta(hours=settings.analytics_utc_offset_hours)
    hour_start = now.replace(minute=0, second=0, microsecond=0)

    applied = db.query(RecommendationState).filter_by(status="APPLIED").all()
    issued = 0

    for state in applied:
        cell_id = state.recommendation_id.replace("rec-", "", 1)
        if not cell_id.isdigit():
            continue
        cell = db.get(OccupancyProbability, int(cell_id))
        if cell is None or not _matches_now(cell, now_local):
            continue

        space = (
            db.query(Space)
            .filter_by(building=cell.building, floor=cell.floor, room_id=cell.room_id)
            .first()
        )
        if space is None:
            continue

        if _already_commanded(db, space.space_id, hour_start):
            continue

        empty = _room_is_empty(db, space)
        if empty is not True:
            # 사람이 있거나 근거가 없으면 끄지 않는다. 다음 주기에 다시 본다.
            logger.debug(
                "자동 제어 보류: %s 가 비어 있지 않음(occupancy=%s)", space.space_id, empty
            )
            continue

        command = ControlCommand(
            command_id=f"cmd-{uuid.uuid4().hex[:10]}",
            space_id=space.space_id,
            action="LIGHT",
            value="OFF",
            trigger="AUTO",
            status="PENDING",
        )
        db.add(command)
        db.commit()
        issued += 1

        mqtt_service.publish_command(
            space.building,
            space.floor,
            space.room_id,
            {
                "command_id": command.command_id,
                "action": "light",
                "value": "off",
                "source": "auto",
                "override_minutes": None,
            },
        )
        logger.info(
            "자동 제어 발행: %s 조명 OFF (추천 %s, %s %02d시)",
            space.space_id,
            state.recommendation_id,
            "월화수목금토일"[cell.weekday],
            cell.hour,
        )

    return issued


class AutoControlService:
    """적용된 추천을 주기적으로 검사하는 백그라운드 작업."""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    def start(self) -> None:
        if not settings.auto_control_enabled:
            logger.info("자동 제어 비활성화 상태입니다 (AUTO_CONTROL_ENABLED=false).")
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name="auto-control", daemon=True
        )
        self._thread.start()
        logger.info(
            "자동 제어 시작: %d초마다 적용된 추천을 확인합니다",
            settings.auto_control_interval_seconds,
        )

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stopping.is_set():
            db = SessionLocal()
            try:
                run_once(db)
            except Exception:
                db.rollback()
                # 여기서 죽으면 자동 제어가 조용히 멈춘다. 로그만 남기고 계속 돈다.
                logger.exception("자동 제어 검사 중 오류")
            finally:
                db.close()
            self._stopping.wait(settings.auto_control_interval_seconds)


auto_control_service = AutoControlService()
