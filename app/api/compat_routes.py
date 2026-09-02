"""
프론트엔드(kepcoproject/Client) 호환 레이어 — 실험용.

프론트는 이 서버와 다른 규격을 전제로 만들어져 있다.

- 경로가 다르다: /monitoring/occupancy-map vs /api/data/latest
- 응답을 {success, data, error} 봉투로 받아 data만 꺼내 쓴다
- 공간을 spaceId 하나로 식별한다 (이 서버는 building/floor/room_id 세 개)
- 모든 요청에 Bearer 토큰을 싣고, /auth/me가 실패하면 로그인 화면으로 튕긴다

이 모듈은 기존 /api/* 엔드포인트를 그대로 둔 채, 프론트가 부르는 모양으로
같은 데이터를 다시 내보낸다. 화면 세 개(대시보드·추천·헤더 알림)를 띄우는 데
필요한 만큼만 구현했다.

주의: 로그인은 계정 하나를 코드에 박아둔 실험용이다. 실제 운영에는 사용자 테이블과
비밀번호 해싱, 서명된 토큰이 필요하다. COMPAT_API_ENABLED=false 로 끌 수 있다.
"""
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from .. import analytics
from ..config import get_settings
from ..database import get_db
from ..models import OccupancyProbability, SensorReading
from ..utils import utcnow

logger = logging.getLogger("smart_energy.compat")

router = APIRouter(tags=["frontend-compat"])

settings = get_settings()

# 실험용 계정. 프론트 목업과 같은 값이라 화면 수정 없이 바로 로그인된다.
DEMO_LOGIN_ID = "demo"
DEMO_PASSWORD = "demo1234"
DEMO_USER = {
    "id": "u-1",
    "loginId": DEMO_LOGIN_ID,
    "name": "관리자",
    "email": "demo@enersave.io",
    "role": "ADMIN",
}

# 토큰을 메모리 집합에 들고 있으면 --reload로 서버가 한 번만 재시작해도 로그인이 풀린다.
# 실험용이므로 만료 없이 고정값을 유도해 쓰고, 검증은 상태 없이 비교만 한다.
# (운영에서는 만료 시각과 사용자별 서명이 들어간 JWT로 바꿔야 한다)
_TOKEN_KEY = b"smart-energy-compat-experimental"


def _derive_token(kind: str) -> str:
    return hmac.new(_TOKEN_KEY, f"{kind}:{DEMO_USER['id']}".encode(), hashlib.sha256).hexdigest()


ACCESS_TOKEN = _derive_token("access")
REFRESH_TOKEN = _derive_token("refresh")

# 재실이 아닌데 이 값을 넘으면 낭비로 본다 (프론트 목업과 동일 기준).
WASTE_POWER_W = 50.0


def ok(data: Any) -> Dict[str, Any]:
    return {"success": True, "data": data, "error": None}


def fail(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"success": False, "data": None, "error": {"code": code, "message": message}},
    )


# ---------------------------------------------------------------------------
# spaceId <-> building/floor/room_id 변환
#
# 공간 테이블을 새로 만들지 않고 합성 ID로 왕복한다. 프론트는 이 값을 그대로
# 경로에 넣어 되돌려주므로, 서버가 다시 세 조각으로 풀면 된다.
# ---------------------------------------------------------------------------
SPACE_SEP = "~"


def make_space_id(building: str, floor: str, room_id: str) -> str:
    return f"{building}{SPACE_SEP}{floor}{SPACE_SEP}{room_id}"


def parse_space_id(space_id: str) -> Optional[tuple]:
    parts = space_id.split(SPACE_SEP)
    return tuple(parts) if len(parts) == 3 else None


def floor_number(floor: str) -> int:
    digits = "".join(ch for ch in floor if ch.isdigit())
    return int(digits) if digits else 0


def _latest_per_room(db: Session) -> List[SensorReading]:
    """공간별 최신 측정값 1건씩. /api/data/latest 와 같은 방식."""
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


def _data_anchor(db: Session) -> datetime:
    """
    '오늘'의 기준 시각. 보통은 현재 시각이지만, 데모 DB처럼 데이터가 과거에 멈춰 있으면
    화면이 전부 0으로 보이므로 가장 최근 측정 시각을 기준으로 삼는다.
    """
    newest = db.query(func.max(SensorReading.device_timestamp)).scalar()
    now = utcnow()
    if newest is None:
        return now
    newest_dt = datetime.fromtimestamp(newest, tz=timezone.utc).replace(tzinfo=None)
    return now if newest_dt > now - timedelta(hours=24) else newest_dt


# ---------------------------------------------------------------------------
# 인증 (실험용)
# ---------------------------------------------------------------------------
def current_user(authorization: Optional[str] = Header(default=None)) -> Optional[Dict[str, Any]]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return DEMO_USER if hmac.compare_digest(token, ACCESS_TOKEN) else None


@router.post("/auth/login", summary="[호환] 로그인")
async def compat_login(payload: Dict[str, Any]):
    login_id = (payload or {}).get("loginId")
    password = (payload or {}).get("password")
    if login_id != DEMO_LOGIN_ID or password != DEMO_PASSWORD:
        return fail(401, "E4010", "아이디 또는 비밀번호가 올바르지 않습니다")

    logger.info("[호환] 로그인: %s", login_id)
    return ok({"accessToken": ACCESS_TOKEN, "refreshToken": REFRESH_TOKEN, "user": DEMO_USER})


@router.post("/auth/refresh", summary="[호환] 토큰 갱신")
async def compat_refresh(payload: Dict[str, Any]):
    supplied = (payload or {}).get("refreshToken") or ""
    if not hmac.compare_digest(supplied, REFRESH_TOKEN):
        return fail(401, "E4010", "리프레시 토큰이 유효하지 않습니다")
    return ok({"accessToken": ACCESS_TOKEN, "refreshToken": REFRESH_TOKEN})


@router.get("/auth/me", summary="[호환] 내 정보")
def compat_me(user: Optional[Dict[str, Any]] = Depends(current_user)):
    if user is None:
        return fail(401, "E4010", "로그인이 필요합니다")
    return ok(user)


# ---------------------------------------------------------------------------
# 대시보드
# ---------------------------------------------------------------------------
@router.get("/monitoring/occupancy-map", summary="[호환] 재실 맵 (E-01)")
def compat_occupancy_map(
    user: Optional[Dict[str, Any]] = Depends(current_user), db: Session = Depends(get_db)
):
    if user is None:
        return fail(401, "E4010", "로그인이 필요합니다")

    spaces = []
    for r in _latest_per_room(db):
        power_w = r.power or 0.0
        occupied = bool(r.occupancy)
        spaces.append(
            {
                "spaceId": make_space_id(r.building, r.floor, r.room_id),
                "code": r.room_id.upper(),
                "name": f"{r.building} {r.floor} {r.room_id}",
                "floor": floor_number(r.floor),
                "occupied": occupied,
                "powerW": round(power_w, 1),
                "wasteFlag": (not occupied) and power_w > WASTE_POWER_W,
            }
        )
    return ok({"spaces": spaces})


@router.get("/monitoring/realtime-power", summary="[호환] 실시간 전력 (E-03)")
def compat_realtime_power(
    user: Optional[Dict[str, Any]] = Depends(current_user), db: Session = Depends(get_db)
):
    if user is None:
        return fail(401, "E4010", "로그인이 필요합니다")

    total_w = waste_w = 0.0
    for r in _latest_per_room(db):
        power_w = r.power or 0.0
        total_w += power_w
        if not r.occupancy:
            waste_w += power_w
    return ok({"totalW": round(total_w, 1), "wasteW": round(waste_w, 1)})


@router.get("/monitoring/savings", summary="[호환] 절감량 (E-04)")
def compat_savings(
    user: Optional[Dict[str, Any]] = Depends(current_user), db: Session = Depends(get_db)
):
    if user is None:
        return fail(401, "E4010", "로그인이 필요합니다")

    end = _data_anchor(db)
    start = end - timedelta(hours=24)

    saved_wh = baseline_wh = 0.0
    for r in _latest_per_room(db):
        result = analytics.compute_savings(db, r.building, r.floor, r.room_id, start, end)
        if result:
            saved_wh += result["saved_kwh"] * 1000
            baseline_wh += result["baseline_kwh"] * 1000

    rate = (saved_wh / baseline_wh) if baseline_wh > 0 else 0.0
    return ok({"todaySavingWh": round(saved_wh), "savingRate": round(rate, 4)})


@router.get("/notifications", summary="[호환] 알림 목록 (H-02)")
def compat_notifications(user: Optional[Dict[str, Any]] = Depends(current_user)):
    if user is None:
        return fail(401, "E4010", "로그인이 필요합니다")
    # 이 서버의 알림은 아웃바운드 웹훅(서버 -> 서버)이라 브라우저가 받을 수 없다.
    # 헤더의 벨 아이콘이 깨지지 않도록 빈 목록을 돌려준다.
    return ok({"items": [], "unreadCount": 0})


# ---------------------------------------------------------------------------
# 절전 추천 — 재실 확률 테이블에서 만들어낸다
# ---------------------------------------------------------------------------
WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


@router.get("/recommendations", summary="[호환] 절전 추천 목록 (F-01)")
def compat_recommendations(
    user: Optional[Dict[str, Any]] = Depends(current_user), db: Session = Depends(get_db)
):
    if user is None:
        return fail(401, "E4010", "로그인이 필요합니다")

    threshold = settings.analytics_idle_threshold
    cells = (
        db.query(OccupancyProbability)
        .filter(OccupancyProbability.probability < threshold)
        .order_by(OccupancyProbability.probability, desc(OccupancyProbability.sample_count))
        .limit(20)
        .all()
    )

    items = []
    for i, cell in enumerate(cells, start=1):
        # 그 시간대에 실제로 얼마나 쓰는지를 절감 기대치로 쓴다.
        avg_power = (
            db.query(func.avg(SensorReading.power))
            .filter(
                SensorReading.building == cell.building,
                SensorReading.floor == cell.floor,
                SensorReading.room_id == cell.room_id,
            )
            .scalar()
            or 0.0
        )
        items.append(
            {
                "recommendationId": f"rec-{cell.id}",
                "spaceId": make_space_id(cell.building, cell.floor, cell.room_id),
                "days": [WEEKDAY_CODES[cell.weekday]],
                "startTime": f"{cell.hour:02d}:00",
                "endTime": f"{(cell.hour + 1) % 24:02d}:00",
                "action": "LIGHT_OFF",
                "actionLabel": "조명이 자동으로 꺼집니다",
                "reason": (
                    f"관측 {cell.sample_count}회 기준 이 시간대 재실 확률 "
                    f"{cell.probability * 100:.0f}%"
                ),
                "confidence": round(1.0 - cell.probability, 2),
                "expectedSavingWh": round(avg_power),
                "status": "PENDING",
            }
        )
    return ok({"items": items})
