import argparse
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, init_db
from app.models import Device, DeviceStatusEnum, SensorReading
from app.status_cache import status_cache

ROOMS = [
    ("bldg-a", "f2", "room-101", "PY-NODE-101", "online", "occupied"),
    ("bldg-a", "f2", "room-102", "PY-NODE-102", "online", "waste"),
    ("bldg-a", "f3", "room-201", "PY-NODE-201", "online", "empty"),
    ("bldg-b", "f1", "room-301", "PY-NODE-301", "offline", "empty"),
    ("bldg-b", "f1", "room-302", "PY-NODE-302", "unknown", None),
]

CURRENT_STATE_MINUTES = 30

WASTE_ROOMS = {"room-102", "room-201"}


def occupancy_for(hour: int, room_id: str) -> bool:
    if room_id == "room-301":
        return False
    if 9 <= hour < 17:
        return random.random() < 0.85
    if 17 <= hour < 20:
        return random.random() < 0.3
    return random.random() < 0.05


def power_for(occupied: bool, hour: int, room_id: str) -> float:
    if occupied:
        base = 180 + 40 * math.sin(hour / 3.0)
        return round(max(0.0, base + random.uniform(-15, 15)), 1)
    if room_id in WASTE_ROOMS and 17 <= hour < 22:
        return round(random.uniform(90, 140), 1)
    return round(random.uniform(0.2, 3.0), 1)


def temp_for(hour: int, occupied: bool) -> float:
    base = 22.5 + 2.5 * math.sin((hour - 6) / 24 * 2 * math.pi)
    return round(base + (1.2 if occupied else 0.0) + random.uniform(-0.4, 0.4), 1)


def seed(hours: int, interval_minutes: int, reset: bool) -> None:
    init_db()
    db = SessionLocal()
    try:
        if reset:
            deleted_readings = db.query(SensorReading).delete()
            deleted_devices = db.query(Device).delete()
            db.commit()
            print(f"기존 데이터 삭제: 측정값 {deleted_readings}건, 노드 {deleted_devices}개")

        now = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
        start = now - timedelta(hours=hours)
        steps = int(hours * 60 / interval_minutes)
        total_readings = 0

        for building, floor, room_id, device_id, status, current_state in ROOMS:
            device = db.get(Device, device_id)
            if device is None:
                device = Device(device_id=device_id, building=building, floor=floor, room_id=room_id)
                db.add(device)
            device.building = building
            device.floor = floor
            device.room_id = room_id
            device.status = DeviceStatusEnum(status)
            device.last_status_change = now - timedelta(minutes=random.randint(5, 240))

            if status == "unknown":
                device.last_seen = None
                db.commit()
                status_cache.set_status(device_id, status, device.last_status_change)
                print(f"  {device_id:14} {building}/{floor}/{room_id:9} status={status:8} (측정값 없음)")
                continue

            last_moment = now if status == "online" else now - timedelta(hours=3)
            recent_from = last_moment - timedelta(minutes=CURRENT_STATE_MINUTES)
            # range(steps) 면 마지막 측정값이 now 에 닿지 못하고 한 간격 전에서 멈춘다.
            # 기본 간격 5분 > 오프라인 기준 180초 라서, online 으로 넣은 노드가
            # 시드 직후부터 화면에 오프라인으로 표시됐다. 끝점을 포함시킨다.
            for i in range(steps + 1):
                moment = start + timedelta(minutes=i * interval_minutes)
                if moment > last_moment:
                    break
                occupied = occupancy_for(moment.hour, room_id)
                power = power_for(occupied, moment.hour, room_id)
                if moment >= recent_from:
                    if current_state == "occupied":
                        occupied = True
                        power = round(random.uniform(180, 220), 1)
                    elif current_state == "waste":
                        occupied = False
                        power = round(random.uniform(95, 135), 1)
                    elif current_state == "empty":
                        occupied = False
                        power = round(random.uniform(0.2, 3.0), 1)
                db.add(
                    SensorReading(
                        device_id=device_id,
                        building=building,
                        floor=floor,
                        room_id=room_id,
                        occupancy=occupied,
                        power=power,
                        temp=temp_for(moment.hour, occupied),
                        device_timestamp=int(moment.replace(tzinfo=timezone.utc).timestamp()),
                        received_at=moment,
                    )
                )
                total_readings += 1
                device.last_seen = moment

            db.commit()
            status_cache.set_status(device_id, status, device.last_status_change)
            labels = {"occupied": "사용 중", "waste": "낭비 경고", "empty": "비어 있음"}
            print(f"  {device_id:14} {building}/{floor}/{room_id:9} status={status:8} 현재={labels.get(current_state, '-'):8} 마지막수신={device.last_seen}")

        print(f"\n완료: 노드 {len(ROOMS)}개, 측정값 {total_readings}건 ({hours}시간 / {interval_minutes}분 간격)")
        print("확인: http://localhost:8000/api/devices , http://localhost:8000/api/data/latest")
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="프론트엔드 개발용 더미 데이터 생성기")
    parser.add_argument("--hours", type=int, default=24, help="몇 시간 분량을 만들지 (기본 24)")
    parser.add_argument("--interval", type=int, default=5, help="측정 간격(분, 기본 5)")
    parser.add_argument("--reset", action="store_true", help="기존 데이터를 지우고 새로 생성")
    parser.add_argument("--seed", type=int, default=42, help="난수 시드 (같은 값이면 같은 데이터)")
    args = parser.parse_args()

    random.seed(args.seed)
    seed(args.hours, args.interval, args.reset)


if __name__ == "__main__":
    main()
