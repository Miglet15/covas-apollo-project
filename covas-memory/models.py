from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime


# ── Inbound from UNIT-01 ──────────────────────────────────────────────────────

class SessionPayload(BaseModel):
    """Raw session log sent from UNIT-01 at end of session or on interval."""
    session_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    raw_text: str                          # Full or partial session transcript
    trigger: Literal["session_end", "interval"] = "session_end"
    # Optional ED context hints UNIT-01 can pass along
    ed_context: Optional[dict] = None      # e.g. {"system": "Sol", "station": "Jameson"}


# ── Memory Records (stored in DB) ────────────────────────────────────────────

class EntityRecord(BaseModel):
    name: str
    entity_type: Literal["person", "place", "faction", "ship", "other"]
    context: Optional[str] = None


class EDMissionRecord(BaseModel):
    mission_id: Optional[str] = None
    mission_type: Optional[str] = None      # e.g. "Delivery", "Assassination"
    giver: Optional[str] = None             # NPC name
    target: Optional[str] = None
    origin_system: Optional[str] = None
    origin_station: Optional[str] = None
    destination_system: Optional[str] = None
    destination_station: Optional[str] = None
    reward: Optional[str] = None
    status: Optional[str] = None            # "active", "complete", "failed"
    notes: Optional[str] = None


class MemoryRecord(BaseModel):
    session_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    category: Literal[
        "general",
        "elite_dangerous",
        "person",
        "place",
        "preference",
        "task"
    ]
    topic: str                              # Short label e.g. "Wing mission debrief"
    summary: str                            # LLM-generated condensed memory
    emotional_tone: Optional[str] = None   # "neutral", "positive", "tense" etc.
    entities: List[EntityRecord] = []
    ed_mission: Optional[EDMissionRecord] = None
    raw_ref: Optional[str] = None          # Optional pointer back to raw log


# ── Outbound / Query responses ────────────────────────────────────────────────

class MemoryQueryRequest(BaseModel):
    query: Optional[str] = None
    category: Optional[str] = None
    session_id: Optional[str] = None
    limit: int = 20


class MemoryQueryResponse(BaseModel):
    memories: List[dict]
    total: int
