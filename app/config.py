from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    mqtt_enabled: bool = True
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: Optional[str] = None
    mqtt_password: Optional[str] = None
    mqtt_use_tls: bool = False
    mqtt_client_id: str = "smart-energy-backend"
    mqtt_keepalive: int = 60

    mqtt_reconnect_min_delay: int = 1
    mqtt_reconnect_max_delay: int = 120

    mqtt_topic_prefix: str = "v1"
    # 제어 명령을 노드로 내려보낼 때 쓰는 QoS. 1이면 최소 한 번은 도착한다.
    mqtt_command_qos: int = 1

    database_url: str = "sqlite:///./smart_energy.db"

    webhook_enabled: bool = False
    webhook_urls: str = ""
    webhook_secret: Optional[str] = None
    webhook_events: str = "*"
    webhook_timeout: float = 5.0
    webhook_max_retries: int = 3
    webhook_retry_backoff: int = 2

    webhook_power_threshold: float = 10.0
    webhook_alert_cooldown: int = 300

    # ---- 분석 로직 (계획서 4.2 재실 확률 기반 절전 추천) ----
    analytics_enabled: bool = True
    # 이 확률 미만이면 '유휴 시간대'로 간주해 절전을 제안
    analytics_idle_threshold: float = 0.2
    # 이동평균 가중치 (0~1, 클수록 최근 값에 민감)
    analytics_ema_alpha: float = 0.15
    # 표본이 이만큼 쌓이면 누적평균 -> 이동평균으로 전환
    analytics_ema_min_samples: int = 20
    # 절감률 계산의 '상시 켜짐' 기준선 (W)
    analytics_baseline_power_w: float = 200.0
    # 재실 패턴을 몇 시 기준으로 묶을지. 서버는 UTC로 저장하지만 사람의 생활 패턴은
    # 현지 시각을 따르므로, 요일·시간대 셀은 현지 기준으로 나눠야 한다.
    # 한국(KST)은 9. 이 값을 바꾸면 scripts/backfill_analytics.py --reset 로 다시 계산할 것.
    analytics_utc_offset_hours: int = 9

    # 이 조도(lux) 이상이면 자연광이 충분하다고 본다.
    # 재실 중이어도 조명을 켜고 있으면 낭비로 잡아낸다 — PIR만으로는 못 잡는 경우다.
    analytics_daylight_lux: float = 400.0

    # ---- 절전 추천 자동 실행 ----
    # 적용된 추천의 시간대가 되면 실제로 제어 명령을 낸다.
    # 그 순간 실제로 비어 있을 때만 끄므로, 사람이 있는 방의 불이 꺼지지는 않는다.
    auto_control_enabled: bool = True
    auto_control_interval_seconds: int = 60

    # ---- 센서 노드 HTTP 수집 ----
    # ESP32가 CT클램프로 재는 것은 전류(A)라 전력(W)으로 바꾸려면 전압을 곱해야 한다.
    # 국내 단상 220V 기준. 현장 전압이 다르면 여기서 바꾼다.
    sensor_line_voltage: float = 220.0

    # ---- 인증 ----
    # 토큰 서명 키. 배포 시 반드시 넣을 것. 비워두면 기동할 때마다 새로 만들어져
    # 서버를 재시작할 때 모든 세션이 끊긴다(로컬 개발에는 문제없음).
    auth_secret: Optional[str] = None
    auth_access_ttl_minutes: int = 60
    auth_refresh_ttl_days: int = 14

    # 시연용 demo/demo1234 계정을 자동으로 만들지 여부.
    # 공개 배포에서는 반드시 false 로 두고 아래 부트스트랩 관리자를 쓸 것.
    auth_demo_account: bool = True

    # 첫 관리자 계정. 값이 있으면 기동 시 없을 때만 만든다.
    auth_bootstrap_admin_id: Optional[str] = None
    auth_bootstrap_admin_password: Optional[str] = None

    # ---- 메일 (회원가입 인증번호 · 비밀번호 재설정) ----
    # 비워두면 메일을 보내지 않고 내용(인증번호·링크)을 서버 로그에 남긴다.
    # SMTP 계정이 준비되기 전에도 흐름을 확인할 수 있게 하기 위한 것이고,
    # 공개 배포에서는 반드시 채울 것.
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    # 587 은 STARTTLS, 465 는 처음부터 SSL. 포트에 맞춰 자동으로 고른다.
    smtp_use_tls: bool = True
    # 비워두면 SMTP_USERNAME 으로 보낸다. Gmail 은 로그인한 주소로만 보낼 수 있다.
    smtp_from: Optional[str] = None
    smtp_from_name: str = "스마트 에너지 절약 시스템"
    smtp_timeout: float = 10.0

    # 메일 본문에 넣을 링크의 앞부분. 사용자가 브라우저로 여는 주소다.
    # 비워두면 요청이 들어온 주소에서 유추한다(리버스 프록시 뒤에서는 틀릴 수 있다).
    public_base_url: Optional[str] = None

    # 비밀번호 재설정 링크의 유효 시간
    password_reset_ttl_minutes: int = 30

    # 회원가입 인증번호. 메일로 여섯 자리 숫자를 보내고 가입 화면에서 입력받는다.
    email_code_ttl_minutes: int = 10
    # 같은 주소로 다시 보내려면 기다려야 하는 시간과 1시간에 보낼 수 있는 횟수.
    # 남의 주소로 메일을 퍼붓는 데 쓰이지 않게 한다.
    email_code_resend_seconds: int = 60
    email_code_hourly_limit: int = 5
    # 틀릴 수 있는 횟수. 여섯 자리뿐이라 제한이 없으면 금방 맞힌다.
    email_code_max_attempts: int = 5
    # 인증번호를 맞힌 뒤 가입 버튼을 누르기까지 기다려 주는 시간
    email_ticket_ttl_minutes: int = 30

    # ---- 프론트엔드 호환 레이어 (시연용) ----
    # kepcoproject/Client 가 기대하는 경로·응답 봉투로 같은 데이터를 다시 내보낸다.
    # 단순화한 로그인이 포함되므로 공개 서버에 올릴 때는 끌 것.
    compat_api_enabled: bool = True
    # 호환 API를 붙일 접두사. 화면과 같은 주소에서 서빙할 때는 경로가 겹치므로
    # ("/spaces" 가 화면이면서 API가 된다) 접두사를 줘서 떼어놓는다.
    # 로컬 개발처럼 화면을 따로 띄울 때는 빈 값으로 두면 된다.
    compat_api_prefix: str = ""

    # 빌드된 프론트엔드를 같이 서빙할지. 이 경로에 index.html 이 있으면 켜진다.
    frontend_dir: str = "web"

    app_name: str = "Smart Energy Saving System - Backend"
    log_level: str = "INFO"
    cors_origins: str = "*"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
