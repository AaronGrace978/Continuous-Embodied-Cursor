# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional, cached readers for the existing motion and emotional substrates.

HTTP runs on dedicated threads. Snapshot/plan/motor code only reads the cache.
Neither service can issue desktop input through this adapter.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.request
from urllib.parse import urlsplit


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Invalid substrate number")
    return value


def _state(name: str, data: dict) -> dict:
    affect = data["affect"]
    result = {"tick": _number(data["tick"]), "affect": {}}
    for key in ("valence", "arousal", "dominance", "curiosity", "solitude"):
        if key in affect:
            low = -1 if key == "valence" else 0
            result["affect"][key] = min(1, max(low, _number(affect[key])))
    if not {"valence", "arousal"} <= result["affect"].keys():
        raise ValueError("Missing affect")
    if name == "motion":
        for key in ("position", "velocity"):
            values = data[key]
            if not isinstance(values, list) or not 1 <= len(values) <= 16:
                raise ValueError("Invalid motion vector")
            result[key] = [_number(value) for value in values]
        result["running"] = data.get("running") is True
    else:
        result["memory_count"] = _number(data.get("memory_count", 0))
    return result


class SubstrateMonitor:
    def __init__(self, urls: dict | None = None, interval: float = 1.0, stale_after: float = 3.0):
        self.urls = urls or {
            "emotion": os.environ.get("CEC_CHRONOSOUL_URL", "http://127.0.0.1:47321/state"),
            "motion": os.environ.get("CEC_MOTION_URL", "http://127.0.0.1:47400/state"),
        }
        for url in self.urls.values():
            parsed = urlsplit(url)
            if (parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                    or parsed.username or parsed.password or parsed.path != "/state" or parsed.query or parsed.fragment):
                raise ValueError("Substrate endpoints must be loopback /state URLs")
        self.interval, self.stale_after = interval, stale_after
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._latest: dict = {}
        self.enabled = False

    def start(self) -> None:
        if self.enabled:
            return
        self.enabled = True
        self._stop = threading.Event()
        for name, url in self.urls.items():
            worker = threading.Thread(target=self._poll, args=(name, url, self._stop), name=f"cec-{name}-reader", daemon=True)
            self._threads.append(worker)
            worker.start()

    def stop(self) -> None:
        self.enabled = False
        self._stop.set()
        for worker in self._threads:
            worker.join(timeout=1.0)
        self._threads.clear()
        with self._lock:
            self._latest.clear()

    def _poll(self, name: str, url: str, stop: threading.Event) -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        while not stop.is_set():
            try:
                request = urllib.request.Request(url, headers={"Accept": "application/json"})
                with opener.open(request, timeout=0.4) as response:
                    raw = response.read(512 * 1024 + 1)
                if len(raw) > 512 * 1024:
                    raise ValueError("Substrate response too large")
                state = _state(name, json.loads(raw))
                with self._lock:
                    if not stop.is_set():
                        self._latest[name] = (time.monotonic(), state)
            except Exception:
                # Never pass provider bodies, transcripts or synthetic mood as live state.
                with self._lock:
                    if not stop.is_set():
                        self._latest.pop(name, None)
            stop.wait(self.interval)

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            latest = dict(self._latest)
        result: dict = {"enabled": self.enabled}
        for name in self.urls:
            stamp, state = latest.get(name, (0.0, {}))
            fresh = self.enabled and bool(state) and now - stamp <= self.stale_after
            result[name] = {"connected": fresh, **(state if fresh else {})}
        return result
