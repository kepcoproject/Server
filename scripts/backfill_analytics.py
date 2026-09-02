"""
이미 쌓인 측정값으로 재실 확률 테이블을 다시 계산한다.

분석 기능(app/analytics.py)은 데이터가 들어올 때마다 확률을 갱신하므로, 그 기능이
생기기 전에 수집된 데이터는 테이블에 반영되어 있지 않다. 이 스크립트는 sensor_readings를
시간순으로 훑으면서 갱신 로직을 그대로 다시 태운다.

사용법:
    python scripts/backfill_analytics.py            # 기존 테이블 위에 이어서 반영
    python scripts/backfill_analytics.py --reset    # 테이블을 비우고 처음부터
"""
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analytics  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.models import OccupancyProbability, SensorReading  # noqa: E402

BATCH = 500


def backfill(reset: bool) -> None:
    init_db()
    db = SessionLocal()
    try:
        if reset:
            removed = db.query(OccupancyProbability).delete()
            db.commit()
            print(f"기존 확률 셀 {removed}개 삭제")

        total = (
            db.query(SensorReading)
            .filter(SensorReading.occupancy.isnot(None))
            .count()
        )
        if total == 0:
            print("반영할 측정값이 없습니다 (occupancy가 기록된 데이터 없음).")
            return

        print(f"측정값 {total}건을 시간순으로 반영합니다...")
        done = 0
        query = (
            db.query(SensorReading)
            .filter(SensorReading.occupancy.isnot(None))
            .order_by(SensorReading.device_timestamp)
            .yield_per(BATCH)
        )
        for reading in query:
            measured_at = datetime.fromtimestamp(
                reading.device_timestamp, tz=timezone.utc
            ).replace(tzinfo=None)
            analytics.update_occupancy_probability(
                db,
                reading.building,
                reading.floor,
                reading.room_id,
                bool(reading.occupancy),
                measured_at,
            )
            done += 1
            if done % BATCH == 0:
                db.commit()
                print(f"  {done}/{total}")
        db.commit()

        cells = db.query(OccupancyProbability).count()
        idle = (
            db.query(OccupancyProbability)
            .filter(OccupancyProbability.probability < 0.2)
            .count()
        )
        print(f"완료: {done}건 반영, 확률 셀 {cells}개 생성 (유휴 시간대 후보 {idle}개)")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="재실 확률 테이블 백필")
    parser.add_argument("--reset", action="store_true", help="기존 확률 테이블을 비우고 시작")
    backfill(parser.parse_args().reset)
