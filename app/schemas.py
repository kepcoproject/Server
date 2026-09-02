from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_serializer


def to_utc_z(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat() + "Z"


class SensorMetrics(BaseModel):
    occupancy: Optional[bool] = None
    power: Optional[float] = None
    temp: Optional[float] = None


class SensorDataPayload(BaseModel):
    device_id: str
    timestamp: int
    metrics: SensorMetrics


class StatusPayload(BaseModel):
    status: Literal["online", "offline"]


class DeviceOut(BaseModel):
    device_id: str = Field(
        description="센서 노드 고유 ID. 노드가 data 메시지에서 스스로 보고한 값",
        examples=["PY-NODE-101"],
    )
    building: str = Field(description="건물 식별자", examples=["bldg-a"])
    floor: str = Field(description="층 식별자", examples=["f2"])
    room_id: str = Field(description="공간(교실) 식별자", examples=["room-101"])
    status: str = Field(
        description=(
            "노드 생존 상태. online=정상 연결됨, offline=연결 끊김(LWT로 감지), "
            "unknown=아직 상태 메시지를 한 번도 받지 못함"
        ),
        examples=["online"],
    )
    last_seen: Optional[datetime] = Field(
        default=None,
        description="마지막 센서 데이터 수신 시각. UTC이며 끝에 Z가 붙는다",
        examples=["2026-08-20T08:57:07.480989Z"],
    )
    last_status_change: Optional[datetime] = Field(
        default=None,
        description="마지막으로 online/offline이 바뀐 시각. UTC(Z)",
        examples=["2026-08-20T08:57:07.486613Z"],
    )

    model_config = {"from_attributes": True}

    @field_serializer("last_seen", "last_status_change")
    def _serialize_datetimes(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_z(value)


class SensorReadingOut(BaseModel):
    device_id: str = Field(examples=["PY-NODE-101"])
    building: str = Field(examples=["bldg-a"])
    floor: str = Field(examples=["f2"])
    room_id: str = Field(examples=["room-101"])
    occupancy: Optional[bool] = Field(
        default=None,
        description="재실 여부. true=사람 있음, false=비어 있음, null=센서가 값을 보내지 않음",
        examples=[False],
    )
    power: Optional[float] = Field(
        default=None, description="순간 소비 전력 (W)", examples=[120.5]
    )
    temp: Optional[float] = Field(default=None, description="온도 (섭씨)", examples=[24.5])
    device_timestamp: int = Field(
        description="센서가 측정한 시각. UNIX epoch 초 단위 정수 (JS에서는 *1000 후 new Date)",
        examples=[1787125800],
    )
    received_at: datetime = Field(
        description="서버가 수신해 저장한 시각. UTC(Z)",
        examples=["2026-08-20T08:57:07.480989Z"],
    )

    model_config = {"from_attributes": True}

    @field_serializer("received_at")
    def _serialize_received_at(self, value: datetime) -> Optional[str]:
        return to_utc_z(value)


class RecommendationOut(BaseModel):
    building: str = Field(examples=["bldg-a"])
    floor: str = Field(examples=["f2"])
    room_id: str = Field(examples=["room-101"])
    occupancy: Optional[bool] = Field(
        default=None, description="가장 최근 측정의 재실 여부", examples=[False]
    )
    power: Optional[float] = Field(
        default=None, description="가장 최근 측정 전력 (W)", examples=[120.5]
    )
    occupancy_probability: Optional[float] = Field(
        default=None,
        description="이 공간의 해당 요일·시간대 재실 확률(0~1). 표본이 없으면 null",
        examples=[0.08],
    )
    sample_count: int = Field(
        description="이 요일·시간대 셀에 누적된 관측 수", examples=[42]
    )
    recommendation: str = Field(
        description="'절전 제안' | '정상' | '데이터 축적 중'", examples=["절전 제안"]
    )
    evaluated_at: str = Field(description="판단 시각. UTC(Z)")


class SavingsOut(BaseModel):
    building: str = Field(examples=["bldg-a"])
    floor: str = Field(examples=["f2"])
    room_id: str = Field(examples=["room-101"])
    period_hours: float = Field(description="집계 구간 길이(시간)", examples=[24.0])
    baseline_power_w: float = Field(
        description="'상시 켜짐' 가정 기준선 전력 (W)", examples=[200.0]
    )
    baseline_kwh: float = Field(description="기준선 사용량 (kWh)", examples=[4.8])
    actual_kwh: float = Field(description="실측 적산 사용량 (kWh)", examples=[1.92])
    saved_kwh: float = Field(description="절감량 (kWh)", examples=[2.88])
    saved_pct: float = Field(description="절감률 (%)", examples=[60.0])
    sample_count: int = Field(description="집계에 쓰인 측정 건수", examples=[288])


class HealthOut(BaseModel):
    status: str = Field(description="서버 상태. 항상 ok", examples=["ok"])
    mqtt_enabled: bool = Field(
        description="MQTT 수신 기능이 켜져 있는지. false면 센서 데이터가 들어오지 않는 설정 상태",
        examples=[True],
    )
    mqtt_connected: bool = Field(
        description="현재 브로커에 연결되어 있는지", examples=[True]
    )


class WebhookConfigOut(BaseModel):
    enabled: bool
    urls: List[str] = Field(description="경로가 마스킹된 수신 URL 목록")
    events: str
    signature_enabled: bool
    timeout: float
    max_retries: int
    known_events: List[str] = Field(description="발행 가능한 전체 이벤트 이름")


class WebhookDeliveryOut(BaseModel):
    delivery_id: str
    event: str
    url: str
    ok: bool
    status_code: Optional[int] = None
    attempts: int
    error: Optional[str] = None
    sent_at: str


class WebhookTestOut(BaseModel):
    queued: bool
    detail: str


DeviceList = List[DeviceOut]
SensorReadingList = List[SensorReadingOut]
