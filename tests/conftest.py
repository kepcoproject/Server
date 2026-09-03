import os
import tempfile

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_db.name}")
os.environ.setdefault("MQTT_HOST", "127.0.0.1")
os.environ.setdefault("MQTT_PORT", "1883")
os.environ.setdefault("MQTT_RECONNECT_MIN_DELAY", "1")
os.environ.setdefault("MQTT_RECONNECT_MAX_DELAY", "2")

# 로컬 .env 에 접두사가 설정돼 있어도 테스트는 항상 루트 기준으로 돈다.
# (화면을 같이 서빙하는 배포에서는 접두사를 쓰지만, 계약 검증은 기본값으로 한다)
os.environ.setdefault("COMPAT_API_PREFIX", "")
