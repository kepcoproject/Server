from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, desc, func
from sqlalchemy.orm import Session

from .. import analytics
from ..database import get_db
from ..models import Device, SensorReading
from ..schemas import DeviceOut, RecommendationOut, SavingsOut, SensorReadingOut
from ..status_cache import status_cache
from ..utils import utcnow

router = APIRouter(tags=["dashboard"])


def _device_out(device: Device) -> DeviceOut:
    """
    캐시된 최신 상태를 씌워 응답 모델을 만든다.

    예전에는 ORM 객체의 status를 직접 바꿨는데, 그건 세션이 autoflush=False라서
    우연히 DB에 새지 않았을 뿐이다. 조회 요청이 데이터를 건드리지 않도록 분리한다.
    """
    out = DeviceOut.model_validate(device)
    cached = status_cache.get_status(device.device_id)
    if cached:
        out.status = cached["status"]
    return out


def _to_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


@router.get(
    "/devices",
    response_model=List[DeviceOut],
    summary="전체 센서 노드 목록",
    description=(
        "등록된 모든 센서 노드와 각 노드의 현재 online/offline 상태를 반환합니다. "
        "대시보드 좌측 노드 목록이나 상태 요약 카드에 사용하세요.\n\n"
        "`building`, `floor` 쿼리로 특정 건물/층만 걸러낼 수 있습니다. "
        "데이터가 하나도 없으면 빈 배열 `[]`을 반환합니다(404가 아님)."
    ),
)
def list_devices(
    building: Optional[str] = Query(default=None, description="건물로 필터", examples=["bldg-a"]),
    floor: Optional[str] = Query(default=None, description="층으로 필터", examples=["f2"]),
    db: Session = Depends(get_db),
):
    query = db.query(Device)
    if building:
        query = query.filter(Device.building == building)
    if floor:
        query = query.filter(Device.floor == floor)
    return [_device_out(d) for d in query.all()]


@router.get(
    "/devices/{device_id}",
    response_model=DeviceOut,
    summary="단일 노드 상세",
    description="특정 노드 하나의 위치와 상태를 조회합니다. 없는 ID면 404를 반환합니다.",
    responses={404: {"description": "해당 device_id의 노드가 없음"}},
)
def get_device(device_id: str, db: Session = Depends(get_db)):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="디바이스를 찾을 수 없습니다.")
    return _device_out(device)


@router.get(
    "/rooms/{building}/{floor}/{room_id}/latest",
    response_model=SensorReadingOut,
    summary="특정 공간의 최신 센서값",
    description=(
        "한 공간의 가장 최근 측정값 1건을 반환합니다. 공간 상세 화면의 현재 상태 표시에 사용하세요.\n\n"
        "아직 해당 공간의 데이터가 한 건도 없으면 404를 반환하므로, "
        "프론트에서는 404를 오류가 아닌 '데이터 없음' 상태로 처리하는 것을 권장합니다."
    ),
    responses={404: {"description": "해당 공간의 측정 데이터가 아직 없음"}},
)
def get_latest_reading(building: str, floor: str, room_id: str, db: Session = Depends(get_db)):
    reading = (
        db.query(SensorReading)
        .filter_by(building=building, floor=floor, room_id=room_id)
        .order_by(desc(SensorReading.device_timestamp))
        .first()
    )
    if reading is None:
        raise HTTPException(status_code=404, detail="해당 공간의 데이터가 없습니다.")
    return reading


@router.get(
    "/rooms/{building}/{floor}/{room_id}/history",
    response_model=List[SensorReadingOut],
    summary="특정 공간의 시계열 이력",
    description=(
        "한 공간의 측정 이력을 최신순으로 반환합니다. 그래프(전력 추이, 재실 패턴)용입니다.\n\n"
        "정렬은 최신이 먼저이므로, 시간축 그래프를 그릴 때는 프론트에서 뒤집어 쓰세요.\n\n"
        "`since`는 UTC 기준이며 `2026-08-20T00:00:00Z` 형식을 권장합니다. "
        "`+09:00` 같은 오프셋을 붙여 보내도 서버가 UTC로 변환해 처리합니다."
    ),
)
def get_reading_history(
    building: str,
    floor: str,
    room_id: str,
    limit: int = Query(default=100, ge=1, le=1000, description="1~1000건", examples=[100]),
    since: Optional[datetime] = Query(
        default=None,
        description="이 시각 이후에 측정된 데이터만 조회. UTC 권장",
        examples=["2026-08-20T00:00:00Z"],
    ),
    db: Session = Depends(get_db),
):
    query = db.query(SensorReading).filter_by(building=building, floor=floor, room_id=room_id)
    normalized_since = _to_naive_utc(since)
    if normalized_since:
        # 정렬은 device_timestamp로 하면서 필터만 received_at으로 걸면 두 시계가 섞여
        # 결과가 어긋난다. 측정 시각 하나로 통일한다.
        since_epoch = int(normalized_since.replace(tzinfo=timezone.utc).timestamp())
        query = query.filter(SensorReading.device_timestamp >= since_epoch)
    return query.order_by(desc(SensorReading.device_timestamp)).limit(limit).all()


@router.get(
    "/data/latest",
    response_model=List[SensorReadingOut],
    summary="전체 공간의 최신값 모음",
    description=(
        "모든 공간의 최신 측정값을 1건씩 모아서 반환합니다. "
        "대시보드 메인 화면을 한 번의 요청으로 채울 수 있어, 공간마다 latest를 따로 부르지 않아도 됩니다.\n\n"
        "실시간처럼 보이게 하려면 5~10초 간격 폴링을 권장합니다."
    ),
)
def get_latest_per_room(db: Session = Depends(get_db)):
    # 공간별 최신 device_timestamp를 한 번에 구한 뒤 조인한다.
    # (예전에는 device를 순회하며 공간 조건으로 조회해서, 한 공간에 노드가 둘이면
    #  같은 측정값이 중복으로 담기고 쿼리도 노드 수만큼 나갔다)
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
            and_(
                SensorReading.building == latest.c.building,
                SensorReading.floor == latest.c.floor,
                SensorReading.room_id == latest.c.room_id,
                SensorReading.device_timestamp == latest.c.device_timestamp,
            ),
        )
        .all()
    )

    # 동일 공간에서 같은 timestamp로 두 건이 들어온 경우까지 막아 공간당 1건만 남긴다.
    seen = set()
    results: List[SensorReading] = []
    for row in rows:
        key = (row.building, row.floor, row.room_id)
        if key not in seen:
            seen.add(key)
            results.append(row)
    return results


@router.get(
    "/rooms/{building}/{floor}/{room_id}/recommendation",
    response_model=RecommendationOut,
    summary="절전 추천 판단",
    description=(
        "해당 공간의 최신 측정값과 '이 요일·시간대에 사람이 있을 확률'을 함께 보고 "
        "절전을 제안할지 판단합니다.\n\n"
        "- `절전 제안` — 지금 비어 있고, 원래도 잘 쓰지 않는 시간대\n"
        "- `정상` — 비어 있어도 평소 자주 쓰는 시간대라 잠깐 자리를 비운 것으로 봄\n"
        "- `데이터 축적 중` — 이 시간대 관측이 아직 없음\n\n"
        "확률 테이블은 센서 데이터가 들어올 때마다 갱신되며, 표본이 쌓이면 이동평균으로 "
        "전환해 방학·시험 기간 같은 패턴 변화도 따라갑니다."
    ),
    responses={404: {"description": "해당 공간의 측정 데이터가 아직 없음"}},
)
def get_recommendation(building: str, floor: str, room_id: str, db: Session = Depends(get_db)):
    reading = (
        db.query(SensorReading)
        .filter_by(building=building, floor=floor, room_id=room_id)
        .order_by(desc(SensorReading.device_timestamp))
        .first()
    )
    if reading is None:
        raise HTTPException(status_code=404, detail="해당 공간의 데이터가 없습니다.")

    now = utcnow()
    cell = analytics.get_cell(db, building, floor, room_id, now)
    probability = cell.probability if cell else None

    return RecommendationOut(
        building=building,
        floor=floor,
        room_id=room_id,
        occupancy=reading.occupancy,
        power=reading.power,
        occupancy_probability=round(probability, 3) if probability is not None else None,
        sample_count=cell.sample_count if cell else 0,
        recommendation=analytics.recommend(reading.occupancy, probability),
        evaluated_at=now.isoformat() + "Z",
    )


@router.get(
    "/rooms/{building}/{floor}/{room_id}/savings",
    response_model=SavingsOut,
    summary="절감률 산출",
    description=(
        "'상시 켜짐' 가정 기준선과 실측 적산 사용량을 비교해 절감률(%)을 냅니다. "
        "발표용 성과 지표 카드에 그대로 쓸 수 있습니다.\n\n"
        "기준선 전력은 .env의 ANALYTICS_BASELINE_POWER_W(기본 200W)를 씁니다. "
        "노드 재부팅 등으로 2시간 넘게 끊긴 구간은 적산에서 제외합니다."
    ),
    responses={404: {"description": "해당 구간에 측정 데이터가 없음"}},
)
def get_savings(
    building: str,
    floor: str,
    room_id: str,
    hours: int = Query(default=24, ge=1, le=720, description="최근 N시간", examples=[24]),
    db: Session = Depends(get_db),
):
    end = utcnow()
    result = analytics.compute_savings(
        db, building, floor, room_id, end - timedelta(hours=hours), end
    )
    if result is None:
        raise HTTPException(status_code=404, detail="해당 구간에 측정 데이터가 없습니다.")
    return SavingsOut(building=building, floor=floor, room_id=room_id, **result)
