"""
프론트엔드 호환 레이어 공통부 — 응답 봉투, 인증, 공간 변환.

프론트는 모든 응답을 {success, data, error} 로 받고 실패 시 error.code 로 분기한다.
엔드포인트가 30개 가까이 되므로 인증 확인을 매번 되풀이하지 않도록 예외로 처리한다.
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import AppUser, AuthToken, SensorReading, Space
from ..utils import utcnow

logger = logging.getLogger("smart_energy.compat")

# 시연용 계정. auth_demo_account 가 켜져 있을 때만 만들어진다.
DEMO_LOGIN_ID = "demo"
DEMO_PASSWORD = "demo1234"

_PBKDF2_ITERATIONS = 200_000

# 토큰 서명 키. 설정이 없으면 기동할 때마다 새로 만든다(재시작하면 세션이 끊긴다).
# 공개 배포에서는 AUTH_SECRET 을 반드시 지정할 것.
_secret = get_settings().auth_secret
if not _secret:
    _secret = secrets.token_urlsafe(32)
    logging.getLogger("smart_energy.compat").warning(
        "AUTH_SECRET 이 설정되지 않아 임시 키를 생성했습니다. "
        "서버를 재시작하면 로그인이 모두 풀립니다. 배포 시에는 .env 에 지정하세요."
    )
_TOKEN_KEY = _secret.encode("utf-8")


# ---------------------------------------------------------------------------
# 비밀번호
#
# PBKDF2-HMAC-SHA256 으로 사용자마다 다른 소금을 섞어 저장한다.
# 예전에는 SHA-256 한 번이라 대입 공격에 약했다. 기존 해시도 읽을 수 있게 해두고,
# 그 계정이 로그인에 성공하면 새 형식으로 조용히 갈아끼운다.
# ---------------------------------------------------------------------------
def hash_password(raw: str, salt: Optional[bytes] = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _legacy_hash(raw: str) -> str:
    return hashlib.sha256(f"smart-energy:{raw}".encode()).hexdigest()


def verify_password(raw: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, iterations, salt_hex, digest_hex = stored.split("$")
            digest = hashlib.pbkdf2_hmac(
                "sha256", raw.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
            )
            return hmac.compare_digest(digest.hex(), digest_hex)
        except (ValueError, TypeError):
            return False
    # 옛 형식(소금 없는 SHA-256)
    return hmac.compare_digest(_legacy_hash(raw), stored)


def needs_rehash(stored: str) -> bool:
    return not stored.startswith("pbkdf2$")


# ---------------------------------------------------------------------------
# 토큰
#
# 만료 시각을 담고 서명한다. 예전에는 사용자 ID만 넣어 고정값을 유도했기 때문에
# 한 번 새어나가면 영구히 유효했다.
# 형식: base64url(payload).base64url(signature)
# ---------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(kind: str, user_id: str) -> str:
    settings = get_settings()
    ttl = (
        timedelta(minutes=settings.auth_access_ttl_minutes)
        if kind == "access"
        else timedelta(days=settings.auth_refresh_ttl_days)
    )
    payload = {
        "u": user_id,
        "k": kind,
        "exp": int((datetime.now(timezone.utc) + ttl).timestamp()),
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64(hmac.new(_TOKEN_KEY, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def decode_token(token: str, kind: str) -> Optional[str]:
    """유효하면 user_id, 아니면 None. 위조·만료·종류 불일치를 모두 걸러낸다."""
    try:
        body, signature = token.split(".")
        expected = _b64(hmac.new(_TOKEN_KEY, body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_unb64(body).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    if payload.get("k") != kind:
        return None
    if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
        return None
    return payload.get("u")


# ---------------------------------------------------------------------------
# 일회용 토큰 (이메일 인증 · 비밀번호 재설정)
#
# 메일로 보내는 링크에는 원문을 싣고 DB 에는 해시만 남긴다. DB 를 볼 수 있는
# 사람이 남의 비밀번호를 재설정하는 링크를 만들어낼 수 없어야 한다.
# ---------------------------------------------------------------------------
VERIFY_EMAIL = "VERIFY_EMAIL"
RESET_PASSWORD = "RESET_PASSWORD"


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_auth_token(db: Session, user: AppUser, purpose: str, ttl: timedelta) -> str:
    """
    새 토큰을 만들고 원문을 돌려준다. 원문은 이 순간에만 존재한다.

    같은 용도의 지난 토큰은 지운다. 재설정 링크를 두 번 요청하면 앞의 것은
    더 이상 듣지 않아야 한다.
    """
    db.query(AuthToken).filter(
        AuthToken.user_id == user.user_id, AuthToken.purpose == purpose
    ).delete()

    raw = secrets.token_urlsafe(32)
    db.add(
        AuthToken(
            token_hash=_token_hash(raw),
            user_id=user.user_id,
            purpose=purpose,
            expires_at=utcnow() + ttl,
        )
    )
    db.commit()
    return raw


def consume_auth_token(db: Session, raw: str, purpose: str) -> Optional[AppUser]:
    """
    토큰을 쓰고 폐기한다. 쓸 수 없는 토큰이면 None.

    만료·재사용·용도 불일치를 모두 같은 결과로 돌려준다. 어느 쪽이 틀렸는지
    알려주면 유효한 토큰을 찾는 데 쓰일 수 있다.
    """
    if not raw:
        return None
    token = db.get(AuthToken, _token_hash(raw))
    if token is None or token.purpose != purpose:
        return None
    if token.used_at is not None or token.expires_at < utcnow():
        return None

    user = db.get(AppUser, token.user_id)
    if user is None:
        return None

    # 한 번 쓰면 사라진다. 링크가 메일함에 남아 있어도 다시 통하지 않는다.
    db.delete(token)
    db.commit()
    return user


def purge_expired_tokens(db: Session) -> None:
    """만료된 토큰을 치운다. 남겨둘 이유가 없다."""
    db.query(AuthToken).filter(AuthToken.expires_at < utcnow()).delete()
    db.commit()


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
def ensure_seed_users(db: Session) -> None:
    """
    첫 기동에 로그인할 수 있는 계정을 마련한다.

    - AUTH_BOOTSTRAP_ADMIN_ID/PASSWORD 가 있으면 그 관리자를 만든다 (공개 배포용)
    - AUTH_DEMO_ACCOUNT 가 켜져 있으면 demo/demo1234 도 만든다 (시연용)

    공개 배포에서는 AUTH_DEMO_ACCOUNT=false 로 두고 부트스트랩 관리자만 쓸 것.
    """
    settings = get_settings()

    admin_id = settings.auth_bootstrap_admin_id
    admin_pw = settings.auth_bootstrap_admin_password
    if admin_id and admin_pw:
        if db.query(AppUser).filter_by(login_id=admin_id).first() is None:
            db.add(
                AppUser(
                    user_id=f"u-{secrets.token_hex(4)}",
                    login_id=admin_id,
                    name="관리자",
                    email="",
                    role="ADMIN",
                    status="ACTIVE",
                    password_hash=hash_password(admin_pw),
                    # 이메일이 없는 계정이라 인증할 대상이 없다.
                    email_verified=True,
                )
            )
            db.commit()
            logger.info("부트스트랩 관리자 계정을 만들었습니다: %s", admin_id)

    if settings.auth_demo_account:
        if db.query(AppUser).filter_by(login_id=DEMO_LOGIN_ID).first() is None:
            db.add(
                AppUser(
                    user_id="u-1",
                    login_id=DEMO_LOGIN_ID,
                    name="관리자",
                    email="demo@enersave.io",
                    role="ADMIN",
                    status="ACTIVE",
                    password_hash=hash_password(DEMO_PASSWORD),
                    email_verified=True,
                )
            )
            db.commit()


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
        # 관리자가 승인 전에 확인할 수 있게 노출한다. 인증되지 않은 주소는
        # 비밀번호 재설정을 받을 수 없다.
        "emailVerified": bool(user.email_verified),
    }


def require_user(
    authorization: Optional[str] = Header(default=None), db: Session = Depends(get_db)
) -> AppUser:
    """로그인한 사용자. 조회 기능은 여기까지면 된다."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise unauthorized()

    user_id = decode_token(authorization.split(" ", 1)[1].strip(), "access")
    if user_id is None:
        # 만료도 위조도 같은 코드로 돌려준다. 프론트가 이 코드를 보고 토큰을 갱신한다.
        raise unauthorized("세션이 만료되었습니다")

    user = db.get(AppUser, user_id)
    if user is None or user.status != "ACTIVE":
        raise unauthorized()
    return user


def require_admin(user: AppUser = Depends(require_user)) -> AppUser:
    """
    쓰기 권한. 공간·디바이스·사용자를 바꾸거나 기기를 제어하는 기능에 건다.

    예전에는 로그인만 하면 누구나 공간을 지우고 제어 명령을 보낼 수 있었다.
    """
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
