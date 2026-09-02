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

    # ---- 센서 노드 HTTP 수집 ----
    # ESP32가 CT클램프로 재는 것은 전류(A)라 전력(W)으로 바꾸려면 전압을 곱해야 한다.
    # 국내 단상 220V 기준. 현장 전압이 다르면 여기서 바꾼다.
    sensor_line_voltage: float = 220.0

    # ---- 프론트엔드 호환 레이어 (시연용) ----
    # kepcoproject/Client 가 기대하는 경로·응답 봉투로 같은 데이터를 다시 내보낸다.
    # 단순화한 로그인이 포함되므로 공개 서버에 올릴 때는 끌 것.
    compat_api_enabled: bool = True

    app_name: str = "Smart Energy Saving System - Backend"
    log_level: str = "INFO"
    cors_origins: str = "*"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
