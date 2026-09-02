import argparse
import json
import random
import signal
import sys
import time

import paho.mqtt.client as mqtt


def build_topics(prefix: str, building: str, floor: str, room: str):
    data_topic = f"{prefix}/{building}/{floor}/{room}/data"
    status_topic = f"{prefix}/{building}/{floor}/{room}/status"
    return data_topic, status_topic


def main():
    parser = argparse.ArgumentParser(description="센서 노드 시뮬레이터")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--prefix", default="v1")
    parser.add_argument("--building", default="bldg-a")
    parser.add_argument("--floor", default="f2")
    parser.add_argument("--room", default="room-101")
    parser.add_argument("--device-id", default="PY-NODE-101")
    parser.add_argument("--interval", type=float, default=5.0, help="발행 주기(초)")
    args = parser.parse_args()

    data_topic, status_topic = build_topics(args.prefix, args.building, args.floor, args.room)

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

    client.will_set(status_topic, payload=json.dumps({"status": "offline"}), qos=1, retain=True)

    def on_connect(client, userdata, connect_flags, reason_code, properties=None):
        print(f"[시뮬레이터] 브로커 연결됨 (reason_code={reason_code})")
        client.publish(status_topic, json.dumps({"status": "online"}), qos=1, retain=True)

    client.on_connect = on_connect
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()

    def graceful_exit(signum, frame):
        print("\n[시뮬레이터] 종료 중... offline 상태 발행")
        client.publish(status_topic, json.dumps({"status": "offline"}), qos=1, retain=True)
        time.sleep(0.5)
        client.loop_stop()
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, graceful_exit)
    signal.signal(signal.SIGTERM, graceful_exit)

    occupancy = False
    print(f"[시뮬레이터] {data_topic} 로 {args.interval}초마다 발행 시작 (Ctrl+C로 종료)")
    while True:
        occupancy = random.random() < 0.5
        payload = {
            "device_id": args.device_id,
            "timestamp": int(time.time()),
            "metrics": {
                "occupancy": occupancy,
                "power": round(random.uniform(2.0, 25.0) if occupancy else random.uniform(0.0, 2.0), 2),
                "temp": round(random.uniform(21.0, 27.0), 1),
            },
        }
        client.publish(data_topic, json.dumps(payload), qos=1)
        print(f"[시뮬레이터] 발행: {payload}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
