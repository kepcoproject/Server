from typing import List

from fastapi import APIRouter, HTTPException, Query

from ..config import get_settings
from ..schemas import WebhookConfigOut, WebhookDeliveryOut, WebhookTestOut
from ..webhooks import EVENT_PING, KNOWN_EVENTS, mask_url, webhook_dispatcher

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

settings = get_settings()


@router.get(
    "",
    response_model=WebhookConfigOut,
    summary="웹훅 설정 상태 조회",
    description=(
        "현재 웹훅이 켜져 있는지, 어떤 이벤트를 어디로 보내는지 확인합니다. "
        "URL은 경로가 마스킹되어 나가고 시크릿 값은 노출되지 않습니다."
    ),
)
def get_webhook_config():
    return WebhookConfigOut(
        enabled=webhook_dispatcher.enabled,
        urls=[mask_url(u) for u in webhook_dispatcher.urls],
        events=settings.webhook_events,
        signature_enabled=bool(settings.webhook_secret),
        timeout=settings.webhook_timeout,
        max_retries=settings.webhook_max_retries,
        known_events=list(KNOWN_EVENTS),
    )


@router.get(
    "/deliveries",
    response_model=List[WebhookDeliveryOut],
    summary="최근 웹훅 전송 이력",
    description="최근 전송 시도 결과입니다. 수신 측에 알림이 도착하지 않을 때 status_code와 error로 원인을 좁힐 수 있습니다.",
)
def list_recent_deliveries(limit: int = Query(default=20, ge=1, le=50)):
    return webhook_dispatcher.recent_deliveries(limit=limit)


@router.post(
    "/test",
    response_model=WebhookTestOut,
    summary="테스트 이벤트 발행",
    description=(
        "수신자 연결을 확인하는 webhook.ping 이벤트를 발행합니다. "
        "실제 전송은 백그라운드에서 이뤄지므로 결과는 /api/webhooks/deliveries 에서 확인하세요."
    ),
    responses={409: {"description": "웹훅이 꺼져 있거나 이벤트 필터에 막힘"}},
)
def send_test_event():
    if not webhook_dispatcher.enabled:
        raise HTTPException(
            status_code=409,
            detail="웹훅이 비활성화 상태입니다. .env의 WEBHOOK_ENABLED와 WEBHOOK_URLS를 확인하세요.",
        )

    queued = webhook_dispatcher.emit(EVENT_PING, {"message": "smart-energy-backend 웹훅 테스트"})
    if not queued:
        raise HTTPException(
            status_code=409,
            detail=f"'{EVENT_PING}' 이벤트가 WEBHOOK_EVENTS 필터에 포함되어 있지 않거나 큐가 가득 찼습니다.",
        )
    return WebhookTestOut(queued=True, detail="테스트 이벤트를 큐에 넣었습니다. /api/webhooks/deliveries 에서 결과를 확인하세요.")
