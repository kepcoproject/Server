"""
계획서 4.2 '1단계: 재실 확률 테이블 기반 임계값 절전 추천'.

smart-energy-system/backend/app/analysis.py 를 현재 스키마에 맞춰 옮긴 것이다.
바뀐 전제 두 가지:

- 공간 식별이 Space.id(정수)가 아니라 building/floor/room_id(문자열) 조합이다.
- 측정 시각이 datetime이 아니라 device_timestamp(UNIX epoch 초)다.

구성:
- update_occupancy_probability(): 데이터가 들어올 때마다 (공간, 요일, 시간대) 셀의
  재실 확률을 갱신한다. 표본이 적을 때는 누적평균, 충분히 쌓이면 이동평균으로 전환해
  방학·시험 기간처럼 패턴이 바뀌어도 따라간다.
- recommend(): 지금 비어 있고 원래도 잘 안 쓰는 시간대면 '절전 제안'.
  평소 자주 쓰는 시간대라면 잠깐 자리를 비운 것으로 보고 '정상' (오탐 방지).
- compute_savings(): 5.4절 before/after 비교로 절감률(%)을 낸다.

2단계(로지스틱 회귀 등 경량 ML) 고도화는 update_occupancy_probability를 대체·확장하는
방식으로 붙이면 된다 — 인터페이스(공간·요일·시간 -> 확률)는 그대로 유지한다.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .config import get_settings
from .models import OccupancyProbability, SensorReading

logger = logging.getLogger("smart_energy.analytics")

settings = get_settings()

RECOMMEND_SAVE = "절전 제안"
RECOMMEND_NORMAL = "정상"
RECOMMEND_WARMING_UP = "데이터 축적 중"

# 판독값 간격이 이보다 크면 노드 재부팅·통신 두절로 보고 적산에서 제외한다.
MAX_GAP_HOURS = 2.0


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _local(value: datetime) -> datetime:
    """
    요일·시간대를 나눌 때 쓰는 현지 시각.

    저장은 UTC로 하지만 "이 공간은 화요일 오후에 비어 있다" 같은 패턴은 사람의
    생활 시간대를 따른다. UTC로 묶으면 한국 기준 09시가 UTC 00시가 되어,
    화면에 뜨는 시간대가 실제와 9시간 어긋난다.
    """
    return _to_naive_utc(value) + timedelta(hours=settings.analytics_utc_offset_hours)


def _to_epoch(value: datetime) -> int:
    return int(_to_naive_utc(value).replace(tzinfo=timezone.utc).timestamp())


def update_occupancy_probability(
    db: Session,
    building: str,
    floor: str,
    room_id: str,
    occupancy: bool,
    ts: datetime,
) -> OccupancyProbability:
    """(공간, 요일, 시간대) 셀의 재실 확률을 새 관측치로 갱신한다. commit은 호출자 몫."""
    local = _local(ts)
    weekday = local.weekday()  # 0=월 ... 6=일 (현지 기준)
    hour = local.hour

    cell = (
        db.query(OccupancyProbability)
        .filter_by(
            building=building, floor=floor, room_id=room_id, weekday=weekday, hour=hour
        )
        .first()
    )
    observed = 1.0 if occupancy else 0.0

    if cell is None:
        cell = OccupancyProbability(
            building=building,
            floor=floor,
            room_id=room_id,
            weekday=weekday,
            hour=hour,
            probability=observed,
            sample_count=1,
            updated_at=_to_naive_utc(ts),
        )
        db.add(cell)
        # 세션이 autoflush=False라 flush하지 않으면 같은 트랜잭션의 다음 조회에서
        # 이 셀이 안 보여 중복 INSERT를 시도하게 된다.
        db.flush()
    else:
        if cell.sample_count < settings.analytics_ema_min_samples:
            # 표본이 적을 때는 단순 누적평균
            cell.probability = (cell.probability * cell.sample_count + observed) / (
                cell.sample_count + 1
            )
        else:
            # 충분히 쌓이면 이동평균으로 전환 (최근 패턴 변화에 반응)
            alpha = settings.analytics_ema_alpha
            cell.probability = (1 - alpha) * cell.probability + alpha * observed
        cell.sample_count += 1
        cell.updated_at = _to_naive_utc(ts)

    return cell


def get_cell(
    db: Session, building: str, floor: str, room_id: str, ts: datetime
) -> Optional[OccupancyProbability]:
    """해당 시각이 속한 (요일, 시간대) 셀을 돌려준다. 아직 관측이 없으면 None."""
    local = _local(ts)
    return (
        db.query(OccupancyProbability)
        .filter_by(
            building=building,
            floor=floor,
            room_id=room_id,
            weekday=local.weekday(),
            hour=local.hour,
        )
        .first()
    )


def get_probability_now(
    db: Session, building: str, floor: str, room_id: str, ts: datetime
) -> Optional[float]:
    cell = get_cell(db, building, floor, room_id, ts)
    return cell.probability if cell else None


def recommend(current_occupancy: Optional[bool], probability_now: Optional[float]) -> str:
    """
    현재 재실 상태 + 해당 시간대 재실 확률을 함께 보고 절전 추천 여부를 판단한다.

    - 지금 비어 있고 이 시간대는 원래도 잘 안 쓴다 -> '절전 제안'
    - 지금 비어 있지만 원래 자주 쓰는 시간대다(잠깐 자리 비움) -> '정상'
    """
    if probability_now is None:
        return RECOMMEND_WARMING_UP
    if current_occupancy is False and probability_now < settings.analytics_idle_threshold:
        return RECOMMEND_SAVE
    return RECOMMEND_NORMAL


# 낭비 유형
WASTE_UNOCCUPIED = "unoccupied"
WASTE_DAYLIGHT = "daylight"


def detect_waste(
    occupancy: Optional[bool], power: Optional[float], lux: Optional[float]
) -> Optional[dict]:
    """
    재실·전력·조도 세 센서를 함께 보고 낭비를 판정한다 (센서 융합).

    센서 하나만으로는 못 잡는 것이 있다.
    - PIR만 보면: 사람이 있으면 무조건 정상으로 판정한다.
      낮에 창가 자리에서 조명을 켜두는 낭비를 놓친다.
    - 전력만 보면: 필요해서 쓰는 것과 낭비를 구분할 수 없다.

    돌려주는 값은 유형과 사람이 읽을 메시지다. 낭비가 아니면 None.
    """
    if power is None or power < settings.webhook_power_threshold:
        return None

    # 아무도 없는데 전력을 쓰는 경우가 가장 명백하다.
    if occupancy is False:
        return {
            "type": WASTE_UNOCCUPIED,
            "message": f"공실인데 전력 {power:.0f}W가 소모되고 있습니다",
            "power": power,
            "lux": lux,
        }

    # 사람이 있어도, 자연광이 충분한데 조명을 켜고 있으면 낭비다.
    if lux is not None and lux >= settings.analytics_daylight_lux:
        return {
            "type": WASTE_DAYLIGHT,
            "message": (
                f"자연광이 충분한데({lux:.0f}lux) 전력 {power:.0f}W를 쓰고 있습니다. "
                "조명을 줄일 수 있습니다"
            ),
            "power": power,
            "lux": lux,
        }

    return None


def compute_savings(
    db: Session,
    building: str,
    floor: str,
    room_id: str,
    start: datetime,
    end: datetime,
    baseline_power_w: Optional[float] = None,
) -> Optional[dict]:
    """
    5.4절 방법론: 실측 전력 적산량(actual)을 '상시 켜짐 가정' 기준선(baseline)과
    비교해 절감률(%)을 계산한다. 구간에 데이터가 없으면 None.
    """
    if baseline_power_w is None:
        baseline_power_w = settings.analytics_baseline_power_w

    start_ts, end_ts = _to_epoch(start), _to_epoch(end)
    if end_ts <= start_ts:
        return None

    readings = (
        db.query(SensorReading)
        .filter(
            SensorReading.building == building,
            SensorReading.floor == floor,
            SensorReading.room_id == room_id,
            SensorReading.device_timestamp >= start_ts,
            SensorReading.device_timestamp <= end_ts,
        )
        .order_by(SensorReading.device_timestamp)
        .all()
    )
    if not readings:
        return None

    period_hours = (end_ts - start_ts) / 3600.0

    # 판독값 사이 평균 전력 x 구간 시간으로 적산 (사다리꼴 근사)
    actual_wh = 0.0
    covered_hours = 0.0
    for i in range(1, len(readings)):
        prev, cur = readings[i - 1], readings[i]
        dt_h = (cur.device_timestamp - prev.device_timestamp) / 3600.0
        if dt_h <= 0 or dt_h > MAX_GAP_HOURS:
            continue
        avg_w = ((prev.power or 0.0) + (cur.power or 0.0)) / 2
        actual_wh += avg_w * dt_h
        covered_hours += dt_h

    if covered_hours <= 0:
        return None

    # 기준선은 '요청한 기간' 전체가 아니라 '실제로 측정된 시간'으로 잡는다.
    # 전체 기간으로 잡으면 데이터가 듬성한 구간에서 절감률이 부풀려진다.
    # (예: 30일을 요청했는데 하루치만 있으면 29일을 통째로 절감한 것처럼 나온다)
    baseline_kwh = (baseline_power_w * covered_hours) / 1000.0
    actual_kwh = actual_wh / 1000.0
    saved_kwh = max(baseline_kwh - actual_kwh, 0.0)
    saved_pct = (saved_kwh / baseline_kwh * 100) if baseline_kwh > 0 else 0.0

    return {
        "period_hours": round(period_hours, 2),
        "covered_hours": round(covered_hours, 2),
        "baseline_power_w": baseline_power_w,
        "baseline_kwh": round(baseline_kwh, 3),
        "actual_kwh": round(actual_kwh, 3),
        "saved_kwh": round(saved_kwh, 3),
        "saved_pct": round(saved_pct, 1),
        "sample_count": len(readings),
    }
