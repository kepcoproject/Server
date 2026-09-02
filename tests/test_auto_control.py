"""
절전 추천 자동 실행 검증.

"적용된 추천이 시간대가 되면 실제로 명령을 낸다"와,
"사람이 있으면 끄지 않는다"는 안전 장치를 함께 확인한다.
"""
from datetime import timedelta

from app import analytics
from app.auto_control import run_once
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import (
    ControlCommand,
    OccupancyProbability,
    RecommendationState,
    SensorReading,
    Space,
)
from app.utils import utcnow

settings = get_settings()


def setup_module(module):
    init_db()


def _now_local():
    return utcnow() + timedelta(hours=settings.analytics_utc_offset_hours)


def _prepare(db, key, occupancy):
    """지금 시간대에 걸리는 '적용된 추천' 하나와 최신 측정값을 만든다."""
    building, floor, room = f"auto-{key}", "f1", f"room-{key}"
    space = Space(
        space_id=f"sp-auto-{key}",
        code=f"AUTO-{key}",
        name=f"자동제어 시험실 {key}",
        building_label="자동동",
        floor_number=1,
        rated_power_w=500,
        status="정상",
        building=building,
        floor=floor,
        room_id=room,
    )
    db.add(space)

    local = _now_local()
    cell = OccupancyProbability(
        building=building,
        floor=floor,
        room_id=room,
        weekday=local.weekday(),
        hour=local.hour,
        probability=0.0,
        sample_count=20,
    )
    db.add(cell)
    db.flush()

    db.add(
        SensorReading(
            device_id=f"AUTO-NODE-{key}",
            building=building,
            floor=floor,
            room_id=room,
            occupancy=occupancy,
            power=300.0,
            device_timestamp=int(utcnow().replace(microsecond=0).timestamp()) + 10**9,
        )
    )
    db.add(
        RecommendationState(recommendation_id=f"rec-{cell.id}", status="APPLIED")
    )
    db.commit()
    return space, cell


def test_applied_recommendation_issues_command_when_room_is_empty():
    db = SessionLocal()
    try:
        space, _ = _prepare(db, "empty", occupancy=False)
        issued = run_once(db)
        assert issued >= 1

        command = (
            db.query(ControlCommand)
            .filter_by(space_id=space.space_id, trigger="AUTO")
            .first()
        )
        assert command is not None
        assert command.action == "LIGHT" and command.value == "OFF"
        assert command.status == "PENDING"
    finally:
        db.close()


def test_does_not_turn_off_when_someone_is_there():
    """스케줄만 믿고 끄면 사람이 있는데 불이 꺼진다. 재실이면 건너뛰어야 한다."""
    db = SessionLocal()
    try:
        space, _ = _prepare(db, "occupied", occupancy=True)
        run_once(db)

        command = (
            db.query(ControlCommand)
            .filter_by(space_id=space.space_id, trigger="AUTO")
            .first()
        )
        assert command is None
    finally:
        db.close()


def test_does_not_issue_twice_in_the_same_hour():
    db = SessionLocal()
    try:
        space, _ = _prepare(db, "dedup", occupancy=False)
        first = run_once(db)
        second = run_once(db)

        commands = (
            db.query(ControlCommand)
            .filter_by(space_id=space.space_id, trigger="AUTO")
            .all()
        )
        assert first >= 1
        assert len(commands) == 1, "같은 시간대에 중복으로 명령이 나가면 안 된다"
        assert second == 0
    finally:
        db.close()


def test_pending_recommendation_does_nothing():
    """적용하지 않은 추천은 자동 실행되면 안 된다."""
    db = SessionLocal()
    try:
        space, cell = _prepare(db, "pending", occupancy=False)
        state = db.get(RecommendationState, f"rec-{cell.id}")
        state.status = "PENDING"
        db.commit()

        run_once(db)
        command = (
            db.query(ControlCommand)
            .filter_by(space_id=space.space_id, trigger="AUTO")
            .first()
        )
        assert command is None
    finally:
        db.close()


def test_occupancy_buckets_use_local_time():
    """
    저장은 UTC지만 요일·시간대는 현지 기준이어야 한다.
    UTC로 묶으면 화면의 "수 11시"가 실제로는 한국 20시가 되어 어긋난다.
    """
    db = SessionLocal()
    try:
        from datetime import datetime

        # UTC 23시 = KST 익일 08시
        ts = datetime(2026, 9, 2, 23, 30, 0)
        analytics.update_occupancy_probability(db, "tz-x", "f1", "room-1", False, ts)
        db.commit()

        cell = analytics.get_cell(db, "tz-x", "f1", "room-1", ts)
        assert cell is not None
        assert cell.hour == 8, f"현지 기준 8시여야 하는데 {cell.hour}시로 저장됨"
        # 2026-09-02는 수요일, KST로는 목요일이 된다
        assert cell.weekday == 3
    finally:
        db.close()
