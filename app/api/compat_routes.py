"""
프론트엔드(kepcoproject/Client) 호환 레이어 — 인증 · 대시보드 · 추천 · 알림.

프론트는 이 서버와 다른 규격을 전제로 만들어져 있다. 경로가 다르고, 응답을
{success, data, error} 봉투로 받으며, 공간을 spaceId 하나로 식별하고, 모든 요청에
Bearer 토큰을 싣는다. 이 모듈은 기존 /api/* 를 그대로 둔 채 그 규격으로 같은
데이터를 다시 내보낸다.

로그인은 공모전 시연 범위에 맞춰 단순화했다. 교내망이나 노트북에서 도는 시연에는
충분하지만, 인터넷에 공개된 서버에는 올리지 말 것 (CORS_ORIGINS=* 와 겹쳐 누구나
들어올 수 있다). COMPAT_API_ENABLED=false 로 끌 수 있다.

정식 서비스로 갈 때 교체할 것: 비밀번호 해싱 강화(현재 SHA-256 단순 해시),
서명·만료가 있는 토큰.

공간·디바이스·사용자·제어·리포트는 compat_admin_routes.py 에 있다.
"""
import logging
import uuid
from datetime import timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from .. import analytics
from ..config import get_settings
from ..database import get_db
from ..models import (
    AppUser,
    Device,
    DeviceStatusEnum,
    Notification,
    OccupancyProbability,
    RecommendationState,
    SensorReading,
)
from ..utils import utcnow
from .compat_common import (
    CompatError,
    data_anchor,
    derive_token,
    ensure_demo_user,
    ensure_space,
    epoch_to_iso_z,
    get_space,
    hash_password,
    iso_z,
    latest_reading_per_room,
    not_found,
    ok,
    require_user,
    to_epoch,
    unauthorized,
    user_out,
)

logger = logging.getLogger("smart_energy.compat")

router = APIRouter(tags=["frontend-compat"])

settings = get_settings()

# 재실이 아닌데 이 값을 넘으면 낭비로 본다 (프론트 목업과 동일 기준).
WASTE_POWER_W = 50.0

WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------
@router.post("/auth/login", summary="[호환] 로그인 (A-01)")
async def compat_login(payload: Dict[str, Any], db: Session = Depends(get_db)):
    ensure_demo_user(db)
    login_id = (payload or {}).get("loginId") or ""
    password = (payload or {}).get("password") or ""

    user = db.query(AppUser).filter_by(login_id=login_id).first()
    if user is None or user.password_hash != hash_password(password):
        raise unauthorized("아이디 또는 비밀번호가 올바르지 않습니다")
    if user.status != "ACTIVE":
        raise CompatError(403, "E4030", "승인 대기 중인 계정입니다. 관리자에게 문의하세요")

    logger.info("[호환] 로그인: %s", login_id)
    return ok(
        {
            "accessToken": derive_token("access", user.user_id),
            "refreshToken": derive_token("refresh", user.user_id),
            "user": user_out(user),
        }
    )


@router.post("/auth/refresh", summary="[호환] 토큰 갱신 (A-02)")
async def compat_refresh(payload: Dict[str, Any], db: Session = Depends(get_db)):
    ensure_demo_user(db)
    supplied = (payload or {}).get("refreshToken") or ""
    for user in db.query(AppUser).filter(AppUser.status == "ACTIVE").all():
        if supplied == derive_token("refresh", user.user_id):
            return ok(
                {
                    "accessToken": derive_token("access", user.user_id),
                    "refreshToken": derive_token("refresh", user.user_id),
                }
            )
    raise unauthorized("리프레시 토큰이 유효하지 않습니다")


@router.get("/auth/me", summary="[호환] 내 정보 (A-04)")
def compat_me(user: AppUser = Depends(require_user)):
    return ok(user_out(user))


@router.post("/auth/signup", summary="[호환] 회원가입 (A-05)")
async def compat_signup(payload: Dict[str, Any], db: Session = Depends(get_db)):
    body = payload or {}
    login_id = (body.get("loginId") or "").strip()
    if not login_id:
        raise CompatError(400, "E4000", "아이디를 입력하세요")
    if db.query(AppUser).filter_by(login_id=login_id).first():
        raise CompatError(409, "E4090", "이미 사용 중인 아이디입니다")

    user = AppUser(
        user_id=f"u-{uuid.uuid4().hex[:8]}",
        login_id=login_id,
        name=(body.get("name") or login_id),
        email=(body.get("email") or ""),
        role="MEMBER",
        # 가입은 즉시 사용이 아니라 관리자 승인 대기 상태로 들어간다.
        status="PENDING",
        password_hash=hash_password(body.get("password") or ""),
    )
    db.add(user)
    db.commit()
    logger.info("[호환] 회원가입 신청: %s", login_id)
    return ok({"userId": user.user_id, "status": user.status})


@router.get("/auth/check-id", summary="[호환] 아이디 중복 확인 (A-06)")
def compat_check_id(loginId: str = Query(...), db: Session = Depends(get_db)):
    ensure_demo_user(db)
    taken = db.query(AppUser).filter_by(login_id=loginId).first() is not None
    return ok({"available": not taken})


@router.patch("/auth/password", summary="[호환] 비밀번호 변경")
async def compat_change_password(
    payload: Dict[str, Any], user: AppUser = Depends(require_user), db: Session = Depends(get_db)
):
    body = payload or {}
    if user.password_hash != hash_password(body.get("currentPassword") or ""):
        raise CompatError(400, "E4001", "현재 비밀번호가 올바르지 않습니다")
    new_password = body.get("newPassword") or ""
    if len(new_password) < 8:
        raise CompatError(400, "E4002", "새 비밀번호는 8자 이상이어야 합니다")
    user.password_hash = hash_password(new_password)
    db.commit()
    return ok({"changed": True})


# ---------------------------------------------------------------------------
# 대시보드
# ---------------------------------------------------------------------------
@router.get("/monitoring/occupancy-map", summary="[호환] 재실 맵 (E-01)")
def compat_occupancy_map(user: AppUser = Depends(require_user), db: Session = Depends(get_db)):
    spaces = []
    for r in latest_reading_per_room(db):
        space = ensure_space(db, r.building, r.floor, r.room_id)
        power_w = r.power or 0.0
        occupied = bool(r.occupancy)
        spaces.append(
            {
                "spaceId": space.space_id,
                "code": space.code,
                "name": space.name,
                "floor": space.floor_number,
                "occupied": occupied,
                "powerW": round(power_w, 1),
                "wasteFlag": (not occupied) and power_w > WASTE_POWER_W,
            }
        )
    db.commit()
    return ok({"spaces": spaces})


@router.get("/monitoring/realtime-power", summary="[호환] 실시간 전력 (E-03)")
def compat_realtime_power(user: AppUser = Depends(require_user), db: Session = Depends(get_db)):
    total_w = waste_w = 0.0
    for r in latest_reading_per_room(db):
        power_w = r.power or 0.0
        total_w += power_w
        if not r.occupancy:
            waste_w += power_w
    return ok({"totalW": round(total_w, 1), "wasteW": round(waste_w, 1)})


@router.get("/monitoring/savings", summary="[호환] 절감량 (E-04)")
def compat_savings(user: AppUser = Depends(require_user), db: Session = Depends(get_db)):
    end = data_anchor(db)
    start = end - timedelta(hours=24)

    saved_wh = baseline_wh = 0.0
    for r in latest_reading_per_room(db):
        result = analytics.compute_savings(db, r.building, r.floor, r.room_id, start, end)
        if result:
            saved_wh += result["saved_kwh"] * 1000
            baseline_wh += result["baseline_kwh"] * 1000

    rate = (saved_wh / baseline_wh) if baseline_wh > 0 else 0.0
    return ok({"todaySavingWh": round(saved_wh), "savingRate": round(rate, 4)})


def occupied_periods(db: Session, space, hours: int):
    """
    재실이 연속으로 true 인 구간을 묶어 [{start, end}] 로 만든다.
    그래프에서 재실 구간을 음영으로 칠하는 데 쓰인다.
    """
    end_dt = data_anchor(db)
    start_ts = to_epoch(end_dt) - hours * 3600
    rows = (
        db.query(SensorReading)
        .filter(
            SensorReading.building == space.building,
            SensorReading.floor == space.floor,
            SensorReading.room_id == space.room_id,
            SensorReading.device_timestamp >= start_ts,
        )
        .order_by(SensorReading.device_timestamp)
        .all()
    )

    periods, run_start, prev_ts = [], None, None
    for row in rows:
        if row.occupancy:
            if run_start is None:
                run_start = row.device_timestamp
        elif run_start is not None:
            periods.append(
                {"start": epoch_to_iso_z(run_start), "end": epoch_to_iso_z(prev_ts or run_start)}
            )
            run_start = None
        prev_ts = row.device_timestamp
    if run_start is not None and prev_ts is not None:
        periods.append({"start": epoch_to_iso_z(run_start), "end": epoch_to_iso_z(prev_ts)})
    return periods


@router.get("/monitoring/occupancy-history/{space_id}", summary="[호환] 재실 이력 (E-02)")
def compat_occupancy_history(
    space_id: str,
    hours: int = Query(default=1, ge=1, le=720),
    interval: str = Query(default="1m"),
    user: AppUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    space = get_space(db, space_id)
    return ok({"periods": occupied_periods(db, space, hours)})


# ---------------------------------------------------------------------------
# 알림 (H-02)
# ---------------------------------------------------------------------------
def _sync_notifications(db: Session) -> None:
    """
    지금 상태에서 알릴 만한 것을 알림 테이블에 반영한다.
    같은 내용이 계속 쌓이지 않도록 동일 메시지가 이미 있으면 건너뛴다.
    """
    messages = []
    for r in latest_reading_per_room(db):
        power_w = r.power or 0.0
        if not r.occupancy and power_w > WASTE_POWER_W:
            space = ensure_space(db, r.building, r.floor, r.room_id)
            messages.append(("WARNING", f"{space.code} 공실인데 {power_w:.0f}W 전력 소비 중입니다"))

    for device in db.query(Device).filter(Device.status == DeviceStatusEnum.offline).all():
        messages.append(("CRITICAL", f"{device.device_id} 노드가 오프라인입니다"))

    existing = {row[0] for row in db.query(Notification.message).all()}
    for level, message in messages:
        if message not in existing:
            db.add(Notification(level=level, message=message))
    db.commit()


@router.get("/notifications", summary="[호환] 알림 목록 (H-02)")
def compat_notifications(user: AppUser = Depends(require_user), db: Session = Depends(get_db)):
    _sync_notifications(db)
    rows = db.query(Notification).order_by(desc(Notification.created_at)).limit(30).all()
    items = [
        {
            "notificationId": f"ntf-{n.notification_id}",
            "level": n.level,
            "message": n.message,
            "createdAt": iso_z(n.created_at),
            "read": n.read,
        }
        for n in rows
    ]
    return ok({"items": items, "unreadCount": sum(1 for n in rows if not n.read)})


# ---------------------------------------------------------------------------
# 절전 추천 (F-01, F-02) — 재실 확률 테이블에서 생성한다
# ---------------------------------------------------------------------------
def _build_recommendations(db: Session):
    threshold = settings.analytics_idle_threshold
    cells = (
        db.query(OccupancyProbability)
        .filter(OccupancyProbability.probability < threshold)
        .order_by(OccupancyProbability.probability, desc(OccupancyProbability.sample_count))
        .limit(30)
        .all()
    )

    items = []
    for cell in cells:
        space = ensure_space(db, cell.building, cell.floor, cell.room_id)
        rec_id = f"rec-{cell.id}"
        state = db.get(RecommendationState, rec_id)

        # 그 공간의 평균 전력을 절감 기대치로 쓴다 (1시간 켜져 있었다고 가정).
        samples = (
            db.query(SensorReading.power)
            .filter(
                SensorReading.building == cell.building,
                SensorReading.floor == cell.floor,
                SensorReading.room_id == cell.room_id,
                SensorReading.power.isnot(None),
            )
            .limit(200)
            .all()
        )
        avg_w = sum(s[0] for s in samples) / len(samples) if samples else 0.0

        items.append(
            {
                "recommendationId": rec_id,
                "spaceId": space.space_id,
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
                "expectedSavingWh": round(avg_w),
                "status": state.status if state else "PENDING",
                "scheduleId": state.schedule_id if state else None,
                "comment": state.comment if state else None,
            }
        )
    db.commit()
    return items


@router.get("/recommendations", summary="[호환] 절전 추천 목록 (F-01)")
def compat_recommendations(user: AppUser = Depends(require_user), db: Session = Depends(get_db)):
    return ok({"items": _build_recommendations(db)})


def _set_recommendation_state(
    db: Session, rec_id: str, status: str, comment: Optional[str] = None
) -> RecommendationState:
    cell_id = rec_id.replace("rec-", "", 1)
    if not cell_id.isdigit() or db.get(OccupancyProbability, int(cell_id)) is None:
        raise not_found("추천을 찾을 수 없습니다")

    state = db.get(RecommendationState, rec_id)
    if state is None:
        state = RecommendationState(recommendation_id=rec_id)
        db.add(state)
    state.status = status
    state.comment = comment
    state.schedule_id = f"sch-{cell_id}" if status == "APPLIED" else None
    state.updated_at = utcnow()
    db.commit()
    return state


@router.post("/recommendations/{rec_id}/apply", summary="[호환] 추천 적용 (F-02)")
def compat_apply_recommendation(
    rec_id: str, user: AppUser = Depends(require_user), db: Session = Depends(get_db)
):
    state = _set_recommendation_state(db, rec_id, "APPLIED")
    logger.info("[호환] 추천 적용: %s", rec_id)
    return ok(
        {"recommendationId": rec_id, "status": state.status, "scheduleId": state.schedule_id}
    )


@router.post("/recommendations/{rec_id}/reject", summary="[호환] 추천 반려 (F-02)")
async def compat_reject_recommendation(
    rec_id: str,
    payload: Optional[Dict[str, Any]] = None,
    user: AppUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    comment = (payload or {}).get("comment")
    state = _set_recommendation_state(db, rec_id, "REJECTED", comment)
    logger.info("[호환] 추천 반려: %s", rec_id)
    return ok({"recommendationId": rec_id, "status": state.status, "comment": state.comment})
