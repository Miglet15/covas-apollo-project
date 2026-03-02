# COVAS Memory Service — Apollo

> **Part of the [COVAS Local AI Project](https://github.com/Miglet15/covas-local-ai-project/tree/dev)**
>
> This repository contains the **memory microservice** that runs on **Apollo** (home server / Ubuntu + Docker).
> It receives session transcripts from the Server running [COVAS](https://github.com/Miglet15/covas-local-ai-project/tree/dev), extracts structured memories using a local LLM via Ollama, and persists them in SQLite for future recall.

---

## Architecture Overview

```
┌─────────────────────────────┐          LAN / Tailscale
│         UNIT-01             │ ──────── HTTP POST /ingest ──────► ┌─────────────────────────────┐
│  (Gaming PC — COVAS AI)     │                                    │         APOLLO              │
│                             │ ◄─────── HTTP GET /memories ─────  │  (Home Server — Docker)     │
│  covas_memory_client.py     │                                    │                             │
│  • Buffers session logs     │                                    │  covas-memory  :8100        │
│  • Sends on interval/end    │                                    │  ├── FastAPI (main.py)      │
│  • Queries memories/missions│                                    │  ├── Summarizer (Phi-3 Mini)│
└─────────────────────────────┘                                    │  ├── SQLite  (/data/*.db)   │
                                                                   │  └── Status page (/)        │
                                                                   │                             │
                                                                   │  ollama  :11434             │
                                                                   │  └── phi3:mini              │
                                                                   └─────────────────────────────┘
```

**Memory flow:**

1. UNIT-01 runs COVAS; `covas_memory_client.py` buffers conversation turns.
2. Every 5 minutes (or at session end) the buffer is sent to Apollo's `/ingest` endpoint.
3. Apollo's `summarizer.py` feeds the transcript to Phi-3 Mini via Ollama.
4. The LLM returns structured JSON — categories, topics, entities, ED missions.
5. Records are stored in SQLite. UNIT-01 can query them at any time for context recall.

---

## Repository Structure

```
covas-memory/           ← Apollo-side service (this repo)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── main.py             ← FastAPI app, all HTTP endpoints
├── summarizer.py       ← Ollama LLM call + JSON parsing
├── storage.py          ← SQLite schema + CRUD helpers
├── models.py           ← Pydantic models (SessionPayload, MemoryRecord, …)
└── covas_memory_client.py  ← Runs on UNIT-01; import into COVAS
```

---

## Apollo Setup

### Prerequisites

- Docker + Docker Compose installed on Apollo
- Apollo reachable from UNIT-01 via LAN or [Tailscale](https://tailscale.com/)

### 1. Pull the Phi-3 Mini model

```bash
# Start Ollama first (or run after step 3 if starting fresh)
docker exec -it ollama ollama pull phi3:mini
```

### 2. Place the files

```
/home/user/covas-apollo-project/
├── docker-compose.yml
└── covas-memory/
    ├── Dockerfile
    ├── requirements.txt
    ├── main.py
    ├── summarizer.py
    ├── storage.py
    └── models.py
```

### 3. Build and start

```bash
cd /home/user/covas-apollo-project/
docker compose up -d --build
```

### 4. Verify

```bash
curl http://localhost:8100/health
# {"status":"ok","time":"2025-..."}
```

Open `http://apollo-ip:8100/` in a browser for the full status dashboard (auto-refreshes every 15 s).

---

## UNIT-01 Setup

### 1. Copy `covas_memory_client.py` into your COVAS project

### 2. Set Apollo's address

```python
# covas_memory_client.py — top of file
MEMORY_SERVICE_URL = "http://server-ip:8100"   # LAN
# or Tailscale:
# MEMORY_SERVICE_URL = "http://tailscale-ip:8100"
```

### 3. Integrate into COVAS

```python
from covas_memory_client import memory

# ── Session lifecycle ──────────────────────────────────────────────────────────
memory.new_session()                          # Call when COVAS starts

# ── During conversation (feed every turn) ─────────────────────────────────────
memory.append(f"User: {user_input}")
memory.append(f"COVAS: {covas_response}")

# ── End of session ────────────────────────────────────────────────────────────
memory.send_session_end(
    ed_context={"system": "Sol", "station": "Jameson Memorial"}
)

# ── Query memories at any time ────────────────────────────────────────────────
recent   = memory.get_recent_memories(limit=5)
missions = memory.get_active_missions()
results  = memory.search_memories("wing mission Robigo")
```

The client automatically sends an interval snapshot every **5 minutes** so no data is lost if a session is interrupted.

---

## API Reference

| Method   | Endpoint                        | Description                                 |
|----------|---------------------------------|---------------------------------------------|
| `GET`    | `/`                             | HTML status dashboard (auto-refresh 15 s)   |
| `GET`    | `/health`                       | Liveness check — `{"status":"ok"}`          |
| `GET`    | `/stats`                        | JSON stats (for UNIT-01 status polling)     |
| `POST`   | `/ingest`                       | Receive a session payload from UNIT-01      |
| `POST`   | `/memories/query`               | Search / filter memories                    |
| `GET`    | `/memories/recent?limit=N`      | N most recent memories                      |
| `GET`    | `/memories/session/{id}`        | All memories for a specific session         |
| `GET`    | `/memories/categories`          | List of valid category strings              |
| `GET`    | `/ed/missions/active`           | Active Elite Dangerous missions             |
| `PATCH`  | `/ed/missions/{id}/status`      | Update mission status (active/complete/failed) |
| `GET`    | `/errors?limit=N`               | Recent processing errors                    |
| `DELETE` | `/errors`                       | Clear error log                             |

### POST `/ingest` — payload schema

```json
{
  "session_id": "session_20250601_143022_a3f9b2",
  "timestamp":  "2025-06-01T14:30:22Z",
  "raw_text":   "User: what's our current mission...\nCOVAS: ...",
  "trigger":    "session_end",
  "ed_context": {
    "system":  "Robigo",
    "station": "Sirius Atmospherics"
  }
}
```

`trigger` must be `"session_end"` or `"interval"`. Ingestion is handled asynchronously — the endpoint returns `202 Accepted` immediately.

---

## Memory Categories

| Category          | Description                                  |
|-------------------|----------------------------------------------|
| `general`         | General conversation and notes               |
| `elite_dangerous` | In-game events, exploration, combat, trading |
| `person`          | Named individuals — NPCs or real people      |
| `place`           | Systems, stations, and notable locations     |
| `preference`      | User preferences and remembered settings     |
| `task`            | Tasks, reminders, or follow-ups              |

---

## Database

SQLite stored at `/data/covas_memory.db` inside the container, backed by a named Docker volume (`memory_data`) for persistence across restarts and rebuilds.

**Inspect directly on Apollo:**

```bash
# List tables
docker exec -it covas-memory sqlite3 /data/covas_memory.db ".tables"

# Browse recent memories
docker exec -it covas-memory sqlite3 /data/covas_memory.db \
  "SELECT created_at, category, topic FROM memories ORDER BY created_at DESC LIMIT 10;"

# Active ED missions
docker exec -it covas-memory sqlite3 /data/covas_memory.db \
  "SELECT mission_type, giver, destination_system, reward FROM ed_missions WHERE status='active';"
```

**Schema tables:**

| Table        | Purpose                                          |
|--------------|--------------------------------------------------|
| `sessions`   | One row per UNIT-01 session with ED context      |
| `memories`   | Core memory records (category, topic, summary)   |
| `entities`   | Named persons, places, factions, ships           |
| `ed_missions`| Elite Dangerous mission tracking                 |
| `error_log`  | LLM processing errors, capped at 500 rows        |

---

## Configuration

All config is via environment variables in `docker-compose.yml`:

| Variable       | Default                   | Description                       |
|----------------|---------------------------|-----------------------------------|
| `OLLAMA_URL`   | `http://ollama:11434`     | Ollama API base URL               |
| `OLLAMA_MODEL` | `phi3:mini`               | Model to use for extraction       |
| `DB_PATH`      | `/data/covas_memory.db`   | SQLite database path              |

To switch to a larger model (e.g. Llama 3):

```yaml
environment:
  OLLAMA_MODEL: "llama3:8b"
```

---

## Useful Commands

```bash
# View live logs
docker compose logs -f covas-memory

# Restart after code changes
docker compose up -d --build covas-memory

# Force-pull a new Ollama model
docker exec -it ollama ollama pull phi3:mini

# Backup the database
docker cp covas-memory:/data/covas_memory.db ./backup_$(date +%F).db

# Full teardown (keeps volumes)
docker compose down

# Full teardown including volumes (⚠ deletes all memories)
docker compose down -v
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/health` times out | Container not running | `docker compose up -d` |
| Ollama shown as unreachable on status page | Model not pulled | `docker exec -it ollama ollama pull phi3:mini` |
| `"Ollama returned invalid JSON"` errors | LLM hallucinating non-JSON | Normal for small models; check `/errors` — usually recovers |
| Memories not appearing after ingest | Async processing lag | Wait ~30 s; check logs with `docker compose logs -f covas-memory` |
| UNIT-01 can't reach Apollo | Firewall / IP mismatch | Confirm `MEMORY_SERVICE_URL` in `covas_memory_client.py`; test with `curl http://apollo-ip:8100/health` from UNIT-01 |

---

## Related

- **[COVAS Local AI Project](https://github.com/Miglet15/covas-local-ai-project/tree/dev)** — The UNIT-01 side: voice input, LLM orchestration, Elite Dangerous integration, and TTS.
- **[Ollama](https://ollama.com/)** — Local LLM runtime used for memory extraction.
- **[Elite Dangerous](https://www.elitedangerous.com/)** — The space game COVAS is built around.

---

*Apollo · covas-memory v1.0 · Phi-3 Mini via Ollama · SQLite*
# covas-apollo-project
