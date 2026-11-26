from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


class JsonCache:
    def __init__(self, directory: str | Path = ".site_audit_cache", ttl_seconds: int = 86400):
        self.directory = Path(directory)
        self.ttl_seconds = ttl_seconds
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - payload["saved_at"] > self.ttl_seconds:
                return None
            return payload["value"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def set(self, key: str, value: Any) -> None:
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved_at": time.time(), "value": value}), encoding="utf-8")
        tmp.replace(path)
