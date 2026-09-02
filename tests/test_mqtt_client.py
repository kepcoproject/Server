from app import mqtt_client


class _FakeThread:

    def __init__(self, *args, **kwargs):
        self.name = kwargs.get("name")
        _FakeThread.created.append(self.name)

    def start(self):
        pass


def _patch_thread(monkeypatch):
    _FakeThread.created = []
    monkeypatch.setattr(mqtt_client.threading, "Thread", _FakeThread)


def test_start_skips_connection_when_disabled(monkeypatch):
    _patch_thread(monkeypatch)
    monkeypatch.setattr(mqtt_client.settings, "mqtt_enabled", False)

    mqtt_client.mqtt_service.start()

    assert _FakeThread.created == []
    assert mqtt_client.mqtt_service.is_connected() is False


def test_start_spawns_connect_thread_when_enabled(monkeypatch):
    _patch_thread(monkeypatch)
    monkeypatch.setattr(mqtt_client.settings, "mqtt_enabled", True)

    mqtt_client.mqtt_service.start()

    assert _FakeThread.created == ["mqtt-initial-connect"]
