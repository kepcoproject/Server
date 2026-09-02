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
