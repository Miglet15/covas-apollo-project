import sqlite3
import json
import os
from datetime import datetime
from typing import Optional, List
from models import MemoryRecord

DB_PATH = os.getenv("DB_PATH", "/data/covas_memory.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")   # Safe concurrent reads/writes
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    """Create all tables on first run."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_conn()
    c = conn.cursor()

    # Sessions table — one row per UNIT-01 session
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id   TEXT PRIMARY KEY,
            started_at   TEXT NOT NULL,
            last_updated TEXT NOT NULL,
            trigger_type TEXT,
            summary      TEXT,
            ed_context   TEXT          -- JSON blob of ED system/station context
        )
    """)

    # Core memories table
    c.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            category       TEXT NOT NULL,
            topic          TEXT NOT NULL,
            summary        TEXT NOT NULL,
            emotional_tone TEXT,
            raw_ref        TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        )
    """)

    # Entities table — persons, places, factions etc.
    c.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id   INTEGER NOT NULL,
            session_id  TEXT NOT NULL,
            name        TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            context     TEXT,
            FOREIGN KEY (memory_id) REFERENCES memories(id)
        )
    """)

    # Elite Dangerous missions table
    c.execute("""
        CREATE TABLE IF NOT EXISTS ed_missions (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id            INTEGER NOT NULL,
            session_id           TEXT NOT NULL,
            mission_id           TEXT,
            mission_type         TEXT,
            giver                TEXT,
            target               TEXT,
            origin_system        TEXT,
            origin_station       TEXT,
            destination_system   TEXT,
            destination_station  TEXT,
            reward               TEXT,
            status               TEXT DEFAULT 'active',
            notes                TEXT,
            created_at           TEXT NOT NULL,
            FOREIGN KEY (memory_id) REFERENCES memories(id)
        )
    """)

    # Error log table — persists processing errors across restarts
    c.execute("""
        CREATE TABLE IF NOT EXISTS error_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            message   TEXT NOT NULL,
            context   TEXT
        )
    """)

    # Useful indexes
    c.execute("CREATE INDEX IF NOT EXISTS idx_memories_session  ON memories(session_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_entities_name     ON entities(name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ed_missions_status ON ed_missions(status)")

    conn.commit()
    conn.close()
    print(f"[storage] DB initialised at {DB_PATH}")


def upsert_session(session_id: str, trigger: str, ed_context: Optional[dict]):
    conn = get_conn()
    now = datetime.utcnow().isoformat()
    conn.execute("""
        INSERT INTO sessions (session_id, started_at, last_updated, trigger_type, ed_context)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            last_updated = excluded.last_updated,
            trigger_type = excluded.trigger_type,
            ed_context   = excluded.ed_context
    """, (session_id, now, now, trigger, json.dumps(ed_context) if ed_context else None))
    conn.commit()
    conn.close()


def save_memory(record: MemoryRecord) -> int:
    """Insert a memory + its entities/missions. Returns memory row id."""
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        INSERT INTO memories (session_id, created_at, category, topic, summary, emotional_tone, raw_ref)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        record.session_id,
        record.created_at.isoformat(),
        record.category,
        record.topic,
        record.summary,
        record.emotional_tone,
        record.raw_ref
    ))
    memory_id = c.lastrowid

    for ent in record.entities:
        c.execute("""
            INSERT INTO entities (memory_id, session_id, name, entity_type, context)
            VALUES (?, ?, ?, ?, ?)
        """, (memory_id, record.session_id, ent.name, ent.entity_type, ent.context))

    if record.ed_mission:
        m = record.ed_mission
        c.execute("""
            INSERT INTO ed_missions (
                memory_id, session_id, mission_id, mission_type, giver, target,
                origin_system, origin_station, destination_system, destination_station,
                reward, status, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            memory_id, record.session_id,
            m.mission_id, m.mission_type, m.giver, m.target,
            m.origin_system, m.origin_station,
            m.destination_system, m.destination_station,
            m.reward, m.status or "active", m.notes,
            record.created_at.isoformat()
        ))

    conn.commit()
    conn.close()
    return memory_id


def query_memories(
    category: Optional[str] = None,
    session_id: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 20
) -> List[dict]:
    conn = get_conn()
    c = conn.cursor()

    query = "SELECT * FROM memories WHERE 1=1"
    params = []

    if category:
        query += " AND category = ?"
        params.append(category)
    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    if search:
        query += " AND (summary LIKE ? OR topic LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = c.execute(query, params).fetchall()
    results = []
    for row in rows:
        r = dict(row)
        # Attach entities
        r["entities"] = [dict(e) for e in c.execute(
            "SELECT * FROM entities WHERE memory_id = ?", (r["id"],)
        ).fetchall()]
        # Attach ED mission if any
        r["ed_mission"] = dict(c.execute(
            "SELECT * FROM ed_missions WHERE memory_id = ?", (r["id"],)
        ).fetchone() or {}) or None
        results.append(r)

    conn.close()
    return results


def get_active_ed_missions() -> List[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM ed_missions WHERE status = 'active' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_ed_mission_status(mission_id: str, status: str):
    conn = get_conn()
    conn.execute(
        "UPDATE ed_missions SET status = ? WHERE mission_id = ?",
        (status, mission_id)
    )
    conn.commit()
    conn.close()


def save_error(entry: dict):
    """Persist an error entry to the DB. Caps the table at 500 rows (oldest pruned)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO error_log (timestamp, message, context) VALUES (?, ?, ?)",
        (entry.get("timestamp"), entry.get("message"), entry.get("context", ""))
    )
    # Prune oldest rows beyond cap
    conn.execute("""
        DELETE FROM error_log WHERE id NOT IN (
            SELECT id FROM error_log ORDER BY id DESC LIMIT 500
        )
    """)
    conn.commit()
    conn.close()


def get_error_log(limit: int = 100) -> list:
    """Return the most recent errors, newest first."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT timestamp, message, context FROM error_log ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_error_log():
    """Delete all rows from the error_log table."""
    conn = get_conn()
    conn.execute("DELETE FROM error_log")
    conn.commit()
    conn.close()


def get_stats() -> dict:
    conn = get_conn()
    c = conn.cursor()

    total_memories  = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    total_sessions  = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    total_entities  = c.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    total_missions  = c.execute("SELECT COUNT(*) FROM ed_missions").fetchone()[0]
    active_missions = c.execute("SELECT COUNT(*) FROM ed_missions WHERE status='active'").fetchone()[0]

    by_category = {
        row[0]: row[1]
        for row in c.execute(
            "SELECT category, COUNT(*) FROM memories GROUP BY category ORDER BY COUNT(*) DESC"
        ).fetchall()
    }

    last_memory = c.execute(
        "SELECT created_at, topic, category FROM memories ORDER BY created_at DESC LIMIT 1"
    ).fetchone()

    last_session = c.execute(
        "SELECT session_id, last_updated FROM sessions ORDER BY last_updated DESC LIMIT 1"
    ).fetchone()

    recent_memories = [
        dict(r) for r in c.execute(
            "SELECT created_at, category, topic, summary FROM memories ORDER BY created_at DESC LIMIT 8"
        ).fetchall()
    ]

    conn.close()
    return {
        "total_memories":  total_memories,
        "total_sessions":  total_sessions,
        "total_entities":  total_entities,
        "total_missions":  total_missions,
        "active_missions": active_missions,
        "by_category":     by_category,
        "last_memory":     dict(last_memory) if last_memory else None,
        "last_session":    dict(last_session) if last_session else None,
        "recent_memories": recent_memories,
    }
