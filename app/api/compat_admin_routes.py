"""
프론트엔드 호환 레이어 — 공간 · 디바이스 · 사용자 · 제어 · 리포트.

인증과 대시보드는 compat_routes.py 에 있다. 여기는 관리 화면들이 쓰는 부분이다.

제어(G-01)는 "접수"와 "실행"을 구분한다. 명령을 넣으면 202 로 접수만 알리고,
노드가 폴링해 가져가 실행한 뒤에야 완료로 바뀐다. 노드가 붙어 있지 않으면 계속
PENDING 이고 화면은 "응답 없음"으로 표시하는데, 그게 실제 상태라 맞는 동작이다.
"""
import logging
import secrets
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import desc
from sqlalchemy.orm import Session

from .. import analytics
from ..config import get_settings
from ..database import get_db
from ..mqtt_client import mqtt_service
from ..models import (
    AppUser,
    ControlCommand,
    ControlLog,
    Device,
    DeviceMeta,
    DeviceStatusEnum,
    SensorReading,
    Space,
)
from ..utils import utcnow
from .compat_common import (
    CompatError,
    data_anchor,
    epoch_to_iso_z,
    floor_number,
    get_space,
    iso_z,
    not_found,
    ok,
    require_admin,
    require_user,
    space_out,
    sync_spaces_from_readings,
    to_epoch,
    user_row,
)
from .compat_routes import occupied_periods

logger = logging.getLogger("smart_energy.compat")

router = APIRouter(tags=["frontend-compat"])

settings = get_settings()

# 리포트 환산 계수. 실제 계약 요금제와 최신 고시값으로 교체할 것.
PRICE_PER_KWH = 120  # 원
CO2_FACTOR_KG_PER_KWH = 0.4781

REPORT_BUCKETS = {"day": 14, "week": 8, "month": 6}
BUCKET_HOURS = {"day": 24, "week": 24 * 7, "month": 24 * 30}

INTERVAL_MINUTES = {"1m": 1, "10m": 10, "1h": 60}

# 마지막 수신이 이보다 오래되면 오프라인으로 본다.
# (펌웨어가 HTTP를 쓰면 MQTT의 LWT가 없어 이 방식으로 판정해야 한다)
OFFLINE_AFTER_SECONDS = 180


def _as_number(value: Any, field: str, cast=int, default=None):
    """
    화면에서 온 숫자 값을 안전하게 바꾼다.

    그냥 int(...) 를 쓰면 "이층" 같은 값이 들어왔을 때 ValueError 가 그대로 올라가
    500 이 난다. 500 은 봉투({success, data, error})가 없어서 화면이 사유를 읽지도
    못하고 "요청에 실패했습니다" 만 띄운다. 400 으로 내려 이유를 보이게 한다.
    """
    if value is None or value == "":
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        raise CompatError(400, "E4000", f"{field} 값이 올바르지 않습니다")


# ---------------------------------------------------------------------------
# 공간 (B-01 ~ B-05)
# ---------------------------------------------------------------------------
@router.get("/spaces", summary="[호환] 공간 목록 (B-02)")
def compat_list_spaces(
    building: Optional[str] = Query(default=None),
    floor: Optional[str] = Query(default=None),
    user: AppUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    # 측정값이 들어온 공간을 먼저 반영해, 센서만 붙여도 화면에 나타나게 한다.
    sync_spaces_from_readings(db)

    query = db.query(Space)
    if building:
        query = query.filter(Space.building_label == building)
    if floor:
        query = query.filter(Space.floor_number == floor_number(str(floor)))
    items = [space_out(db, s) for s in query.order_by(Space.space_id).all()]
    return ok({"items": items})


@router.get("/spaces/{space_id}", summary="[호환] 공간 상세 (B-03)")
def compat_get_space(
    space_id: str, user: AppUser = Depends(require_user), db: Session = Depends(get_db)
):
    return ok(space_out(db, get_space(db, space_id)))


@router.post("/spaces", summary="[호환] 공간 등록 (B-01)")
async def compat_create_space(
    payload: Dict[str, Any], user: AppUser = Depends(require_admin), db: Session = Depends(get_db)
):
    body = payload or {}
    code = (body.get("code") or "").strip()
    if not code:
        raise CompatError(400, "E4000", "공간 코드를 입력하세요")

    building_label = body.get("building") or "미지정"
    floor_no = _as_number(body.get("floor"), "층", int, 0)

    # 센서 토픽과 이어질 위치. 화면에서 만든 공간은 아직 센서가 없으므로
    # 코드에서 유추해 두고, 실제 측정값이 들어오면 그 위치로 맞춰진다.
    location = (building_label, f"f{floor_no}", code.lower())
    if db.query(Space).filter_by(
        building=location[0], floor=location[1], room_id=location[2]
    ).first():
        raise CompatError(409, "E4090", "같은 위치의 공간이 이미 있습니다")

    used = {s.space_id for s in db.query(Space.space_id).all()}
    n = 1
    while f"sp-{n}" in used:
        n += 1

    space = Space(
        space_id=f"sp-{n}",
        code=code,
        name=body.get("name") or code,
        building_label=building_label,
        floor_number=floor_no,
        rated_power_w=_as_number(body.get("ratedPowerW"), "정격 전력", float, 0.0),
        status="정상",
        building=location[0],
        floor=location[1],
        room_id=location[2],
    )
    db.add(space)
    db.commit()
    logger.info("[호환] 공간 등록: %s (%s)", space.space_id, code)
    return ok(space_out(db, space))


@router.patch("/spaces/{space_id}", summary="[호환] 공간 수정 (B-04)")
async def compat_update_space(
    space_id: str,
    payload: Dict[str, Any],
    user: AppUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    space = get_space(db, space_id)
    body = payload or {}
    if "code" in body and body["code"]:
        space.code = body["code"]
    if "name" in body and body["name"]:
        space.name = body["name"]
    if "building" in body and body["building"]:
        space.building_label = body["building"]
    if "floor" in body and body["floor"] is not None:
        space.floor_number = _as_number(body["floor"], "층", int, space.floor_number)
    if "ratedPowerW" in body and body["ratedPowerW"] is not None:
        space.rated_power_w = _as_number(
            body["ratedPowerW"], "정격 전력", float, space.rated_power_w
        )
    db.commit()
    return ok(space_out(db, space))


@router.delete("/spaces/{space_id}", summary="[호환] 공간 삭제 (B-05)")
def compat_delete_space(
    space_id: str, user: AppUser = Depends(require_admin), db: Session = Depends(get_db)
):
    space = get_space(db, space_id)
    node_count = (
        db.query(Device)
        .filter(
            Device.building == space.building,
            Device.floor == space.floor,
            Device.room_id == space.room_id,
        )
        .count()
    )
    if node_count:
        # 노드가 남아 있는 공간을 지우면 측정값의 소속이 사라진다.
        # 코드는 E4090 이어야 한다. 화면(Spaces.jsx)이 이 값을 보고
        # "연결된 센서 노드를 먼저 해제하세요" 를 띄운다. 다른 코드면 사유가 안 보인다.
        raise CompatError(
            409, "E4090", f"노드 {node_count}개가 등록된 공간은 삭제할 수 없습니다"
        )
    db.delete(space)
    db.commit()
    return ok({"deleted": True})


# ---------------------------------------------------------------------------
# 디바이스 (C-01 ~ C-03)
# ---------------------------------------------------------------------------
def _device_status(device: Device, anchor) -> str:
    """
    노드 생존 판정.

    LWT로 offline이 확정된 노드는 그대로 오프라인이다. 그 외에는 마지막 수신 시각으로
    판단하는데, 기준은 현재 시각이 아니라 데이터 기준 시각(anchor)이다. 데모 DB처럼
    데이터 전체가 과거에 멈춰 있어도 '그 시점 기준으로 살아 있던 노드'를 구분할 수 있다.

    LWT의 online만 믿으면 13일 전에 끊긴 노드도 계속 온라인으로 보이므로,
    상태 메시지와 무관하게 수신 시각을 함께 본다.
    """
    if getattr(device.status, "value", device.status) == "offline":
        return "OFFLINE"
    if device.last_seen is None:
        return "OFFLINE"
    age = (anchor - device.last_seen).total_seconds()
    return "ONLINE" if age <= OFFLINE_AFTER_SECONDS else "OFFLINE"


def _device_out(db: Session, device: Device, anchor=None) -> Dict[str, Any]:
    if anchor is None:
        anchor = data_anchor(db)
    meta = db.get(DeviceMeta, device.device_id)
    space = (
        db.query(Space)
        .filter_by(building=device.building, floor=device.floor, room_id=device.room_id)
        .first()
    )
    return {
        "deviceId": device.device_id,
        "spaceId": space.space_id if space else None,
        "sensors": [s for s in (meta.sensors.split(",") if meta and meta.sensors else []) if s],
        "actuators": [
            a for a in (meta.actuators.split(",") if meta and meta.actuators else []) if a
        ],
        "status": _device_status(device, anchor),
        "signalStrength": meta.signal_strength if meta else 0,
        "firmware": meta.firmware if meta else "v1.0.0",
        "lastSeenAt": iso_z(device.last_seen),
    }


@router.get("/devices", summary="[호환] 노드 목록 (C-02)")
def compat_list_devices(user: AppUser = Depends(require_user), db: Session = Depends(get_db)):
    sync_spaces_from_readings(db)
    anchor = data_anchor(db)  # 노드마다 다시 계산하지 않도록 한 번만 구한다
    items = [
        _device_out(db, d, anchor) for d in db.query(Device).order_by(Device.device_id).all()
    ]
    return ok({"items": items})


@router.get("/devices/{device_id}", summary="[호환] 노드 상세 (C-03)")
def compat_get_device(
    device_id: str, user: AppUser = Depends(require_user), db: Session = Depends(get_db)
):
    device = db.get(Device, device_id)
    if device is None:
        raise not_found("노드를 찾을 수 없습니다")
    return ok(_device_out(db, device))


@router.delete("/devices/{device_id}", summary="[호환] 노드 삭제")
def compat_delete_device(
    device_id: str, user: AppUser = Depends(require_admin), db: Session = Depends(get_db)
):
    """
    노드를 등록 해제한다.

    공간 삭제(B-05)가 "노드를 먼저 해제하라"고 막는데 정작 해제할 방법이 없어서
    막다른 길이었다. 그 출구를 만든다.

    측정값은 Device.readings 의 cascade 로 함께 사라진다. 노드를 지운다는 건
    그 자리의 측정 이력도 지운다는 뜻이라 화면에서 되돌릴 수 없다.
    """
    device = db.get(Device, device_id)
    if device is None:
        raise not_found("노드를 찾을 수 없습니다")

    # DeviceMeta 는 FK 가 아니라 같은 키를 쓰는 별도 표라 직접 지운다.
    meta = db.get(DeviceMeta, device_id)
    if meta is not None:
        db.delete(meta)

    db.delete(device)
    db.commit()
    logger.info("[호환] 노드 삭제: %s", device_id)
    return ok({"deleted": True})


@router.post("/devices", summary="[호환] 노드 등록 (C-01)")
async def compat_create_device(
    payload: Dict[str, Any], user: AppUser = Depends(require_admin), db: Session = Depends(get_db)
):
    body = payload or {}
    device_id = (body.get("deviceId") or "").strip()
    if not device_id:
        raise CompatError(400, "E4000", "노드 ID를 입력하세요")
    if db.get(Device, device_id) is not None:
        raise CompatError(409, "E4090", "이미 등록된 노드 ID입니다")

    space = get_space(db, body.get("spaceId") or "")
    device = Device(
        device_id=device_id,
        building=space.building,
        floor=space.floor,
        room_id=space.room_id,
        status=DeviceStatusEnum.unknown,
    )
    db.add(device)

    api_key = f"dvk_{secrets.token_urlsafe(24)}"
    db.add(
        DeviceMeta(
            device_id=device_id,
            space_id=space.space_id,
            sensors=",".join(body.get("sensors") or []),
            actuators=",".join(body.get("actuators") or []),
            firmware="v1.0.0",
            signal_strength=0,
        )
    )
    db.commit()

    out = _device_out(db, device)
    # deviceApiKey 는 등록 응답에만 실려 나가고 이후 조회에는 포함되지 않는다.
    out["deviceApiKey"] = api_key
    logger.info("[호환] 노드 등록: %s -> %s", device_id, space.space_id)
    return ok(out)


# ---------------------------------------------------------------------------
# 전력 이력 (D-02)
# ---------------------------------------------------------------------------
@router.get("/telemetry/power-history/{space_id}", summary="[호환] 전력 이력 (D-02)")
def compat_power_history(
    space_id: str,
    hours: int = Query(default=1, ge=1, le=720),
    interval: str = Query(default="1m"),
    user: AppUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    space = get_space(db, space_id)
    minutes = INTERVAL_MINUTES.get(interval, 1)
    bucket_seconds = minutes * 60

    end_ts = to_epoch(data_anchor(db))
    start_ts = end_ts - hours * 3600

    rows = (
        db.query(SensorReading)
        .filter(
            SensorReading.building == space.building,
            SensorReading.floor == space.floor,
            SensorReading.room_id == space.room_id,
            SensorReading.device_timestamp >= start_ts,
            SensorReading.device_timestamp <= end_ts,
        )
        .order_by(SensorReading.device_timestamp)
        .all()
    )

    # 요청한 간격으로 다운샘플링한다. 값이 없는 구간은 null 로 두어
    # 프론트가 선을 끊어 그리도록 한다(connectNulls=false).
    buckets: Dict[int, List[float]] = {}
    for row in rows:
        if row.power is None:
            continue
        slot = (row.device_timestamp // bucket_seconds) * bucket_seconds
        buckets.setdefault(slot, []).append(row.power)

    points = []
    slot = (start_ts // bucket_seconds) * bucket_seconds
    while slot <= end_ts:
        values = buckets.get(slot)
        points.append(
            {
                "t": epoch_to_iso_z(slot),
                "powerW": round(sum(values) / len(values)) if values else None,
            }
        )
        slot += bucket_seconds

    return ok({"points": points, "occupiedPeriods": occupied_periods(db, space, hours)})


# ---------------------------------------------------------------------------
# 제어 (G-01, G-04)
# ---------------------------------------------------------------------------
@router.post("/control/commands", summary="[호환] 수동 제어 접수 (G-01)")
async def compat_post_control(
    payload: Dict[str, Any], user: AppUser = Depends(require_admin), db: Session = Depends(get_db)
):
    body = payload or {}
    space = get_space(db, body.get("spaceId") or "")

    devices = (
        db.query(Device)
        .filter(
            Device.building == space.building,
            Device.floor == space.floor,
            Device.room_id == space.room_id,
        )
        .all()
    )
    # 목록 화면과 달리 여기서는 데이터 기준 시각(anchor)이 아니라 진짜 현재 시각으로 본다.
    # anchor 는 "DB 에서 가장 최근 측정값" 이라, 데이터가 사흘 전에 멈춰 있어도
    # 그 시점 기준으로는 모든 노드가 ONLINE 이 된다. 목록에서는 그게 맞지만,
    # 제어는 지금 당장 명령을 가져갈 노드가 있어야 성립한다. anchor 로 판정하면
    # 화면은 "온라인"이라 해놓고 명령은 아무도 안 가져가서 15초 뒤 "응답 없음"만 뜬다.
    if not any(_device_status(d, utcnow()) == "ONLINE" for d in devices):
        raise CompatError(503, "E5030", "센서 노드가 오프라인입니다")

    command = ControlCommand(
        command_id=f"cmd-{uuid.uuid4().hex[:10]}",
        space_id=space.space_id,
        action=body.get("action") or "LIGHT",
        value=str(body.get("value") or "OFF"),
        trigger="MANUAL",
        status="PENDING",
        override_minutes=body.get("overrideMinutes"),
    )
    db.add(command)
    db.commit()
    logger.info("[호환] 제어 접수: %s %s=%s", space.space_id, command.action, command.value)

    # MQTT가 붙어 있으면 즉시 내려보낸다. 노드가 다음 폴링 주기를 기다릴 필요가 없어
    # 화면의 타임아웃(15초) 안에 결과가 돌아온다.
    # 실패하거나 MQTT가 꺼져 있으면 노드가 HTTP로 가져가는 경로가 남아 있다.
    delivered = mqtt_service.publish_command(
        space.building,
        space.floor,
        space.room_id,
        {
            "command_id": command.command_id,
            "action": command.action.lower(),
            "value": command.value.lower(),
            "source": command.trigger.lower(),
            "override_minutes": command.override_minutes,
        },
    )
    if not delivered:
        logger.debug("MQTT 발행 불가 — 노드의 HTTP 폴링을 기다린다 (%s)", command.command_id)

    # 202 Accepted — 접수했다는 뜻이지 실행됐다는 뜻이 아니다.
    return JSONResponse(
        status_code=202,
        content=ok({"commandId": command.command_id, "status": command.status}),
    )


@router.get("/control/commands/{command_id}", summary="[호환] 명령 상태 조회")
def compat_get_command(
    command_id: str, user: AppUser = Depends(require_user), db: Session = Depends(get_db)
):
    command = db.get(ControlCommand, command_id)
    if command is None:
        raise not_found("명령을 찾을 수 없습니다")
    return ok(
        {
            "commandId": command.command_id,
            "spaceId": command.space_id,
            "action": command.action,
            "value": command.value,
            "trigger": command.trigger,
            "status": command.status,
            "createdAt": iso_z(command.created_at),
            "completedAt": iso_z(command.completed_at),
        }
    )


@router.get("/control/logs", summary="[호환] 제어 이력 (G-04)")
def compat_control_logs(user: AppUser = Depends(require_user), db: Session = Depends(get_db)):
    rows = db.query(ControlLog).order_by(desc(ControlLog.timestamp)).limit(100).all()
    items = [
        {
            "logId": f"log-{r.log_id}",
            "timestamp": iso_z(r.timestamp),
            "spaceId": r.space_id,
            "action": r.action,
            "value": r.value,
            "trigger": r.trigger,
            "result": r.result,
        }
        for r in rows
    ]
    return ok({"items": items})


# ---------------------------------------------------------------------------
# 리포트 (H-01)
# ---------------------------------------------------------------------------
@router.get("/reports/savings", summary="[호환] 기간별 절감 리포트 (H-01)")
def compat_report_savings(
    period: str = Query(default="day"),
    spaceId: Optional[str] = Query(default=None),
    user: AppUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    if period not in REPORT_BUCKETS:
        period = "day"
    count = REPORT_BUCKETS[period]
    hours = BUCKET_HOURS[period]

    targets = [get_space(db, spaceId)] if spaceId else db.query(Space).all()
    end = data_anchor(db)

    items = []
    for i in range(count - 1, -1, -1):
        bucket_end = end - timedelta(hours=hours * i)
        bucket_start = bucket_end - timedelta(hours=hours)
        usage_wh = baseline_wh = 0.0
        for space in targets:
            result = analytics.compute_savings(
                db, space.building, space.floor, space.room_id, bucket_start, bucket_end
            )
            if result:
                usage_wh += result["actual_kwh"] * 1000
                baseline_wh += result["baseline_kwh"] * 1000
        items.append(
            {
                "date": iso_z(bucket_start),
                "usageWh": round(usage_wh),
                "baselineWh": round(baseline_wh),
            }
        )

    total_usage = sum(i["usageWh"] for i in items)
    total_baseline = sum(i["baselineWh"] for i in items)
    total_saving = max(total_baseline - total_usage, 0)
    rate = (total_saving / total_baseline) if total_baseline > 0 else 0.0

    return ok(
        {
            "summary": {
                "totalSavingWh": total_saving,
                "savingCost": round((total_saving / 1000) * PRICE_PER_KWH),
                "co2ReductionKg": round((total_saving / 1000) * CO2_FACTOR_KG_PER_KWH, 1),
                "savingRate": round(rate, 4),
            },
            "items": items,
        }
    )


# ---------------------------------------------------------------------------
# 사용자 (A-08, A-09)
# ---------------------------------------------------------------------------
@router.get("/users", summary="[호환] 사용자 목록 (A-08)")
def compat_list_users(user: AppUser = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(AppUser).order_by(AppUser.requested_at).all()
    return ok({"items": [user_row(u) for u in rows]})


@router.patch("/users/{user_id}", summary="[호환] 승인 · 권한 변경 (A-09)")
async def compat_update_user(
    user_id: str,
    payload: Dict[str, Any],
    user: AppUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(AppUser, user_id)
    if target is None:
        raise not_found("사용자를 찾을 수 없습니다")

    body = payload or {}
    if body.get("status") in ("ACTIVE", "PENDING", "REJECTED"):
        if target.user_id == user.user_id and body["status"] != "ACTIVE":
            # 스스로를 반려하면 그 자리에서 로그인이 막히고, 부트스트랩 관리자도
            # 아이디가 이미 있으면 다시 만들지 않는다. DB 를 직접 고치기 전에는
            # 아무도 들어올 수 없다. 권한 해제와 같은 이유로 막는다.
            raise CompatError(400, "E4003", "자신의 계정 상태는 바꿀 수 없습니다")
        target.status = body["status"]
    if body.get("role") in ("ADMIN", "MEMBER"):
        if target.user_id == user.user_id and body["role"] != "ADMIN":
            # 마지막 관리자가 스스로 권한을 내리면 아무도 승인할 수 없게 된다.
            raise CompatError(400, "E4003", "자신의 관리자 권한은 해제할 수 없습니다")
        target.role = body["role"]
    db.commit()
    logger.info("[호환] 사용자 변경: %s -> %s/%s", user_id, target.status, target.role)
    return ok(user_row(target))
