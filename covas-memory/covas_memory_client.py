"""
covas_memory_client.py  —  runs on UNIT-01
Handles sending session data to Apollo's memory service.
Can be called directly at session end, or run as a background thread for interval sends.
"""

import requests
import threading
import uuid
import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger("covas.memory_client")

# ── Config ────────────────────────────────────────────────────────────────────
# Apollo's IP on your local network or Tailscale address
MEMORY_SERVICE_URL = "http://192.168.1.65:8100"   # Update with Apollo's actual IP
# Or use Tailscale: MEMORY_SERVICE_URL = "http://100.x.x.x:8100"

INTERVAL_SECONDS = 300   # Send interval snapshot every 5 minutes


class MemoryClient:
    def __init__(
        self,
        service_url: str = MEMORY_SERVICE_URL,
        interval: int = INTERVAL_SECONDS
    ):
        self.service_url = service_url
        self.interval = interval
        self.session_id: str = self._new_session_id()
        self._interval_timer: Optional[threading.Timer] = None
        self._buffer: list[str] = []   # Accumulates log lines between sends
        self._lock = threading.Lock()

    # ── Session management ────────────────────────────────────────────────────

    def _new_session_id(self) -> str:
        return f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    def new_session(self):
        """Call this when COVAS starts a new conversation session."""
        with self._lock:
            self.session_id = self._new_session_id()
            self._buffer.clear()
        self._start_interval_timer()
        log.info(f"[MemoryClient] New session: {self.session_id}")

    # ── Buffer: accumulate log lines ──────────────────────────────────────────

    def append(self, text: str):
        """Add a line/chunk from the session log to the buffer."""
        with self._lock:
            self._buffer.append(text)

    def _drain_buffer(self) -> str:
        with self._lock:
            content = "\n".join(self._buffer)
            self._buffer.clear()
        return content

    # ── Sending ───────────────────────────────────────────────────────────────

    def _send(self, raw_text: str, trigger: str, ed_context: Optional[dict] = None):
        if not raw_text.strip():
            log.debug("[MemoryClient] Nothing to send, buffer was empty.")
            return
        payload = {
            "session_id": self.session_id,
            "timestamp": datetime.utcnow().isoformat(),
            "raw_text": raw_text,
            "trigger": trigger,
            "ed_context": ed_context
        }
        try:
            resp = requests.post(
                f"{self.service_url}/ingest",
                json=payload,
                timeout=10
            )
            resp.raise_for_status()
            log.info(f"[MemoryClient] Sent ({trigger}) → {resp.status_code}")
        except requests.RequestException as e:
            log.warning(f"[MemoryClient] Failed to send to memory service: {e}")

    def send_session_end(self, ed_context: Optional[dict] = None):
        """Call this when the COVAS session ends."""
        self._stop_interval_timer()
        raw_text = self._drain_buffer()
        self._send(raw_text, "session_end", ed_context)
        log.info(f"[MemoryClient] Session end sent: {self.session_id}")

    # ── Interval timer ────────────────────────────────────────────────────────

    def _interval_send(self):
        raw_text = self._drain_buffer()
        self._send(raw_text, "interval")
        self._start_interval_timer()   # Reschedule

    def _start_interval_timer(self):
        self._stop_interval_timer()
        self._interval_timer = threading.Timer(self.interval, self._interval_send)
        self._interval_timer.daemon = True
        self._interval_timer.start()

    def _stop_interval_timer(self):
        if self._interval_timer:
            self._interval_timer.cancel()
            self._interval_timer = None

    # ── Query helpers ─────────────────────────────────────────────────────────

    def get_recent_memories(self, limit: int = 10) -> list:
        try:
            resp = requests.get(f"{self.service_url}/memories/recent?limit={limit}", timeout=10)
            resp.raise_for_status()
            return resp.json().get("memories", [])
        except requests.RequestException as e:
            log.warning(f"[MemoryClient] Query failed: {e}")
            return []

    def get_active_missions(self) -> list:
        try:
            resp = requests.get(f"{self.service_url}/ed/missions/active", timeout=10)
            resp.raise_for_status()
            return resp.json().get("missions", [])
        except requests.RequestException as e:
            log.warning(f"[MemoryClient] Mission query failed: {e}")
            return []

    def search_memories(self, query: str, category: Optional[str] = None, limit: int = 10) -> list:
        try:
            resp = requests.post(
                f"{self.service_url}/memories/query",
                json={"query": query, "category": category, "limit": limit},
                timeout=10
            )
            resp.raise_for_status()
            return resp.json().get("memories", [])
        except requests.RequestException as e:
            log.warning(f"[MemoryClient] Search failed: {e}")
            return []


# ── Singleton for easy import into COVAS ─────────────────────────────────────
memory = MemoryClient()
