import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.compat_admin_routes import router as compat_admin_router
from .api.compat_common import CompatError, compat_error_handler
from .api.compat_routes import router as compat_router
from .api.device_routes import router as device_router
from .auto_control import auto_control_service
from .api.routes import router as api_router
from .api.webhook_routes import router as webhook_router
from .config import get_settings
from .database import init_db
from .schemas import HealthOut
from .mqtt_client import mqtt_service
from .webhooks import webhook_dispatcher

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    webhook_dispatcher.start()
    mqtt_service.start()
    auto_control_service.start()
    yield
    auto_control_service.stop()
    mqtt_service.stop()
    webhook_dispatcher.stop()


API_DESCRIPTION = """
빛가람 AI·ICT 경진대회 출품작 - 스마트 에너지 절약 시스템의 백엔드 API입니다.

센서 노드가 MQTT로 보낸 재실/전력/온도 데이터를 저장하고, 대시보드가 쓸 조회 API를 제공합니다.

**프론트엔드가 알아야 할 것**

- 모든 시각 필드는 UTC이며 `2026-08-20T08:57:07.480989Z` 처럼 끝에 `Z`가 붙습니다.
  JS에서는 `new Date(값)` 하면 자동으로 로컬(KST) 시간으로 변환됩니다.
- `device_timestamp`만 예외로 UNIX epoch **초** 단위 정수입니다. JS에서는 `new Date(값 * 1000)`.
- 목록 API는 데이터가 없으면 빈 배열 `[]`, 단건 API는 404를 반환합니다.
- 실시간 갱신은 5~10초 간격 폴링을 권장합니다. (웹훅은 서버 대 서버 기능이라 브라우저에서 받을 수 없습니다)
- 자세한 연동 가이드: docs/frontend-guide.md
"""

app = FastAPI(
    title=settings.app_name,
    description=API_DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
)

origins = [o.strip() for o in settings.cors_origins.split(",")] if settings.cors_origins else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")
app.include_router(webhook_router, prefix="/api")
# ESP32 노드가 HTTP로 직접 붙는 경로 (/api/sensors/data, /api/spaces/{id}/actuator/latest)
app.include_router(device_router, prefix="/api")

# 프론트엔드는 /auth, /monitoring 처럼 접두사 없이 부른다.
# 다만 화면을 같은 주소에서 서빙하면 /spaces, /devices, /users 가 화면 경로와
# 겹치므로, 그때는 COMPAT_API_PREFIX 로 API 쪽을 떼어놓는다.
if settings.compat_api_enabled:
    # 실패 응답도 {success, data, error} 봉투로 나가야 프론트가 error.code 로 분기한다.
    app.add_exception_handler(CompatError, compat_error_handler)
    _prefix = settings.compat_api_prefix.rstrip("/")
    app.include_router(compat_router, prefix=_prefix)
    app.include_router(compat_admin_router, prefix=_prefix)
    _startup_logger = logging.getLogger("smart_energy.main")
    _startup_logger.warning("=" * 72)
    _startup_logger.warning("  프론트엔드 호환 레이어가 켜져 있습니다 (시연용 구성)")
    _startup_logger.warning("  /auth/login 은 단순화한 시연용 로그인입니다.")
    _startup_logger.warning("  인터넷에 공개된 서버에는 올리지 마세요.")
    _startup_logger.warning("  끄려면 .env 에 COMPAT_API_ENABLED=false")
    _startup_logger.warning("=" * 72)


@app.get(
    "/health",
    response_model=HealthOut,
    tags=["dashboard"],
    summary="헬스체크",
    description="서버가 살아있는지, MQTT 수신이 켜져 있고 연결되어 있는지 확인합니다.",
)
def health():
    return {
        "status": "ok",
        "mqtt_enabled": settings.mqtt_enabled,
        "mqtt_connected": mqtt_service.is_connected(),
    }


# ---------------------------------------------------------------------------
# 빌드된 프론트엔드 서빙
#
# API 라우터를 모두 등록한 뒤에 붙여야 한다. FastAPI 는 등록 순서로 매칭하므로,
# 아래 catch-all 이 먼저 오면 API 요청까지 가로챈다.
# ---------------------------------------------------------------------------
_frontend_dir = Path(settings.frontend_dir)
if (_frontend_dir / "index.html").is_file():
    _assets = _frontend_dir / "assets"
    if _assets.is_dir():
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str):
        """
        실제 파일이 있으면 그 파일을, 없으면 index.html 을 준다.

        /spaces 같은 주소는 서버에 파일이 없지만 프론트의 화면 경로다.
        index.html 을 돌려줘야 브라우저에서 라우팅이 이어진다.
        """
        candidate = _frontend_dir / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_frontend_dir / "index.html")

    logging.getLogger("smart_energy.main").info(
        "프론트엔드를 함께 서빙합니다 (%s). API 접두사: %s",
        _frontend_dir,
        settings.compat_api_prefix or "(없음)",
    )
