"""
프론트엔드 호환 레이어 공통부 — 응답 봉투, 인증, 공간 변환.

프론트는 모든 응답을 {success, data, error} 로 받고 실패 시 error.code 로 분기한다.
엔드포인트가 30개 가까이 되므로 인증 확인을 매번 되풀이하지 않도록 예외로 처리한다.
"""
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AppUser, SensorReading, Space
from ..utils import utcnow

logger = logging.getLogger("smart_energy.compat")

# 시연용 계정. 프론트 목업과 같은 값이라 화면 수정 없이 로그인된다.
DEMO_LOGIN_ID = "demo"
DEMO_PASSWORD = "demo1234"

# 시연 범위에서는 만료 없이 고정값을 유도해 쓴다. 검증은 상태 없이 비교만 하므로
# 서버를 재시작해도 로그인이 풀리지 않는다.
# 정식 서비스에서는 서명·만료가 있는 토큰으로 교체할 것.
_TOKEN_KEY = b"smart-energy-compat-demo"


def derive_token(kind: str, user_id: str) -> str:
    return hmac.new(_TOKEN_KEY, f"{kind}:{user_id}".encode(), hashlib.sha256).hexdigest()


def hash_password(raw: str) -> str:
    return hashlib.sha256(f"smart-energy:{raw}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# 응답 봉투
# ---------------------------------------------------------------------------
def ok(data: Any) -> Dict[str, Any]:
    return {"success": True, "data": data, "error": None}


class CompatError(Exception):
    """프론트가 이해하는 형태로 실패를 돌려주기 위한 예외."""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


async def compat_error_handler(request: Request, exc: CompatError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={
            "success": False,
            "data": None,
            "error": {"code": exc.code, "message": exc.message},
        },
    )


def unauthorized(message: str = "로그인이 필요합니다") -> CompatError:
    return CompatError(401, "E4010", message)


def not_found(message: str) -> CompatError:
    return CompatError(404, "E4040", message)


# ---------------------------------------------------------------------------
# 사용자
# ---------------------------------------------------------------------------
def ensure_demo_user(db: Session) -> AppUser:
    """시연 계정이 없으면 만든다. 첫 기동에도 바로 로그인되도록."""
    user = db.query(AppUser).filter_by(login_id=DEMO_LOGIN_ID).first()
    if user is None:
        user = AppUser(
            user_id="u-1",
            login_id=DEMO_LOGIN_ID,
            name="관리자",
            email="demo@enersave.io",
            role="ADMIN",
            status="ACTIVE",
            password_hash=hash_password(DEMO_PASSWORD),
        )
        db.add(user)
        db.commit()
    return user


def user_out(user: AppUser) -> Dict[str, Any]:
    return {
        "id": user.user_id,
        "loginId": user.login_id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
    }


def user_row(user: AppUser) -> Dict[str, Any]:
    """사용자 관리 화면(A-08)이 쓰는 형태."""
    return {
        "userId": user.user_id,
        "loginId": user.login_id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "requestedAt": iso_z(user.requested_at),
    }


def require_user(
    authorization: Optional[str] = Header(default=None), db: Session = Depends(get_db)
) -> AppUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise unauthorized()
    token = authorization.split(" ", 1)[1].strip()

    user = ensure_demo_user(db)
    for candidate in db.query(AppUser).filter(AppUser.status == "ACTIVE").all():
        if hmac.compare_digest(token, derive_token("access", candidate.user_id)):
            return candidate
    raise unauthorized()


def require_admin(user: AppUser = Depends(require_user)) -> AppUser:
    if user.role != "ADMIN":
        raise CompatError(403, "E4030", "관리자 권한이 필요합니다")
    return user


# ---------------------------------------------------------------------------
# 시각
# ---------------------------------------------------------------------------
def iso_z(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat() + "Z"


def epoch_to_iso_z(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def to_epoch(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


# ---------------------------------------------------------------------------
# 공간
#
# 센서는 building/floor/room_id 로 위치를 말하고, 화면은 spaceId 하나로 다룬다.
# Space 테이블이 그 사이를 잇는다. 측정값이 들어온 공간은 자동으로 등록된다.
# ---------------------------------------------------------------------------
def floor_number(floor: str) -> int:
    digits = "".join(ch for ch in floor if ch.isdigit())
    return int(digits) if digits else 0


def _next_space_id(db: Session) -> str:
    used = {s.space_id for s in db.query(Space.space_id).all()}
    n = 1
    while f"sp-{n}" in used:
        n += 1
    return f"sp-{n}"


def ensure_space(db: Session, building: str, floor: str, room_id: str) -> Space:
    space = db.query(Space).filter_by(building=building, floor=floor, room_id=room_id).first()
    if space is not None:
        return space
    space = Space(
        space_id=_next_space_id(db),
        code=room_id.upper(),
        name=f"{building} {floor} {room_id}",
        building_label=building,
        floor_number=floor_number(floor),
        rated_power_w=0.0,
        status="정상",
        building=building,
        floor=floor,
        room_id=room_id,
    )
    db.add(space)
    db.flush()
    return space


def sync_spaces_from_readings(db: Session) -> int:
    """측정값이 들어온 적 있는 공간을 Space 테이블에 반영한다. 새로 만든 개수를 돌려준다."""
    rows = (
        db.query(SensorReading.building, SensorReading.floor, SensorReading.room_id)
        .group_by(SensorReading.building, SensorReading.floor, SensorReading.room_id)
        .all()
    )
    created = 0
    for building, floor, room_id in rows:
        before = db.query(Space).filter_by(building=building, floor=floor, room_id=room_id).first()
        if before is None:
            ensure_space(db, building, floor, room_id)
            created += 1
    if created:
        db.commit()
    return created


def get_space(db: Session, space_id: str) -> Space:
    space = db.get(Space, space_id)
    if space is None:
        raise not_found("공간을 찾을 수 없습니다")
    return space


def space_out(db: Session, space: Space) -> Dict[str, Any]:
    from ..models import Device  # 순환 참조 방지를 위해 지역 임포트

    node_count = (
        db.query(func.count(Device.device_id))
        .filter(
            Device.building == space.building,
            Device.floor == space.floor,
            Device.room_id == space.room_id,
        )
        .scalar()
        or 0
    )
    return {
        "spaceId": space.space_id,
        "code": space.code,
        "name": space.name,
        "building": space.building_label,
        "floor": space.floor_number,
        "nodeCount": int(node_count),
        "ratedPowerW": space.rated_power_w,
        "status": space.status,
    }


def latest_reading_per_room(db: Session) -> List[SensorReading]:
    """공간별 최신 측정값 1건씩."""
    latest = (
        db.query(
            SensorReading.building.label("building"),
            SensorReading.floor.label("floor"),
            SensorReading.room_id.label("room_id"),
            func.max(SensorReading.device_timestamp).label("device_timestamp"),
        )
        .group_by(SensorReading.building, SensorReading.floor, SensorReading.room_id)
        .subquery()
    )
    rows = (
        db.query(SensorReading)
        .join(
            latest,
            (SensorReading.building == latest.c.building)
            & (SensorReading.floor == latest.c.floor)
            & (SensorReading.room_id == latest.c.room_id)
            & (SensorReading.device_timestamp == latest.c.device_timestamp),
        )
        .all()
    )
    seen, out = set(), []
    for row in rows:
        key = (row.building, row.floor, row.room_id)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def data_anchor(db: Session) -> datetime:
    """
    '지금'의 기준 시각. 데모 DB처럼 데이터가 과거에 멈춰 있으면 화면이 전부 0으로
    보이므로, 최근 24시간에 데이터가 없으면 가장 최근 측정 시각을 기준으로 삼는다.
    """
    newest = db.query(func.max(SensorReading.device_timestamp)).scalar()
    now = utcnow()
    if newest is None:
        return now
    newest_dt = datetime.fromtimestamp(newest, tz=timezone.utc).replace(tzinfo=None)
    return now if to_epoch(now) - newest <= 24 * 3600 else newest_dt
