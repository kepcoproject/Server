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

    app_name: str = "Smart Energy Saving System - Backend"
    log_level: str = "INFO"
    cors_origins: str = "*"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
