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
import re
import uuid
from datetime import timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from .. import analytics, mailer
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
    RESET_PASSWORD,
    VERIFY_EMAIL,
    CompatError,
    consume_auth_token,
    data_anchor,
    decode_token,
    ensure_seed_users,
    issue_token,
    needs_rehash,
    verify_password,
    ensure_space,
    epoch_to_iso_z,
    get_space,
    hash_password,
    iso_z,
    issue_auth_token,
    latest_reading_per_room,
    purge_expired_tokens,
    not_found,
    ok,
    require_admin,
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

# 회원가입 입력 규칙. 화면(Signup.jsx)의 검증과 같은 기준으로 맞춘다.
MIN_LOGIN_ID_LENGTH = 4
MIN_PASSWORD_LENGTH = 8
LOGIN_ID_PATTERN = re.compile(r"[A-Za-z0-9_]+")
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def _text(body: Dict[str, Any], field: str, label: str, *, strip: bool = True) -> str:
    """
    본문에서 문자열 값을 꺼낸다. 없으면 빈 문자열.

    그냥 (body.get(x) or "").strip() 을 쓰면 숫자나 배열이 왔을 때 AttributeError 가
    올라가 500 이 난다. 인증이 없는 경로들이라 아무나 500 을 만들 수 있었고,
    500 응답에는 봉투가 없어 화면이 사유를 읽지도 못한다.
    """
    value = body.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CompatError(400, "E4000", f"{label} 값이 올바르지 않습니다")
    return value.strip() if strip else value


def _optional_text(body: Dict[str, Any], field: str, label: str) -> Optional[str]:
    """보내지 않은 것(None)과 빈 값을 구분해야 하는 항목에 쓴다."""
    if body.get(field) is None:
        return None
    return _text(body, field, label, strip=False)


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------
@router.post("/auth/login", summary="[호환] 로그인 (A-01)")
async def compat_login(payload: Dict[str, Any], db: Session = Depends(get_db)):
    ensure_seed_users(db)
    body = payload or {}
    login_id = _text(body, "loginId", "아이디")
    password = _text(body, "password", "비밀번호", strip=False)

    user = db.query(AppUser).filter_by(login_id=login_id).first()
    if user is None or not verify_password(password, user.password_hash):
        raise unauthorized("아이디 또는 비밀번호가 올바르지 않습니다")
    if user.status != "ACTIVE":
        raise CompatError(403, "E4030", "승인 대기 중인 계정입니다. 관리자에게 문의하세요")

    # 옛 형식(소금 없는 SHA-256)으로 저장된 계정은 로그인 성공 시 조용히 갈아끼운다.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()
        logger.info("[호환] 비밀번호 해시를 새 형식으로 갱신: %s", login_id)

    logger.info("[호환] 로그인: %s", login_id)
    return ok(
        {
            "accessToken": issue_token("access", user.user_id, user.password_hash),
            "refreshToken": issue_token("refresh", user.user_id, user.password_hash),
            "user": user_out(user),
        }
    )


@router.post("/auth/refresh", summary="[호환] 토큰 갱신 (A-02)")
async def compat_refresh(payload: Dict[str, Any], db: Session = Depends(get_db)):
    ensure_seed_users(db)
    raw_token = (payload or {}).get("refreshToken") or ""
    user_id = decode_token(raw_token, "refresh")
    if user_id is None:
        raise unauthorized("리프레시 토큰이 유효하지 않습니다")

    user = db.get(AppUser, user_id)
    if user is None or user.status != "ACTIVE":
        raise unauthorized("리프레시 토큰이 유효하지 않습니다")

    # 비밀번호가 바뀐 뒤의 토큰이면 여기서 끊는다. 액세스 토큰은 1시간이면
    # 만료되지만 리프레시는 14일이라, 여기를 막지 않으면 재설정이 무의미하다.
    if decode_token(raw_token, "refresh", user.password_hash) is None:
        raise unauthorized("비밀번호가 바뀌었습니다. 다시 로그인하세요")

    return ok(
        {
            "accessToken": issue_token("access", user.user_id, user.password_hash),
            "refreshToken": issue_token("refresh", user.user_id, user.password_hash),
        }
    )


@router.get("/auth/me", summary="[호환] 내 정보 (A-04)")
def compat_me(user: AppUser = Depends(require_user)):
    return ok(user_out(user))


@router.post("/auth/signup", summary="[호환] 회원가입 (A-05)")
async def compat_signup(
    payload: Dict[str, Any], request: Request, db: Session = Depends(get_db)
):
    body = payload or {}
    login_id = _text(body, "loginId", "아이디")
    password = _text(body, "password", "비밀번호", strip=False)
    password_confirm = _optional_text(body, "passwordConfirm", "비밀번호 확인")
    name = _text(body, "name", "이름")
    email = _text(body, "email", "이메일")

    # 화면에서도 막지만 서버에서 다시 본다. API 를 직접 호출하면 화면 검증을 건너뛸 수 있다.
    if not login_id:
        raise CompatError(400, "E4000", "아이디를 입력하세요")
    if len(login_id) < MIN_LOGIN_ID_LENGTH:
        raise CompatError(400, "E4000", f"아이디는 {MIN_LOGIN_ID_LENGTH}자 이상이어야 합니다")
    if not LOGIN_ID_PATTERN.fullmatch(login_id):
        raise CompatError(400, "E4000", "아이디는 영문·숫자·밑줄만 쓸 수 있습니다")

    if not name:
        raise CompatError(400, "E4000", "이름을 입력하세요")

    if not email:
        raise CompatError(400, "E4000", "이메일을 입력하세요")
    if not EMAIL_PATTERN.fullmatch(email):
        raise CompatError(400, "E4000", "이메일 형식이 올바르지 않습니다")

    if not password:
        raise CompatError(400, "E4000", "비밀번호를 입력하세요")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise CompatError(400, "E4000", f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다")
    # 확인란은 화면에만 있는 값이라 안 보낼 수도 있다. 보냈다면 일치해야 한다.
    if password_confirm is not None and password != password_confirm:
        raise CompatError(400, "E4000", "비밀번호가 서로 다릅니다")

    if db.query(AppUser).filter_by(login_id=login_id).first():
        raise CompatError(409, "E4090", "이미 사용 중인 아이디입니다")

    # 이메일 1개당 계정 1개. 대소문자만 다른 주소는 같은 주소로 본다
    # (Gmail 을 비롯한 대부분의 메일 서비스가 그렇게 다룬다).
    # 저장은 입력한 그대로 두고 비교만 소문자로 맞춘다.
    #
    # DB 유니크 제약을 걸지 않은 이유: 부트스트랩·데모 관리자 계정은 email 이
    # 빈 문자열이라 유니크 인덱스를 만들면 서로 충돌한다. 여기서 막는다.
    if db.query(AppUser).filter(func.lower(AppUser.email) == email.lower()).first():
        raise CompatError(409, "E4090", "이미 가입된 이메일입니다")

    user = AppUser(
        user_id=f"u-{uuid.uuid4().hex[:8]}",
        login_id=login_id,
        name=name,
        email=email,
        role="MEMBER",
        # 가입은 즉시 사용이 아니라 관리자 승인 대기 상태로 들어간다.
        status="PENDING",
        password_hash=hash_password(password),
    )
    db.add(user)
    db.commit()

    # 인증 메일을 보낸다. 메일 서버가 죽어 있어도 가입 자체는 성공시킨다 —
    # 인증은 나중에 다시 보낼 수 있고, 여기서 실패시키면 계정만 사라진다.
    _send_verification(db, request, user)

    logger.info("[호환] 회원가입 신청: %s", login_id)
    return ok(
        {
            "userId": user.user_id,
            "status": user.status,
            "emailVerified": False,
            # 화면이 "메일을 확인하세요" 를 띄울지 판단하는 값
            "verificationSent": True,
        }
    )


# ---------------------------------------------------------------------------
# 이메일 인증 · 비밀번호 재설정
#
# 두 기능은 한 쌍이다. 비밀번호를 잊으면 메일로 되찾는데, 그 메일 주소가
# 본인 것이 아니면 남이 계정을 가져갈 수 있다. 그래서 재설정은 인증이 끝난
# 주소로만 보낸다.
# ---------------------------------------------------------------------------
def _link(request: Request, path: str, token: str) -> str:
    """메일에 넣을 링크. 설정된 공개 주소가 있으면 그것을 쓴다."""
    base = (settings.public_base_url or "").rstrip("/")
    if not base:
        # 리버스 프록시 뒤에서는 틀릴 수 있다. 그래서 PUBLIC_BASE_URL 을 권한다.
        base = str(request.base_url).rstrip("/")
    return f"{base}{path}?token={token}"


def _send_verification(db: Session, request: Request, user: AppUser) -> None:
    token = issue_auth_token(
        db, user, VERIFY_EMAIL, timedelta(hours=settings.email_verify_ttl_hours)
    )
    mailer.send_verification(user.email, user.name, _link(request, "/verify-email", token))


@router.post("/auth/email/verify", summary="[호환] 이메일 인증 확인")
async def compat_verify_email(payload: Dict[str, Any], db: Session = Depends(get_db)):
    token = _text(payload or {}, "token", "인증 링크")
    user = consume_auth_token(db, token, VERIFY_EMAIL)
    if user is None:
        raise CompatError(400, "E4000", "링크가 만료되었거나 이미 사용되었습니다")

    user.email_verified = True
    db.commit()
    logger.info("[호환] 이메일 인증 완료: %s", user.login_id)
    return ok({"verified": True, "loginId": user.login_id})


@router.post("/auth/email/resend", summary="[호환] 인증 메일 다시 보내기")
async def compat_resend_verification(
    payload: Dict[str, Any], request: Request, db: Session = Depends(get_db)
):
    """
    응답은 언제나 같다. 가입된 주소인지 알려주면 어떤 주소가 등록돼 있는지
    확인하는 수단이 된다.
    """
    email = _text(payload or {}, "email", "이메일")
    if email:
        user = db.query(AppUser).filter(func.lower(AppUser.email) == email.lower()).first()
        if user is not None and not user.email_verified:
            _send_verification(db, request, user)
    return ok({"sent": True})


@router.post("/auth/password/forgot", summary="[호환] 비밀번호 재설정 요청")
async def compat_forgot_password(
    payload: Dict[str, Any], request: Request, db: Session = Depends(get_db)
):
    """
    가입한 이메일로 재설정 링크를 보낸다.

    주소가 없든, 인증이 안 됐든, 메일 발송이 실패했든 응답은 같다.
    다르게 답하면 어떤 주소가 가입돼 있는지 알아내는 데 쓰인다.
    """
    ensure_seed_users(db)
    purge_expired_tokens(db)

    email = _text(payload or {}, "email", "이메일")
    if email:
        user = db.query(AppUser).filter(func.lower(AppUser.email) == email.lower()).first()
        # 인증되지 않은 주소로는 보내지 않는다. 아무 주소나 적어두고 그 주소로
        # 재설정 링크를 받을 수 있으면 이메일을 확인하는 의미가 없다.
        if user is not None and user.email_verified:
            token = issue_auth_token(
                db,
                user,
                RESET_PASSWORD,
                timedelta(minutes=settings.password_reset_ttl_minutes),
            )
            mailer.send_password_reset(
                user.email, user.name, _link(request, "/reset-password", token)
            )
            logger.info("[호환] 비밀번호 재설정 링크 발송: %s", user.login_id)
        else:
            logger.info("[호환] 비밀번호 재설정 요청 — 보낼 대상 없음")

    return ok({"sent": True})


@router.post("/auth/password/reset", summary="[호환] 비밀번호 재설정 완료")
async def compat_reset_password(payload: Dict[str, Any], db: Session = Depends(get_db)):
    body = payload or {}
    new_password = _text(body, "newPassword", "새 비밀번호", strip=False)
    confirm = _optional_text(body, "passwordConfirm", "비밀번호 확인")

    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise CompatError(
            400, "E4000", f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다"
        )
    if confirm is not None and new_password != confirm:
        raise CompatError(400, "E4000", "비밀번호가 서로 다릅니다")

    # 토큰 확인은 비밀번호 검사 뒤에 한다. 형식이 틀렸다고 토큰을 태워버리면
    # 사용자가 링크를 다시 받아야 한다.
    user = consume_auth_token(db, _text(body, "token", "재설정 링크"), RESET_PASSWORD)
    if user is None:
        raise CompatError(400, "E4000", "링크가 만료되었거나 이미 사용되었습니다")

    user.password_hash = hash_password(new_password)
    db.commit()
    logger.info("[호환] 비밀번호 재설정 완료: %s", user.login_id)
    return ok({"changed": True})


@router.get("/auth/check-id", summary="[호환] 아이디 중복 확인 (A-06)")
def compat_check_id(loginId: str = Query(...), db: Session = Depends(get_db)):
    ensure_seed_users(db)
    taken = db.query(AppUser).filter_by(login_id=loginId).first() is not None
    return ok({"available": not taken})


@router.patch("/auth/password", summary="[호환] 비밀번호 변경")
async def compat_change_password(
    payload: Dict[str, Any], user: AppUser = Depends(require_user), db: Session = Depends(get_db)
):
    body = payload or {}
    current = _text(body, "currentPassword", "현재 비밀번호", strip=False)
    new_password = _text(body, "newPassword", "새 비밀번호", strip=False)
    if not verify_password(current, user.password_hash):
        raise CompatError(400, "E4001", "현재 비밀번호가 올바르지 않습니다")
    if len(new_password) < 8:
        raise CompatError(400, "E4002", "새 비밀번호는 8자 이상이어야 합니다")
    user.password_hash = hash_password(new_password)
    db.commit()

    # 비밀번호가 바뀌면 예전 토큰은 전부 무효가 된다(다른 기기의 세션도 끊긴다).
    # 방금 스스로 바꾼 사람까지 로그아웃시킬 이유는 없으므로 새 토큰을 함께 준다.
    return ok(
        {
            "changed": True,
            "accessToken": issue_token("access", user.user_id, user.password_hash),
            "refreshToken": issue_token("refresh", user.user_id, user.password_hash),
        }
    )


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
        # 재실 맵(E-01)의 wasteFlag 와 같은 기준이어야 한다. 예전에는 공실이면
        # 대기전력 몇 W 까지 전부 더해서, 방마다 '정상'으로 표시된 곳의 전력이
        # 상단 '낭비 전력' 합계에 섞여 들어갔다. 빨간 방은 하나인데 합계가 더 컸다.
        if not r.occupancy and power_w > WASTE_POWER_W:
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

    낭비 판정은 analytics.detect_waste 가 재실·전력·조도를 함께 보고 내린다.
    대시보드 카드는 공실 낭비만 표시하지만(프론트 문구가 그렇게 고정되어 있다),
    여기서는 채광 낭비도 정확한 문구로 알릴 수 있다.

    같은 내용이 계속 쌓이지 않도록 동일 메시지가 이미 있으면 건너뛴다.
    """
    messages = []
    for r in latest_reading_per_room(db):
        waste = analytics.detect_waste(r.occupancy, r.power, r.lux)
        if waste:
            space = ensure_space(db, r.building, r.floor, r.room_id)
            messages.append(("WARNING", f"{space.code} {waste['message']}"))

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
    rec_id: str, user: AppUser = Depends(require_admin), db: Session = Depends(get_db)
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
    user: AppUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    comment = (payload or {}).get("comment")
    state = _set_recommendation_state(db, rec_id, "REJECTED", comment)
    logger.info("[호환] 추천 반려: %s", rec_id)
    return ok({"recommendationId": rec_id, "status": state.status, "comment": state.comment})
