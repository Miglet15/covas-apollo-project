from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse
from datetime import datetime
import logging
import httpx
import os

from models import SessionPayload, MemoryQueryRequest
from summarizer import extract_memories
from storage import (
    init_db, upsert_session, save_memory,
    query_memories, get_active_ed_missions,
    update_ed_mission_status, get_stats,
    save_error, get_error_log, clear_error_log
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("covas-memory")

OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "phi3:mini")

app = FastAPI(title="COVAS Memory Service", version="1.0.0")

# ── Runtime stats ─────────────────────────────────────────────────────────────
_runtime = {
    "start_time":        datetime.utcnow(),
    "sessions_received": 0,
    "memories_stored":   0,
    "last_ingest":       None,
    "last_session_id":   None,
    "errors":            0,
    "error_log":         [],   # in-memory cache, capped at 200
}

def _record_error(message: str, context: str = ""):
    """Log an error with full detail, persist to DB, and increment counter."""
    _runtime["errors"] += 1
    entry = {
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "message":   message,
        "context":   context,
    }
    _runtime["error_log"].append(entry)
    if len(_runtime["error_log"]) > 200:
        _runtime["error_log"] = _runtime["error_log"][-200:]
    try:
        save_error(entry)
    except Exception:
        pass


@app.on_event("startup")
def startup():
    init_db()
    # Restore persisted error log and count into _runtime
    try:
        persisted = get_error_log(limit=200)
        _runtime["error_log"] = persisted
        _runtime["errors"]    = len(persisted)
    except Exception:
        pass
    log.info("COVAS Memory Service started.")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# ── Stats JSON (for external polling e.g. UNIT-01 status page) ───────────────

@app.get("/stats")
def stats():
    db = get_stats()
    uptime = (datetime.utcnow() - _runtime["start_time"]).total_seconds()
    return {
        "uptime_seconds":    int(uptime),
        "sessions_received": _runtime["sessions_received"],
        "memories_stored":   _runtime["memories_stored"],
        "errors":            _runtime["errors"],
        "error_log":         list(reversed(_runtime["error_log"]))[:20],  # newest first, UNIT-01 preview
        "last_ingest":       _runtime["last_ingest"],
        "last_session_id":   _runtime["last_session_id"],
        **db,
    }


# ── Status HTML page ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def status_page():
    # Check Ollama reachability
    ollama_ok = False
    try:
        with httpx.Client(timeout=3.0) as c:
            r = c.get(f"{OLLAMA_URL}/api/tags")
            ollama_ok = r.status_code == 200
    except Exception:
        pass

    try:
        db = get_stats()
    except Exception:
        db = {}

    uptime     = datetime.utcnow() - _runtime["start_time"]
    h, rem     = divmod(int(uptime.total_seconds()), 3600)
    m, s       = divmod(rem, 60)
    uptime_str = f"{h}h {m}m {s}s"

    last_ingest = _runtime["last_ingest"] or "None"
    last_sid    = _runtime["last_session_id"] or "—"
    ollama_cls  = "ok" if ollama_ok else "err"
    ollama_txt  = f"● Online ({OLLAMA_MODEL})" if ollama_ok else "● Unreachable"

    # Category rows
    category_rows = ""
    for cat, count in (db.get("by_category") or {}).items():
        category_rows += f"<tr><td>{cat}</td><td>{count}</td></tr>\n"
    if not category_rows:
        category_rows = "<tr><td colspan='2' style='color:#555'>No memories yet</td></tr>"

    # Recent memories
    recent_rows = ""
    for m_rec in (db.get("recent_memories") or []):
        ts      = str(m_rec.get("created_at", ""))[:16].replace("T", " ")
        cat     = m_rec.get("category", "")
        topic   = m_rec.get("topic", "")
        summary = m_rec.get("summary", "")
        short   = summary[:120] + ("…" if len(summary) > 120 else "")
        recent_rows += (
            f"<tr>"
            f"<td style='color:#888;white-space:nowrap'>{ts}</td>"
            f"<td><span class='badge'>{cat}</span></td>"
            f"<td><strong>{topic}</strong><br>"
            f"<span style='color:#777;font-size:11px'>{short}</span></td>"
            f"</tr>\n"
        )
    if not recent_rows:
        recent_rows = "<tr><td colspan='3' style='color:#555'>No memories yet</td></tr>"

    # Active missions
    try:
        missions = get_active_ed_missions()
    except Exception:
        missions = []

    mission_rows = ""
    for ms in missions:
        mission_rows += (
            f"<tr>"
            f"<td>{ms.get('mission_type') or 'Unknown'}</td>"
            f"<td>{ms.get('giver','—')}</td>"
            f"<td>{ms.get('origin_system','?')} / {ms.get('origin_station','?')}</td>"
            f"<td>{ms.get('destination_system','?')} / {ms.get('destination_station','?')}</td>"
            f"<td>{ms.get('reward') or '—'}</td>"
            f"</tr>\n"
        )
    if not mission_rows:
        mission_rows = "<tr><td colspan='5' style='color:#555'>No active missions on record</td></tr>"

    last_mem_txt = "—"
    if db.get("last_memory"):
        lm = db["last_memory"]
        last_mem_txt = f"{lm.get('topic','')} [{lm.get('category','')}] @ {str(lm.get('created_at',''))[:16]}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="15">
  <title>COVAS Memory — Apollo</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Courier New', monospace;
      background: #07070d;
      color: #c8a040;
      padding: 32px 40px;
    }}
    h1 {{ color: #ff7700; letter-spacing: 4px; font-size: 22px; margin-bottom: 4px; }}
    .subtitle {{ color: #553300; font-size: 11px; margin-bottom: 30px; letter-spacing: 2px; }}
    h2 {{
      color: #cc6600; font-size: 12px; letter-spacing: 3px; text-transform: uppercase;
      border-bottom: 1px solid #1e1000; padding-bottom: 5px; margin: 26px 0 10px;
    }}
    .stat-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 4px; }}
    .stat-box {{
      background: #0c0800; border: 1px solid #251500;
      padding: 14px 18px; border-radius: 3px;
    }}
    .big-num {{ font-size: 30px; color: #ff9922; display: block; line-height: 1; margin-bottom: 3px; }}
    .stat-label {{ font-size: 10px; color: #664400; text-transform: uppercase; letter-spacing: 1px; }}
    .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    td, th {{ padding: 6px 12px; border: 1px solid #1a0e00; font-size: 12px; vertical-align: top; }}
    th {{ color: #774400; font-weight: normal; background: #0a0600;
          text-transform: uppercase; font-size: 10px; letter-spacing: 1px; }}
    td:first-child {{ color: #665500; white-space: nowrap; }}
    .ok   {{ color: #44dd77; }}
    .err  {{ color: #ff4444; }}
    .warn {{ color: #ffaa00; }}
    .badge {{
      display: inline-block; background: #150a00; border: 1px solid #3a1800;
      color: #bb6600; font-size: 10px; padding: 1px 6px; border-radius: 3px; white-space: nowrap;
    }}
    .footer {{ color: #2a1a00; font-size: 11px; margin-top: 36px; }}
  </style>
</head>
<body>

  <h1>◈ COVAS MEMORY — APOLLO</h1>
  <div class="subtitle">MEMORY MICROSERVICE · AUTO-REFRESH 15s · {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</div>

  <div class="stat-grid">
    <div class="stat-box">
      <span class="big-num">{db.get('total_memories', 0)}</span>
      <span class="stat-label">Total Memories</span>
    </div>
    <div class="stat-box">
      <span class="big-num">{db.get('total_sessions', 0)}</span>
      <span class="stat-label">Sessions Stored</span>
    </div>
    <div class="stat-box">
      <span class="big-num">{db.get('active_missions', 0)}</span>
      <span class="stat-label">Active ED Missions</span>
    </div>
    <div class="stat-box">
      <span class="big-num">{_runtime['errors']}</span>
      <span class="stat-label">Processing Errors</span>
    </div>
  </div>

  <div class="two-col">
    <div>
      <h2>Service Status</h2>
      <table>
        <tr><td>Status</td>         <td class="ok">● Online</td></tr>
        <tr><td>Uptime</td>         <td>{uptime_str}</td></tr>
        <tr><td>Ollama / Model</td> <td class="{ollama_cls}">{ollama_txt}</td></tr>
        <tr><td>Sessions (this run)</td><td>{_runtime['sessions_received']}</td></tr>
        <tr><td>Memories (this run)</td><td>{_runtime['memories_stored']}</td></tr>
        <tr><td>Last Ingest</td>    <td>{last_ingest}</td></tr>
        <tr><td>Last Session ID</td><td style="font-size:10px">{last_sid}</td></tr>
        <tr><td>Last Memory</td>    <td style="font-size:10px">{last_mem_txt}</td></tr>
      </table>

      <h2>Memories by Category</h2>
      <table>
        <tr><th>Category</th><th>Count</th></tr>
        {category_rows}
        <tr><td>Entities tracked</td><td>{db.get('total_entities', 0)}</td></tr>
        <tr><td>ED Missions (total)</td><td>{db.get('total_missions', 0)}</td></tr>
      </table>
    </div>

    <div>
      <h2>Recent Memories</h2>
      <table>
        <tr><th style="width:105px">Time</th><th style="width:100px">Category</th><th>Topic / Summary</th></tr>
        {recent_rows}
      </table>
    </div>
  </div>

  <h2>Active Elite Dangerous Missions</h2>
  <table>
    <tr><th>Type</th><th>Giver</th><th>Origin</th><th>Destination</th><th>Reward</th></tr>
    {mission_rows}
  </table>

  <p class="footer">
    Apollo · covas-memory v1.0 · Ollama: {OLLAMA_URL} · DB: {db.get('total_memories',0)} memories across {db.get('total_sessions',0)} sessions
  </p>

</body>
</html>"""
    return HTMLResponse(content=html)


# ── Core: ingest session from UNIT-01 ────────────────────────────────────────

def _process_session(payload: SessionPayload):
    log.info(f"Processing session {payload.session_id} (trigger={payload.trigger})")
    try:
        upsert_session(payload.session_id, payload.trigger, payload.ed_context)
        records = extract_memories(payload.session_id, payload.raw_text)
        count = 0
        for record in records:
            memory_id = save_memory(record)
            log.info(f"  Saved memory #{memory_id}: [{record.category}] {record.topic}")
            count += 1
        _runtime["sessions_received"] += 1
        _runtime["memories_stored"]   += count
        _runtime["last_ingest"]        = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        _runtime["last_session_id"]    = payload.session_id
        log.info(f"Session {payload.session_id} — {count} memories stored.")
    except Exception as e:
        _record_error(str(e), f"session={payload.session_id} trigger={payload.trigger}")
        log.error(f"Failed to process session {payload.session_id}: {e}")


@app.post("/ingest", status_code=202)
def ingest_session(payload: SessionPayload, background_tasks: BackgroundTasks):
    if not payload.raw_text.strip():
        raise HTTPException(status_code=400, detail="raw_text cannot be empty")
    background_tasks.add_task(_process_session, payload)
    return {"status": "accepted", "session_id": payload.session_id}


# ── Query memories ────────────────────────────────────────────────────────────

@app.post("/memories/query")
def query(req: MemoryQueryRequest):
    results = query_memories(
        category=req.category,
        session_id=req.session_id,
        search=req.query,
        limit=req.limit
    )
    return {"memories": results, "total": len(results)}


@app.get("/memories/recent")
def recent_memories(limit: int = 10):
    results = query_memories(limit=limit)
    return {"memories": results, "total": len(results)}


@app.get("/memories/session/{session_id}")
def memories_by_session(session_id: str):
    results = query_memories(session_id=session_id)
    return {"memories": results, "total": len(results)}


# ── Elite Dangerous specific ──────────────────────────────────────────────────

@app.get("/ed/missions/active")
def active_missions():
    missions = get_active_ed_missions()
    return {"missions": missions, "total": len(missions)}


@app.patch("/ed/missions/{mission_id}/status")
def set_mission_status(mission_id: str, status: str):
    if status not in ("active", "complete", "failed"):
        raise HTTPException(status_code=400, detail="status must be active, complete, or failed")
    update_ed_mission_status(mission_id, status)
    return {"status": "updated", "mission_id": mission_id, "new_status": status}


@app.get("/errors")
def get_errors(limit: int = 100):
    """Return the error log — most recent first. Sourced from DB for persistence."""
    try:
        errors = get_error_log(limit=limit)
    except Exception:
        errors = list(reversed(_runtime["error_log"]))[:limit]
    return {"errors": errors, "total": _runtime["errors"]}


@app.delete("/errors")
def clear_errors():
    """Clear all errors from both the in-memory log and the DB."""
    _runtime["error_log"].clear()
    _runtime["errors"] = 0
    try:
        clear_error_log()
    except Exception:
        pass
    return {"status": "cleared"}


@app.get("/memories/categories")
def list_categories():
    return {"categories": ["general", "elite_dangerous", "person", "place", "preference", "task"]}
