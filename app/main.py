import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    yield
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
