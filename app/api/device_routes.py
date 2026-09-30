"""
센서 노드(ESP32) 직접 연동 — HTTP 수집과 제어 폴링.

노드 펌웨어(smart-energy-system/firmware/esp32_sensor_node.ino)는 MQTT가 아니라
HTTP를 쓴다. 브로커 없이 백엔드에 바로 POST하고, 제어 명령은 주기적으로 GET해서
가져간다. MQTT 경로(app/mqtt_client.py)는 그대로 두고 이 경로를 함께 연다.

펌웨어가 보내는 것과 이 서버가 저장하는 것의 차이:

    node_key      -> device_id
    space_id      -> Space 를 거쳐 building / floor / room_id
    current_amp   -> power (전압을 곱해 W로 환산)
    light_lux     -> lux
    (없음)         -> temp   노드에 온도 센서가 없어 항상 비어 있다
    (없음)         -> device_timestamp   펌웨어가 시각을 안 보내 수신 시각을 쓴다

주의: 제어 폴링 응답은 프론트용 {success, data, error} 봉투를 쓰지 않는다.
펌웨어가 doc["action"] 을 그대로 읽기 때문에 평범한 JSON으로 내보내야 한다.
"""
import logging
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import (
    ControlCommand,
    ControlLog,
    Device,
    DeviceStatusEnum,
    SensorReading,
    Space,
)
from ..utils import utcnow
from .compat_common import to_epoch

logger = logging.getLogger("smart_energy.device")

# 접수된 명령을 노드가 이 시간 안에 가져가지 않으면 버린다.
# 화면은 15초 뒤 "응답 없음"으로 포기하는데, 서버에 PENDING 이 그대로 남아 있으면
# 몇 시간 뒤 노드가 붙는 순간 그 오래된 명령이 실행된다. 조명이 저절로 꺼지는 셈이다.
COMMAND_TTL_SECONDS = 300

router = APIRouter(tags=["sensor-node"])

settings = get_settings()


class IngestError(Exception):
    """수집 본문이 잘못됐을 때. 400 으로 돌려주기 위한 신호."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


# 노드가 보낼 수 있는 space_id 의 모양. 펌웨어는 정수, 화면은 "sp-1" 을 쓴다.
SPACE_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _number(body: Dict[str, Any], field: str) -> Optional[float]:
    """
    측정값을 실수로 바꾼다.

    이 경로는 인증이 없다. 펌웨어가 아닌 무언가가 문자열이나 배열을 보내면
    float() 가 그대로 터져 500 이 났고, SQLAlchemy 까지 넘어간 값은
    INSERT 단계에서 터졌다. 여기서 걸러 400 으로 돌려준다.
    """
    value = body.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise IngestError(f"{field} 값이 숫자가 아닙니다")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise IngestError(f"{field} 값이 숫자가 아닙니다")


def _occupancy(body: Dict[str, Any]) -> Optional[bool]:
    """재실 여부. 불리언 컬럼이라 애매한 값을 그대로 넣으면 INSERT 에서 터진다."""
    value = body.get("occupancy")
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
        return value.strip().lower() in ("true", "1")
    raise IngestError("occupancy 값이 참/거짓이 아닙니다")


def _space_key(raw: Any) -> str:
    """
    space_id 를 문자열 키로 바꾼다.

    str(raw) 를 그대로 쓰면 {"a": 1} 같은 본문이 "{'a': 1}" 이라는 이름의 공간을
    만들어 버린다. 인증이 없는 경로라 아무나 공간 목록을 더럽힐 수 있었다.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise IngestError("space_id 형식이 올바르지 않습니다")
    key = str(raw).strip()
    if not SPACE_KEY_PATTERN.fullmatch(key):
        raise IngestError("space_id 형식이 올바르지 않습니다")
    return key


def resolve_space(db: Session, raw: Any) -> Optional[Space]:
    """
    펌웨어의 SPACE_ID 는 정수(1)이고 화면 쪽 spaceId 는 문자열("sp-1")이다.
    둘 다 받아준다.
    """
    if raw is None:
        return None
    key = _space_key(raw)
    space = db.get(Space, key)
    if space is not None:
        return space
    if key.isdigit():
        return db.get(Space, f"sp-{key}")
    return None


def _auto_create_space(db: Session, raw: Any) -> Space:
    """
    설정한 SPACE_ID 에 맞는 공간이 아직 없으면 만들어 준다.
    노드를 처음 켰을 때 데이터가 버려지지 않도록 하기 위한 것이고,
    이름은 화면(공간 관리)에서 고치면 된다.
    """
    key = _space_key(raw)
    space_id = f"sp-{key}" if key.isdigit() else key
    space = Space(
        space_id=space_id,
        code=f"SPACE-{key}",
        name=f"자동 등록된 공간 {key}",
        building_label="미지정",
        floor_number=0,
        rated_power_w=0.0,
        status="정상",
        building="field",
        floor="f0",
        room_id=f"space-{key}",
    )
    db.add(space)
    db.flush()
    logger.warning(
        "space_id=%s 에 해당하는 공간이 없어 자동 등록했습니다. "
        "공간 관리 화면에서 이름과 위치를 채워 주세요.",
        raw,
    )
    return space


@router.post("/sensors/data", summary="[노드] 센서 데이터 수집")
async def ingest_sensor_data(payload: Dict[str, Any], db: Session = Depends(get_db)):
    """
    ESP32 노드가 측정 주기마다 호출한다.

    펌웨어는 응답 본문을 읽지 않고 상태 코드만 로그에 찍으므로,
    실패 원인을 알 수 있게 코드를 구분해서 돌려준다.
    """
    body = payload or {}
    node_key = (body.get("node_key") or "").strip()
    if not node_key:
        return JSONResponse(status_code=400, content={"error": "node_key가 없습니다"})

    try:
        space = resolve_space(db, body.get("space_id"))
        if space is None:
            if body.get("space_id") is None:
                return JSONResponse(status_code=400, content={"error": "space_id가 없습니다"})
            space = _auto_create_space(db, body.get("space_id"))
        current_amp = _number(body, "current_amp")
        lux = _number(body, "light_lux")
        occupancy = _occupancy(body)
    except IngestError as exc:
        return JSONResponse(status_code=400, content={"error": exc.message})

    now = utcnow()

    device = db.get(Device, node_key)
    if device is None:
        device = Device(
            device_id=node_key,
            building=space.building,
            floor=space.floor,
            room_id=space.room_id,
            status=DeviceStatusEnum.online,
        )
        db.add(device)
        logger.info("새 노드 등록(HTTP): %s -> %s", node_key, space.space_id)
    else:
        device.building = space.building
        device.floor = space.floor
        device.room_id = space.room_id
    # HTTP 노드는 LWT가 없다. 데이터가 들어오는 동안은 온라인으로 본다.
    device.status = DeviceStatusEnum.online
    device.last_seen = now

    power_w = current_amp * settings.sensor_line_voltage if current_amp is not None else None

    reading = SensorReading(
        device_id=node_key,
        building=space.building,
        floor=space.floor,
        room_id=space.room_id,
        occupancy=occupancy,
        power=round(power_w, 2) if power_w is not None else None,
        temp=None,  # 노드에 온도 센서가 없다
        lux=lux,
        # 펌웨어가 시각을 보내지 않으므로 서버 수신 시각을 측정 시각으로 삼는다.
        # utcnow() 는 tzinfo 를 뗀 UTC라, 그냥 .timestamp() 하면 로컬 시간대로
        # 해석되어 어긋난다(KST면 9시간). to_epoch 가 UTC로 못박아 변환한다.
        device_timestamp=to_epoch(now),
    )
    db.add(reading)
    db.commit()

    logger.debug(
        "수집: node=%s space=%s occ=%s power=%.1fW lux=%s",
        node_key,
        space.space_id,
        occupancy,
        power_w or 0.0,
        lux,
    )
    return {"stored": True, "spaceId": space.space_id, "powerW": reading.power}


@router.get("/spaces/{space_key}/actuator/latest", summary="[노드] 제어 명령 폴링")
def poll_actuator(
    space_key: str,
    actuator_type: str = Query(default="light"),
    db: Session = Depends(get_db),
):
    """
    노드가 주기적으로 호출해 실행할 상태를 가져간다.

    응답은 봉투 없이 {"action": "on"|"off", "source": ...} 로 나간다.
    명령이 없으면 204 를 돌려주고, 펌웨어는 200 일 때만 반응하므로 릴레이를 건드리지 않는다.
    """
    space = resolve_space(db, space_key)
    if space is None:
        return Response(status_code=204)

    # 실패로 닫힌 명령은 없던 것으로 친다. 만료시킨 명령도 최신이기는 해서,
    # 이걸 빼지 않으면 204 를 한 번 준 다음 폴링에서 결국 그 명령이 내려간다.
    command = (
        db.query(ControlCommand)
        .filter(
            ControlCommand.space_id == space.space_id,
            ControlCommand.action.ilike(actuator_type),
            ControlCommand.status != "FAILED",
        )
        .order_by(desc(ControlCommand.created_at))
        .first()
    )
    if command is None:
        return Response(status_code=204)

    if command.status == "PENDING":
        now = utcnow()
        if (now - command.created_at).total_seconds() > COMMAND_TTL_SECONDS:
            # 화면은 이미 포기한 명령이다. 지금 실행하면 사용자가 의도하지 않은 시점에
            # 조명이 바뀐다. 실패로 닫고 아무것도 내려보내지 않는다.
            command.status = "FAILED"
            command.completed_at = now
            db.add(
                ControlLog(
                    space_id=command.space_id,
                    action=command.action,
                    value=command.value,
                    trigger=command.trigger,
                    result="FAILED",
                )
            )
            db.commit()
            logger.info("제어 만료: %s %s", space.space_id, command.command_id)
            return Response(status_code=204)

        # 펌웨어가 실행 결과를 되돌려주지 않으므로, 가져간 시점을 실행으로 본다.
        # 노드가 실제로 릴레이를 못 돌린 경우는 서버가 알 수 없다.
        command.status = "COMPLETED"
        command.delivered_at = now
        command.completed_at = now
        db.add(
            ControlLog(
                space_id=command.space_id,
                action=command.action,
                value=command.value,
                trigger=command.trigger,
                result="COMPLETED",
            )
        )
        db.commit()
        logger.info(
            "제어 전달: %s %s=%s -> %s",
            space.space_id,
            command.action,
            command.value,
            space_key,
        )

    return {
        "action": command.value.lower(),
        "source": command.trigger.lower(),
        "commandId": command.command_id,
    }
