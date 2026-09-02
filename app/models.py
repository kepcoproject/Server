import enum

from .utils import utcnow

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .database import Base


class DeviceStatusEnum(str, enum.Enum):
    online = "online"
    offline = "offline"
    unknown = "unknown"


class Device(Base):
    __tablename__ = "devices"

    device_id = Column(String, primary_key=True, index=True)
    building = Column(String, nullable=False, index=True)
    floor = Column(String, nullable=False, index=True)
    room_id = Column(String, nullable=False, index=True)

    status = Column(SAEnum(DeviceStatusEnum), default=DeviceStatusEnum.unknown, nullable=False)
    last_seen = Column(DateTime, nullable=True)
    last_status_change = Column(DateTime, nullable=True)

    readings = relationship("SensorReading", back_populates="device", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_device_location", "building", "floor", "room_id"),)


class SensorReading(Base):
    __tablename__ = "sensor_readings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(String, ForeignKey("devices.device_id"), nullable=False, index=True)

    building = Column(String, nullable=False, index=True)
    floor = Column(String, nullable=False, index=True)
    room_id = Column(String, nullable=False, index=True)

    occupancy = Column(Boolean, nullable=True)
    power = Column(Float, nullable=True)
    temp = Column(Float, nullable=True)
    # ESP32 노드가 조도 센서(LDR) 값을 함께 보낸다. MQTT 경로에서는 비어 있다.
    lux = Column(Float, nullable=True)

    device_timestamp = Column(Integer, nullable=False)
    received_at = Column(DateTime, default=utcnow, nullable=False)

    device = relationship("Device", back_populates="readings")

    __table_args__ = (
        Index("ix_reading_location_time", "building", "floor", "room_id", "device_timestamp"),
    )


class OccupancyProbability(Base):
    """
    (공간, 요일, 시간대)별 재실 확률. 계획서 4.2의 확률 테이블을 담는다.
    셀 하나 = 특정 공간의 특정 요일 특정 시(hour)에 사람이 있을 확률.
    """

    __tablename__ = "occupancy_probabilities"

    id = Column(Integer, primary_key=True, autoincrement=True)

    building = Column(String, nullable=False)
    floor = Column(String, nullable=False)
    room_id = Column(String, nullable=False)

    weekday = Column(Integer, nullable=False)  # 0=월 ... 6=일
    hour = Column(Integer, nullable=False)  # 0~23

    probability = Column(Float, nullable=False, default=0.0)
    sample_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "building", "floor", "room_id", "weekday", "hour", name="uq_occupancy_cell"
        ),
        Index("ix_occupancy_cell", "building", "floor", "room_id", "weekday", "hour"),
    )


# ---------------------------------------------------------------------------
# 아래는 프론트엔드(kepcoproject/Client) 연동을 위한 모델이다.
#
# 프론트는 공간을 spaceId 하나로 다루고, 공간·디바이스·사용자·제어 이력을
# 관리하는 화면을 갖고 있다. 센서 수집 쪽(Device, SensorReading)은 그대로 두고,
# 화면이 필요로 하는 정보를 여기에 담는다.
# ---------------------------------------------------------------------------


class Space(Base):
    """
    공간. 센서 토픽의 building/floor/room_id 조합에 사람이 읽는 이름을 붙인 것.
    측정값이 들어오면 자동으로 만들어지고, 화면에서 이름과 정격 전력을 채울 수 있다.
    """

    __tablename__ = "spaces"

    space_id = Column(String, primary_key=True, index=True)

    code = Column(String, nullable=False)  # A301 처럼 짧은 표시용 코드
    name = Column(String, nullable=False)  # "3층 회의실"
    building_label = Column(String, nullable=False)  # 화면에 보이는 건물명 "A동"
    floor_number = Column(Integer, nullable=False, default=0)
    rated_power_w = Column(Float, nullable=False, default=0.0)
    status = Column(String, nullable=False, default="정상")

    # 센서 토픽과의 연결고리
    building = Column(String, nullable=False, index=True)
    floor = Column(String, nullable=False, index=True)
    room_id = Column(String, nullable=False, index=True)

    created_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("building", "floor", "room_id", name="uq_space_location"),
    )


class AppUser(Base):
    """대시보드 사용자. 승인 대기(PENDING) 상태를 관리자가 승인하는 흐름이 있다."""

    __tablename__ = "app_users"

    user_id = Column(String, primary_key=True, index=True)
    login_id = Column(String, nullable=False, unique=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, default="")
    role = Column(String, nullable=False, default="MEMBER")  # ADMIN | MEMBER
    status = Column(String, nullable=False, default="PENDING")  # ACTIVE | PENDING
    password_hash = Column(String, nullable=False, default="")
    requested_at = Column(DateTime, default=utcnow, nullable=False)


class ControlCommand(Base):
    """
    제어 명령. 접수(PENDING) 후 노드가 가져가 실행하면 완료(COMPLETED)로 바뀐다.
    화면은 202 응답을 받고 commandId로 상태를 폴링한다.
    """

    __tablename__ = "control_commands"

    command_id = Column(String, primary_key=True, index=True)
    space_id = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False)  # LIGHT | VENT
    value = Column(String, nullable=False)  # ON | OFF | OPEN | CLOSE
    trigger = Column(String, nullable=False, default="MANUAL")  # MANUAL | AUTO
    status = Column(String, nullable=False, default="PENDING")
    override_minutes = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    delivered_at = Column(DateTime, nullable=True)  # 노드가 가져간 시각
    completed_at = Column(DateTime, nullable=True)


class ControlLog(Base):
    """제어 이력. 완료·실패한 명령이 여기에 쌓인다."""

    __tablename__ = "control_logs"

    log_id = Column(Integer, primary_key=True, autoincrement=True)
    space_id = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False)
    value = Column(String, nullable=False)
    trigger = Column(String, nullable=False, default="MANUAL")
    result = Column(String, nullable=False)  # COMPLETED | FAILED
    timestamp = Column(DateTime, default=utcnow, nullable=False, index=True)


class RecommendationState(Base):
    """
    절전 추천의 적용·반려 상태.
    추천 자체는 재실 확률 테이블에서 매번 새로 계산하므로, 사람이 내린 판단만 저장한다.
    """

    __tablename__ = "recommendation_states"

    recommendation_id = Column(String, primary_key=True, index=True)
    status = Column(String, nullable=False, default="PENDING")  # PENDING | APPLIED | REJECTED
    schedule_id = Column(String, nullable=True)
    comment = Column(String, nullable=True)
    updated_at = Column(DateTime, default=utcnow, nullable=False)


class Notification(Base):
    """화면 헤더의 알림 벨에 표시되는 항목. 낭비 감지·노드 오프라인 등이 쌓인다."""

    __tablename__ = "notifications"

    notification_id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String, nullable=False, default="INFO")  # INFO | WARNING | CRITICAL
    message = Column(String, nullable=False)
    read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)


class DeviceMeta(Base):
    """
    노드의 화면 표시용 정보. 센서 수집에는 필요 없지만 디바이스 관리 화면이 요구한다.
    device_id 는 Device 테이블과 같은 값을 쓴다.
    """

    __tablename__ = "device_meta"

    device_id = Column(String, primary_key=True, index=True)
    space_id = Column(String, nullable=True, index=True)
    sensors = Column(String, nullable=False, default="")  # 콤마 구분: PIR,LUX,CURRENT
    actuators = Column(String, nullable=False, default="")  # 콤마 구분: LIGHT,VENT
    firmware = Column(String, nullable=False, default="v1.0.0")
    signal_strength = Column(Integer, nullable=False, default=0)
    api_key_hash = Column(String, nullable=False, default="")
