# 스마트 에너지 절약 시스템 - 백엔드

빛가람 AI·ICT 경진대회 출품작의 백엔드 서버입니다. MQTT로 수신한 센서 데이터를 저장하고,
LWT(Last Will and Testament) 기반으로 센서 노드의 온라인/오프라인 상태를 관리하며,
프론트엔드 대시보드용 REST API를 제공합니다.

실행 및 테스트를 모두 마쳤습니다 (`pytest` 56개 전 통과, 로컬 mosquitto 브로커로
데이터 발행 → 저장 → API 조회 → LWT offline 반영까지 end-to-end 확인).

## 1. 폴더 구조

```
smart-energy-backend/
├── app/
│   ├── main.py            # FastAPI 엔트리포인트 (lifespan에서 DB 초기화 + MQTT 기동)
│   ├── config.py          # 환경변수 기반 설정 (.env)
│   ├── database.py        # SQLAlchemy 엔진/세션 (SQLite ↔ PostgreSQL 전환 가능)
│   ├── models.py          # Device, SensorReading 테이블 정의
│   ├── schemas.py         # MQTT payload 검증 + API 응답 스키마 (Pydantic)
│   ├── mqtt_client.py     # paho-mqtt 클라이언트 (백그라운드 실행, 지수 백오프 재연결)
│   ├── mqtt_handlers.py   # 토픽 파싱, data/status 메시지 처리 로직
│   ├── status_cache.py    # 디바이스 상태용 스레드세이프 인메모리 캐시 (Redis로 교체 가능)
│   ├── webhooks.py        # 아웃바운드 웹훅 디스패처 (큐 + 워커 스레드 + 재시도 + HMAC 서명)
│   └── api/
│       ├── routes.py          # 대시보드용 REST API 엔드포인트
│       └── webhook_routes.py  # 웹훅 설정 조회 / 전송 이력 / 테스트 발행
├── scripts/
│   ├── simulate_sensor.py   # 하드웨어 없이 테스트할 수 있는 센서 노드 시뮬레이터
│   ├── seed_demo_data.py    # 프론트엔드 개발용 더미 데이터 생성기 (브로커 불필요)
│   └── webhook_receiver.py  # 웹훅 수신자 테스트 서버 (서명 검증 예제 포함)
├── start-backend.bat       # 더블클릭 한 번으로 설치+시드+서버 실행 (Windows)
├── tests/                 # pytest 단위/통합 테스트
├── requirements.txt
├── .env.example
└── README.md
```

## 2. 토픽 및 데이터 규격

| 구분 | 토픽 패턴 | Payload 예시 |
| --- | --- | --- |
| 센서 데이터 | `v1/{building}/{floor}/{room_id}/data` | `{"device_id":"PY-NODE-101","timestamp":1787125800,"metrics":{"occupancy":true,"power":12.5,"temp":24.5}}` |
| LWT 상태 | `v1/{building}/{floor}/{room_id}/status` | `{"status":"online"}` / `{"status":"offline"}` (retain) |

- `status` payload에는 `device_id`가 없으므로, 토픽 경로의 `building/floor/room_id`(공간 위치) 기준으로
  디바이스를 매칭합니다. 아직 `data`를 한 번도 보내지 않은 노드의 상태가 먼저 들어오면
  `{building}-{floor}-{room_id}` 형태의 placeholder device row를 만들어둡니다.
- 노드(ESP32) 쪽에서는 MQTT 연결 시 `will_set(status_topic, {"status":"offline"}, retain=True)`로
  LWT를 등록하고, 정상 연결 후 `{"status":"online"}`을 즉시 publish(retain)하는 방식을 권장합니다.
  (`scripts/simulate_sensor.py` 참고)

### 2.1 하드웨어 구성 (게이트웨이 없음)

```
[ESP32 노드] --Wi-Fi--> [공유기] --TCP/1883--> [MQTT 브로커] --구독--> [백엔드 서버]
```

ESP32가 공유기에 Wi-Fi로 직접 붙어 MQTT로 브로커에 발행합니다. **블루투스 게이트웨이나 중계
허브가 필요 없는 구조**입니다. 노드가 곧 MQTT 클라이언트이므로 중간 변환 장비가 없고, 노드를
추가할 때 서버 쪽 코드 변경도 필요 없습니다(토픽 규격만 지키면 자동으로 등록됨).

노드(ESP32) 펌웨어가 지켜야 할 것 4가지:

1. **NTP로 시각 동기화** — payload의 `timestamp`는 UNIX epoch 초입니다. NTP 동기화 전에 발행하면
   1970년이나 부팅 후 경과초가 들어갑니다. (서버는 비정상 timestamp를 감지하면 수신 시각으로
   대체하지만, `last_seen`이 부정확해지므로 노드에서 맞추는 것이 맞습니다)
2. **LWT 등록** — 연결 시 `will_set(status_topic, {"status":"offline"}, qos=1, retain=True)`.
   노드가 정전/고장으로 죽으면 브로커가 대신 offline을 발행해 줍니다.
3. **연결 직후 online 발행** — `{"status":"online"}`을 retain=True로 발행.
4. **자동 재연결** — Wi-Fi/브로커 끊김 시 지수 백오프 재연결.

참조 구현은 [scripts/simulate_sensor.py](scripts/simulate_sensor.py)입니다. 동일한 순서(LWT 등록 →
연결 → online 발행 → 주기 발행)로 작성하면 됩니다.

### 2.2 현장 배포 체크리스트

| 항목 | 확인 내용 |
| --- | --- |
| 브로커 주소 | ESP32는 `localhost`로 못 붙습니다. 브로커 PC의 **LAN IP**(예: `192.168.0.10`)를 펌웨어에 넣을 것 |
| 브로커 바인딩 | mosquitto가 `127.0.0.1`이 아닌 `0.0.0.0`에 바인딩되어 있는지 (`listener 1883 0.0.0.0`) |
| 방화벽 | 브로커 PC의 인바운드 **TCP 1883** 허용 (Windows 방화벽이 기본 차단) |
| AP isolation | 공유기의 단말 간 통신 차단이 켜져 있으면 ESP32가 PC에 못 붙습니다. 시연 실패의 흔한 원인이라 **행사장 Wi-Fi 대신 자체 공유기/핫스팟 준비 권장** |
| IP 고정 | 브로커 PC의 IP가 DHCP로 바뀌면 노드가 전부 끊깁니다. 고정 IP 설정 권장 |

## 3. 로컬 실행

```bash
cd smart-energy-backend
python3 -m venv .venv && source .venv/bin/activate   # 선택 사항
pip install -r requirements.txt

cp .env.example .env   # 필요시 MQTT_HOST 등 값 수정

python scripts/seed_demo_data.py --reset   # 더미 데이터 생성 (선택, 프론트 개발용)

uvicorn app.main:app --reload --port 8000
```

- `GET /health` → `{"status": "ok", "mqtt_enabled": true|false, "mqtt_connected": true|false}`
- Swagger 문서: `http://localhost:8000/docs`

### 로컬 MQTT 브로커가 없다면

브로커가 없으면 시작 시 아래 경고가 한 번 뜨고(이후에는 조용히 백그라운드 재시도),
API 서버 자체는 정상 동작한다. `/health`의 `mqtt_connected`가 `false`로 표시된다.

```
[WARNING] MQTT 브로커(localhost:1883)에 연결하지 못했습니다: [WinError 10061] ...
```

선택지는 세 가지다.

1. **브로커 없이 API/대시보드만 개발** — `.env`에 아래 값을 넣으면 연결 시도 자체를 하지 않아
   경고가 사라진다. (`/health`가 `mqtt_enabled: false`로 응답하므로 장애와 구분된다)

   ```bash
   MQTT_ENABLED=false
   ```

2. **Docker로 띄우기**

   ```bash
   docker run -it -p 1883:1883 eclipse-mosquitto
   ```

3. **Windows에 직접 설치** (Docker가 없을 때)

   ```bash
   winget install EclipseFoundation.Mosquitto
   ```

   설치 후 `mosquitto -v` 로 포그라운드 실행하면 로그를 보면서 테스트할 수 있다.

### 하드웨어 없이 데이터 흐름 테스트

```bash
python scripts/simulate_sensor.py --building bldg-a --floor f2 --room room-101 \
    --device-id PY-NODE-101 --host localhost --port 1883
```

### 3D 가상 건물 시연 (`/demo3d`)

서버 주소 뒤에 `/demo3d` 를 붙이면 3D 건물이 뜬다. 예: `http://service.gsmsv.site:33141/demo3d`

방마다 **가상 센서 노드**가 하나씩 붙어 실제 ESP32 와 같은 경로로 서버와 이야기한다.

| 가상 노드가 하는 일 | 경로 | 주기 |
|---|---|---|
| 측정값 보내기 (재실·전력·조도) | `POST /api/sensors/data` | 5초, 상태가 바뀌면 곧바로 |
| 제어 명령 가져가기 | `GET /api/spaces/{id}/actuator/latest` | 2초 |

그래서 **대시보드에서 조명을 끄면 3D 에서도 꺼지고**, 3D 에서 사람이 불을 켠 채 나가면
대시보드에 낭비가 뜬다. 창 두 개를 나란히 띄워 두고 시연하면 된다.

- 관리자 계정으로 로그인하면 서버에 등록된 공간으로 건물을 짓는다. 관리자가 아니면 측정값은
  보내지만 3D 화면에서 원격 제어는 못 한다.
- **서버 없이 시연하기** 를 누르면 모든 동작을 화면 안에서 흉내 낸다. 대회장 네트워크가 불안할 때 쓴다.
- **낮/밤** 을 바꾸면 창밖 조도가 12 → 560lux 로 바뀐다. 낮에는 사람이 있어도 불을 켜 둔 방이
  "자연광 낭비" 로 잡힌다 — 재실·전력·조도 센서를 함께 보는 판정을 눈으로 보여준다.
- 가상 노드는 디바이스 화면에 `SIM3D-sp-1` 처럼 뜬다. 시연이 끝나면 `DELETE /devices/{id}` 로 지울 수 있다.
- three.js 는 `app/static/demo3d/vendor/` 에 함께 넣어 두었다(MIT). CDN 이 막혀도 뜬다.

## 4. 주요 REST API

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/api/devices` | 전체 센서 노드 + 최신 온/오프라인 상태 (`building`, `floor` 쿼리 필터 지원) |
| GET | `/api/devices/{device_id}` | 단일 노드 상세 |
| GET | `/api/rooms/{building}/{floor}/{room_id}/latest` | 특정 공간의 최신 센서값 1건 |
| GET | `/api/rooms/{building}/{floor}/{room_id}/history?limit=100&since=...` | 특정 공간의 시계열 이력 (최대 1000건) |
| GET | `/api/data/latest` | 전체 공간의 최신값 1건씩 (대시보드 요약용) |
| GET | `/api/rooms/{building}/{floor}/{room_id}/recommendation` | 절전 추천 판단 (재실 확률 기반) |
| GET | `/api/rooms/{building}/{floor}/{room_id}/savings?hours=24` | 기준선 대비 절감률(%) 산출 |
| GET | `/health` | 서버 상태 + MQTT 활성화/연결 여부 |
| GET | `/api/webhooks` | 현재 웹훅 설정 상태 (시크릿 값은 노출하지 않음) |
| GET | `/api/webhooks/deliveries?limit=20` | 최근 웹훅 전송 이력 (성공/실패 원인 확인) |
| POST | `/api/webhooks/test` | 수신자 연결 확인용 `webhook.ping` 이벤트 발행 |


## 5. 웹훅 (아웃바운드 이벤트 알림)

대시보드가 주기적으로 polling 하지 않아도, **서버가 먼저** 외부 시스템에 알려줘야 하는
상황(노드가 죽었다, 빈 교실에 전기가 켜져 있다)을 위해 아웃바운드 웹훅을 제공한다.
알림 서버, Slack/Discord 릴레이, 학교 관제 시스템 등 HTTP를 받을 수 있는 곳이면 어디든 붙는다.

### 5.1 설정 (.env)

```bash
WEBHOOK_ENABLED=true
WEBHOOK_URLS=http://localhost:9000/webhook,https://hooks.example.com/energy   # 콤마로 여러 개
WEBHOOK_SECRET=아무거나-긴-랜덤문자열      # 설정 시 HMAC-SHA256 서명 헤더 추가
WEBHOOK_EVENTS=*                          # 또는 device.offline,alert.energy_waste
WEBHOOK_TIMEOUT=5
WEBHOOK_MAX_RETRIES=3
WEBHOOK_RETRY_BACKOFF=2
WEBHOOK_POWER_THRESHOLD=10.0              # alert.energy_waste 판단 임계값 (W)
WEBHOOK_ALERT_COOLDOWN=300                # 같은 공간 재알림 최소 간격 (초)
```

기본값은 **비활성화**라서, 값을 넣지 않으면 기존 동작에 아무 영향이 없다.

### 5.2 이벤트 종류

| 이벤트 | 발생 시점 | data 주요 필드 |
| --- | --- | --- |
| `device.online` | LWT 상태가 offline/unknown → online 으로 **바뀔 때** | `device_id`, `building`, `floor`, `room_id`, `previous_status`, `changed_at` |
| `device.offline` | LWT 상태가 online → offline 으로 **바뀔 때** | 위와 동일 |
| `alert.energy_waste` | 재실 `false` + `power >= WEBHOOK_POWER_THRESHOLD` | `device_id`, 위치, `occupancy`, `power`, `threshold`, `message` |
| `webhook.ping` | `POST /api/webhooks/test` 호출 시 | `message` |

- retain된 status 메시지는 브로커에 재연결할 때마다 다시 배달되므로, **실제로 상태가 바뀐
  경우에만** 발행한다(중복 알림 방지).
- `alert.energy_waste`는 같은 공간에 대해 `WEBHOOK_ALERT_COOLDOWN` 초 동안 재발행하지 않는다.
  (5초마다 데이터가 들어오는데 매번 쏘면 수신자가 스팸을 맞는다.)

### 5.3 요청 형식

```http
POST /webhook HTTP/1.1
Content-Type: application/json
X-Webhook-Event: alert.energy_waste
X-Webhook-Delivery: 3f2b1c...            # 전송 고유 ID (수신자 쪽 중복 처리 판별용)
X-Webhook-Timestamp: 1787125800
X-Webhook-Signature: sha256=9a1f...      # HMAC-SHA256("{timestamp}.{body}", WEBHOOK_SECRET)

{
  "id": "3f2b1c...",
  "event": "alert.energy_waste",
  "occurred_at": "2026-08-19T10:30:00.000000Z",
  "data": {
    "device_id": "PY-NODE-101",
    "building": "bldg-a", "floor": "f2", "room_id": "room-101",
    "occupancy": false, "power": 120.5, "threshold": 10.0,
    "message": "bldg-a/f2/room-101 공간이 비어 있는데 전력 120.5W가 소모되고 있습니다."
  }
}
```

- 수신자가 **2xx가 아닌 응답**을 주거나 타임아웃되면 `WEBHOOK_MAX_RETRIES` 만큼 지수 백오프로
  재시도한다(2초 → 4초 → 8초). 단 4xx(429 제외)는 "수신자가 요청 자체를 거부한 것"이라 즉시 포기한다.
- 전송은 전용 워커 스레드가 큐에서 꺼내 수행하므로, 수신자가 느리거나 죽어 있어도
  MQTT 수신 루프와 REST API 응답은 전혀 느려지지 않는다.

### 5.4 웹훅 수신자 테스트

```bash
python scripts/webhook_receiver.py --port 9000 --secret my-secret
```

```bash
curl -X POST http://localhost:8000/api/webhooks/test
```

전송 결과는 `GET /api/webhooks/deliveries` 에서 `status_code` / `error` 로 확인할 수 있다.

> **보안 주의** — Slack/Discord 웹훅 URL은 경로 자체가 비밀번호(아는 사람은 누구나 그 채널에
> 글을 쓸 수 있다)다. 그래서 `GET /api/webhooks`와 `/deliveries`는 URL의 경로/쿼리를
> `https://hooks.slack.com/***` 형태로 **마스킹**해서 반환하고, `WEBHOOK_SECRET`은 값 대신
> 설정 여부(`signature_enabled`)만 노출한다. 현재 REST API 전체에 인증이 없으므로, 외부에
> 공개 배포할 때는 `CORS_ORIGINS` 제한과 함께 리버스 프록시 단에서 접근 제어를 두는 것을 권장한다.
수신 측 서명 검증 코드는 `scripts/webhook_receiver.py`의 `verify_signature()`를 그대로 쓰면 된다
(반드시 `hmac.compare_digest`로 비교하고, 타임스탬프가 5분 이상 오래되면 거부할 것).

### 5.5 프론트엔드 연동

프론트엔드는 별도 저장소 **[kepcoproject/Client](https://github.com/kepcoproject/Client)** 에 있습니다.

- 프론트는 `VITE_API_BASE_URL`에 이 서버 주소를 넣고 `VITE_USE_MOCKS=false`로 바꾸면 붙습니다.
- 개발 중에는 `CORS_ORIGINS=*`라 그대로 붙고, 배포 시에만 프론트 도메인으로 좁히면 됩니다.
- `start-backend.bat` — 더블클릭하면 가상환경 생성 → 패키지 설치 → 더미 데이터 생성 → 서버 실행까지
  자동입니다. 파이썬을 모르는 팀원도 백엔드를 띄울 수 있습니다.

### 5.6 호환 레이어 — 프론트 연동

프론트는 이 서버와 **다른 API 규격**을 전제로 만들어져 있습니다. 경로가 다르고,
응답을 `{success, data, error}` 봉투로 받으며, 공간을 `spaceId` 하나로 식별하고,
모든 요청에 Bearer 토큰을 싣습니다.

프론트를 고치지 않고 붙이기 위해 `app/api/compat_routes.py` 가 그 규격으로
같은 데이터를 다시 내보냅니다. 기존 `/api/*` 는 그대로 살아 있습니다.

| 제공 | 대응 |
| --- | --- |
| `/auth/login`, `/auth/me`, `/auth/refresh` | 시연용 로그인 |
| `/monitoring/occupancy-map`, `/realtime-power`, `/savings` | 대시보드 |
| `/recommendations` | 재실 확률 테이블에서 생성한 절전 추천 |
| `/notifications` | 빈 목록 (웹훅은 서버 대 서버라 브라우저가 못 받음) |

로그인은 공모전 시연 범위에 맞춰 단순화했습니다. 계정(`demo` / `demo1234`)이 코드에
있고 토큰에 만료가 없습니다. 교내망이나 노트북에서 도는 시연에는 충분합니다.

> ⚠️ **인터넷에 공개된 서버에는 올리지 마세요.** `CORS_ORIGINS=*` 와 겹쳐 누구나
> 로그인해 데이터를 볼 수 있습니다. 공개 배포 시에는 `.env` 에
> `COMPAT_API_ENABLED=false` 를 넣어 이 계층을 끄고, 사용자 테이블·비밀번호 해싱·
> 서명과 만료가 있는 토큰으로 교체해야 합니다.
> (서버를 켤 때마다 콘솔에 같은 안내가 출력됩니다)

**연동 확인 방법**

```bash
python scripts/backfill_analytics.py   # 기존 측정값으로 재실 확률 테이블 채우기
```

프론트 `.env` 를 아래로 바꾸고 `npm run dev` 하면 `demo` / `demo1234` 로 로그인됩니다.

```
VITE_API_BASE_URL=http://localhost:8000
VITE_USE_MOCKS=false
```

더미 데이터에는 화면에서 확인해야 할 상태가 전부 들어 있습니다:
사용 중(약 190W) · **낭비 경고**(빈 공간 + 약 110W) · 비어 있음 · 오프라인 노드 · 측정값 없는 노드.

기존 가이드 문서에는 실행 방법, 실제 응답 예시, 실행 방법, 실제 응답 예시,
필드 의미, 시각 처리 규칙(UTC `Z` / epoch 초), 폴링 주기, 상태 코드 처리까지 정리되어 있습니다.
더미 데이터는 `python scripts/seed_demo_data.py --reset`으로 만들며, 브로커나 센서 없이도
교실 5개 × 24시간 분량으로 화면을 개발할 수 있습니다(offline/unknown 노드, 전력 낭비 패턴 포함).

**모든 시각 응답은 UTC이며 끝에 `Z`가 붙습니다.** (`app/schemas.py`의 `to_utc_z`)
시간대 표시가 없으면 JS `new Date()`가 로컬 시간으로 해석해 9시간 어긋나므로, 이 규칙을 지켜야 합니다.
`since` 쿼리는 `+09:00` 오프셋으로 들어와도 서버가 UTC로 정규화해 비교합니다.

## 6. 설계 포인트 (요구사항 대응)

1. **토픽 수신/파싱** — `mqtt_handlers.parse_topic()`이 `v1/{building}/{floor}/{room_id}/{data|status}`를
   분해하고, `SensorDataPayload`/`StatusPayload`(Pydantic)로 payload를 검증합니다. 형식이 안 맞으면
   예외 없이 로그만 남기고 무시하도록 해 잘못된 메시지 하나가 전체 수신 루프를 죽이지 않습니다.
2. **LWT 기반 상태 관리** — `status_cache.py`의 인메모리 캐시에 즉시 반영(빠른 조회용) + DB에도
   write-through로 저장(재시작 내구성용)하는 이중 구조입니다. Redis로 교체하고 싶다면 동일한
   `get_status/set_status/all` 인터페이스의 어댑터로 바꿔 끼우면 됩니다.
3. **백그라운드 MQTT + 예외 처리** — `mqtt_client.py`는 FastAPI `lifespan`에서 기동되어 별도 스레드로
   최초 연결을 시도합니다. 브로커가 꺼져 있어도 API 서버는 정상 기동되고(`mqtt_connected: false`),
   연결 실패 시 1초 → 2초 → 4초 → … 방식으로 `MQTT_RECONNECT_MAX_DELAY`까지 지수 백오프
   재시도합니다. 연결 성공 후 끊김이 발생하면 paho의 `reconnect_delay_set()`이 동일한 정책으로
   자동 재연결을 처리합니다.
4. **환경변수 모듈화** — 브로커 host/port/계정/TLS 여부, DB URL 등 전부 `.env`(`app/config.py`,
   `pydantic-settings`)로 분리되어 있어 배포 환경마다 코드 수정 없이 값만 교체하면 됩니다.
5. **웹훅 격리** — 외부 HTTP 호출은 큐 + 워커 스레드로 완전히 분리되어 있고, `emit()`은 어떤
   경우에도 예외를 밖으로 던지지 않습니다. 즉 수신자 장애가 센서 데이터 수집에 절대 영향을
   주지 않습니다(장애 격리).

## 7. 테스트

```bash
pytest -v
```

- `tests/test_mqtt_handlers.py` — 토픽 파싱, data/status 메시지 처리, 잘못된 payload 무시, DB 반영 검증
- `tests/test_api.py` — FastAPI `TestClient`로 REST 엔드포인트 스모크 테스트
- `tests/test_webhooks.py` — 서명 계산, 재시도/포기 정책, 이벤트 필터, 상태 변화 시 1회만 발행,
  절전 알림 쿨다운, 웹훅 REST 엔드포인트 검증
- `tests/test_analytics.py` — 재실 확률 갱신(누적평균/이동평균), 요일·시간대 분리, 절감률 계산,
  추천 3가지 상태, 분석 REST 엔드포인트 검증

## 8. 공개 배포

현장 시연만 할 거라면 이 절은 건너뛰어도 된다. 노트북에서 돌리는 것으로 충분하다.

인터넷에 올릴 때는 **아래 넷을 반드시 바꿔야 한다.** 하나라도 빠지면 주소를 아는
누구나 공간을 지우거나 기기를 제어할 수 있다.

| 설정 | 값 | 이유 |
| --- | --- | --- |
| `AUTH_SECRET` | 긴 임의 문자열 | 비우면 재시작마다 로그인이 풀린다 |
| `AUTH_DEMO_ACCOUNT` | `false` | `demo/demo1234` 가 공개되면 안 된다 |
| `AUTH_BOOTSTRAP_ADMIN_ID` / `_PASSWORD` | 팀 관리자 계정 | 첫 로그인 수단 |
| `CORS_ORIGINS` | 프론트 주소 | `*` 는 아무 사이트나 API를 부를 수 있다 |

`AUTH_SECRET` 은 이렇게 만든다.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 8.1 권한 구조

| 역할 | 할 수 있는 것 |
| --- | --- |
| `MEMBER` | 조회만 — 대시보드, 리포트, 추천 목록, 공간·노드 목록 |
| `ADMIN` | 위 전부 + 공간 등록·수정·삭제, 노드 등록, 기기 제어, 추천 적용·반려, 사용자 승인 |

회원가입은 `PENDING` 으로 들어가고 관리자가 승인해야 로그인된다.
비밀번호는 PBKDF2-HMAC-SHA256(20만 회, 사용자별 소금)으로 저장하고,
토큰은 서명과 만료 시각을 담는다.

### 8.2 Render 로 올리기

저장소에 `Dockerfile` 과 `render.yaml` 이 들어 있다. Render 에서 저장소를 연결하면
블루프린트를 읽어 웹 서비스와 PostgreSQL 을 함께 만든다. 대시보드에서
`AUTH_BOOTSTRAP_ADMIN_*` 과 `CORS_ORIGINS` 만 채우면 된다.

PostgreSQL 을 쓰려면 `requirements.txt` 의 `psycopg2-binary` 주석을 풀어야 한다.

### 8.3 클라우드에서는 MQTT 가 빠진다

호스팅 업체에는 브로커가 없다. 그래서 클라우드에 올린 서버는 노드를
**HTTP(`POST /api/sensors/data`)로만** 받을 수 있다. MQTT 로 받으려면 외부 브로커
(HiveMQ Cloud 등)를 따로 두고 `MQTT_HOST` 를 그쪽으로 돌려야 한다.

실물 센서를 붙이는 시연은 어차피 같은 네트워크여야 하므로, **클라우드 배포는
"화면을 보여주는 용도", 현장 시연은 "노트북"** 으로 나누는 편이 단순하다.

## 9. 현재 상태와 남은 과제

**공모전 시연 기준으로 완성된 범위**

- MQTT 수신 → 검증 → 저장, LWT 기반 온·오프라인 상태 관리
- 대시보드용 조회 API와 아웃바운드 웹훅(서명·재시도·장애 격리)
- 재실 확률 기반 절전 추천과 절감률 산출 (`app/analytics.py`)
- 프론트엔드 연동 (`app/api/compat_routes.py`) — 로그인, 대시보드, 절전 추천 화면
- 테스트 56개

**정식 서비스로 갈 때 필요한 것**

- 인증 교체 — 사용자 테이블, 비밀번호 해싱, 서명·만료가 있는 토큰
- `CORS_ORIGINS` 를 프론트 도메인으로 제한
- 프론트가 기대하는 나머지 기능 — 공간 CRUD(`/spaces`), 디바이스 등록(`/devices` POST),
  기기 제어(`/control/*`), 리포트(`/reports/savings`), 알림(`/notifications`).
  현재는 대시보드와 절전 추천 화면만 연결되어 있습니다.
- 서보모터·릴레이 제어를 위한 downlink 토픽 (`v1/{building}/{floor}/{room_id}/cmd`)
- 대시보드 실시간 갱신용 WebSocket (현재는 폴링)
- PostgreSQL 전환 시 Alembic 마이그레이션
