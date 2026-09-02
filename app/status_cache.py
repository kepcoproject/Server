import threading
from datetime import datetime

from .utils import utcnow
from typing import Dict, Optional, TypedDict


class StatusEntry(TypedDict):
    status: str
    changed_at: datetime


class DeviceStatusCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._store: Dict[str, StatusEntry] = {}

    def set_status(self, device_id: str, status: str, changed_at: Optional[datetime] = None) -> None:
        with self._lock:
            self._store[device_id] = {
                "status": status,
                "changed_at": changed_at or utcnow(),
            }

    def get_status(self, device_id: str) -> Optional[StatusEntry]:
        with self._lock:
            return self._store.get(device_id)

    def drop(self, device_id: str) -> None:
        with self._lock:
            self._store.pop(device_id, None)

    def all(self) -> Dict[str, StatusEntry]:
        with self._lock:
            return dict(self._store)


status_cache = DeviceStatusCache()
