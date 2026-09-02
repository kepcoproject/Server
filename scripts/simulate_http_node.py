"""
ESP32 노드(HTTP 방식) 시뮬레이터.

실물 하드웨어 없이 수집과 제어 폴링을 확인할 때 쓴다.
펌웨어 esp32_sensor_node.ino 와 같은 순서로 동작한다.

    1. 측정 주기마다  POST /api/sensors/data
    2. 폴링 주기마다  GET  /api/spaces/{SPACE_ID}/actuator/latest?actuator_type=light

MQTT 방식 노드는 simulate_sensor.py 를 쓴다.

사용 예:
    python scripts/simulate_http_node.py --space-id 1
    python scripts/simulate_http_node.py --space-id 1 --interval 3 --occupancy-cycle 20
    python scripts/simulate_http_node.py --once          # 한 번만 보내고 종료
"""
import argparse
import json
import random
import signal
import sys
import time
import urllib.error
import urllib.request

running = True


def _stop(signum, frame):
    global running
    running = False
    print("\n종료합니다.")


def post_json(url: str, body: dict, timeout: float = 5.0):
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(body).encode("utf-8")
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode("utf-8", "replace")[:200]}
    except Exception as exc:  # 네트워크 자체가 안 되는 경우
        return 0, {"error": str(exc)}


def get_json(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception as exc:
        return 0, {"error": str(exc)}


def main():
    parser = argparse.ArgumentParser(description="ESP32 HTTP 노드 시뮬레이터")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--node-key", default="node-classroom-01")
    parser.add_argument("--space-id", default="1", help="펌웨어의 SPACE_ID (정수 또는 sp-N)")
    parser.add_argument("--interval", type=float, default=5.0, help="측정 전송 주기(초)")
    parser.add_argument("--poll-interval", type=float, default=10.0, help="제어 폴링 주기(초)")
    parser.add_argument(
        "--occupancy-cycle",
        type=int,
        default=12,
        help="이 횟수마다 재실/공실이 번갈아 바뀐다",
    )
    parser.add_argument("--once", action="store_true", help="한 번만 전송하고 종료")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _stop)

    data_url = f"{args.base_url}/api/sensors/data"
    control_url = (
        f"{args.base_url}/api/spaces/{args.space_id}/actuator/latest?actuator_type=light"
    )

    print(f"노드 {args.node_key} -> {args.base_url} (space_id={args.space_id})")
    print("Ctrl+C 로 종료합니다.\n")

    relay_on = True
    tick = 0
    last_poll = 0.0

    while running:
        occupied = (tick // max(1, args.occupancy_cycle)) % 2 == 0

        # 재실이면 조명·기기가 켜져 있어 전류가 크고, 공실이면 대기전력만 남는다.
        if occupied:
            amp = round(random.uniform(1.4, 2.2), 2)
            lux = round(random.uniform(400, 800), 1)
        else:
            # 공실인데 전류가 남아 있으면 낭비로 잡힌다 (시연에서 보여줄 상황)
            amp = round(random.uniform(0.4, 0.9), 2)
            lux = round(random.uniform(20, 120), 1)

        status, body = post_json(
            data_url,
            {
                "node_key": args.node_key,
                "space_id": args.space_id,
                "occupancy": occupied,
                "light_lux": lux,
                "current_amp": amp,
                "source": "sim",
            },
        )
        if status == 200:
            print(
                f"[전송] {status} occupancy={occupied} amp={amp:.2f} "
                f"-> {body.get('powerW')}W (space={body.get('spaceId')})"
            )
        else:
            print(f"[전송 실패] status={status} {body.get('error', '')}")

        if args.once:
            break

        now = time.monotonic()
        if now - last_poll >= args.poll_interval:
            last_poll = now
            code, command = get_json(control_url)
            if code == 200 and command.get("action"):
                want_on = command["action"] == "on"
                if want_on != relay_on:
                    relay_on = want_on
                    print(
                        f"[제어 동기화] 릴레이 -> {'ON' if relay_on else 'OFF'} "
                        f"(source={command.get('source')})"
                    )

        # 종료 신호에 빨리 반응하도록 잘게 나눠 기다린다
        waited = 0.0
        while running and waited < args.interval:
            time.sleep(min(0.2, args.interval - waited))
            waited += 0.2
        tick += 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
