# COVAS Memory Service

> **part of the [COVAS Local AI Project](https://github.com/Miglet15/covas-local-ai-project/tree/dev)**  
> the memory backend — runs on a home server, handles transcript ingestion, LLM extraction, and SQLite persistence so your ship's computer actually remembers who you are session to session

---

## what this is

this is the companion microservice to [covas-local-ai-project](https://github.com/Miglet15/covas-local-ai-project/tree/dev). it doesn't do much on its own — it exists to offload memory processing off the gaming PC so inference and game I/O don't have to compete for resources.

**what it does:**

- receives session transcripts from the main COVAS server over HTTP
- feeds them through a small local LLM (Phi-3 Mini via Ollama) to extract structured memories
- stores everything in SQLite — memories, entities, active ED missions
- serves those memories back on request so the main server can inject them into context

the main server's `covas_memory_client.py` handles all communication with this service automatically. you mostly just need this running and reachable.

---

## architecture

```
gaming PC (covas-local-ai-project)
    │
    │  HTTP POST /ingest  →  sends session transcripts every 5 min or on session end
    │  HTTP GET /memories ←  queries stored memories for context injection
    │
    ▼
home server (this repo — Docker)
    ├── covas-memory  :8100
    │     ├── FastAPI (main.py)
    │     ├── Summarizer — Phi-3 Mini via Ollama
    │     ├── SQLite (/data/covas_memory.db)
    │     └── status dashboard (/)
    │
    └── ollama  :11434
          └── phi3:mini
```

**memory flow:**

1. gaming PC runs COVAS; `covas_memory_client.py` buffers conversation turns
2. every 5 minutes (or at session end) the buffer is sent to `/ingest`
3. `summarizer.py` feeds the transcript to Phi-3 Mini via Ollama
4. the LLM returns structured JSON — categories, topics, entities, ED missions
5. records are stored in SQLite and available for recall on the next query

---

## repository structure

```
covas-apollo-project/
├── docker-compose.yml
├── .gitignore
├── README.md
└── covas-memory/
    ├── Dockerfile
    ├── requirements.txt
    ├── main.py             # FastAPI app, all HTTP endpoints
    ├── summarizer.py       # Ollama LLM call + JSON parsing
    ├── storage.py          # SQLite schema + CRUD helpers
    ├── models.py           # Pydantic models (SessionPayload, MemoryRecord, …)
    └── covas_memory_client.py  # copy this to the gaming PC / COVAS project
```

---

## setup

### prerequisites

- Docker + Docker Compose on the home server
- home server reachable from the gaming PC via LAN or [Tailscale](https://tailscale.com/)
- Ollama running on the home server

### 1. pull the model

```bash
docker exec -it ollama ollama pull phi3:mini
```

if Ollama isn't running yet, start it first (step 3), then come back and pull.

### 2. place the files

```
/your/path/covas/
├── docker-compose.yml
└── covas-memory/
    ├── Dockerfile
    ├── requirements.txt
    ├── main.py
    ├── summarizer.py
    ├── storage.py
    └── models.py
```

### 3. build and start

```bash
git clone https://github.com/Miglet15/covas-apollo-project.git
cd covas-apollo-project
docker compose up -d --build
```

### 4. verify

```bash
curl http://localhost:8100/health
# {"status":"ok","time":"..."}
```

status dashboard at `http://your-server-ip:8100/` — auto-refreshes every 15 seconds.

---

## gaming PC setup

### 1. copy `covas_memory_client.py` into your covas-local-ai-project directory

### 2. set the service address

```python
# covas_memory_client.py — top of file
MEMORY_SERVICE_URL = "http://192.168.x.x:8100"   # LAN
# or via Tailscale:
# MEMORY_SERVICE_URL = "http://100.x.x.x:8100"
```

### 3. integrate into the main server

```python
from covas_memory_client import memory

# session lifecycle
memory.new_session()                          # call when COVAS starts

# during conversation — feed every turn
memory.append(f"User: {user_input}")
memory.append(f"COVAS: {covas_response}")

# end of session
memory.send_session_end(
    ed_context={"system": "Sol", "station": "Jameson Memorial"}
)

# query memories at any time
recent   = memory.get_recent_memories(limit=5)
missions = memory.get_active_missions()
results  = memory.search_memories("wing mission Robigo")
```

the client automatically sends an interval snapshot every 5 minutes so nothing is lost if a session ends unexpectedly.

---

## api reference

| method | endpoint | description |
|--------|----------|-------------|
| `GET` | `/` | HTML status dashboard (auto-refresh 15 s) |
| `GET` | `/health` | liveness check — `{"status":"ok"}` |
| `GET` | `/stats` | JSON stats |
| `POST` | `/ingest` | receive a session payload from the gaming PC |
| `POST` | `/memories/query` | search / filter memories |
| `GET` | `/memories/recent?limit=N` | N most recent memories |
| `GET` | `/memories/session/{id}` | all memories for a specific session |
| `GET` | `/memories/categories` | list of valid category strings |
| `GET` | `/ed/missions/active` | active Elite Dangerous missions |
| `PATCH` | `/ed/missions/{id}/status` | update mission status (active/complete/failed) |
| `GET` | `/errors?limit=N` | recent processing errors |
| `DELETE` | `/errors` | clear error log |

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

`trigger` must be `"session_end"` or `"interval"`. ingestion is async — the endpoint returns `202 Accepted` immediately.

---

## memory categories

| category | description |
|----------|-------------|
| `general` | general conversation and notes |
| `elite_dangerous` | in-game events, exploration, combat, trading |
| `person` | named individuals — NPCs or real people |
| `place` | systems, stations, and notable locations |
| `preference` | user preferences and remembered settings |
| `task` | tasks, reminders, or follow-ups |

---

## database

SQLite stored at `/data/covas_memory.db` inside the container, backed by a named Docker volume (`memory_data`) so it survives restarts and rebuilds.

**inspect directly on the server:**

```bash
# list tables
docker exec -it covas-memory sqlite3 /data/covas_memory.db ".tables"

# browse recent memories
docker exec -it covas-memory sqlite3 /data/covas_memory.db \
  "SELECT created_at, category, topic FROM memories ORDER BY created_at DESC LIMIT 10;"

# active ED missions
docker exec -it covas-memory sqlite3 /data/covas_memory.db \
  "SELECT mission_type, giver, destination_system, reward FROM ed_missions WHERE status='active';"
```

**schema tables:**

| table | purpose |
|-------|---------|
| `sessions` | one row per gaming PC session with ED context |
| `memories` | core memory records (category, topic, summary) |
| `entities` | named persons, places, factions, ships |
| `ed_missions` | Elite Dangerous mission tracking |
| `error_log` | LLM processing errors, capped at 500 rows |

---

## configuration

all config via environment variables in `docker-compose.yml`:

| variable | default | description |
|----------|---------|-------------|
| `OLLAMA_URL` | `http://ollama:11434` | Ollama API base URL |
| `OLLAMA_MODEL` | `phi3:mini` | model to use for extraction |
| `DB_PATH` | `/data/covas_memory.db` | SQLite database path |

to switch to a larger model:

```yaml
environment:
  OLLAMA_MODEL: "llama3:8b"
```

---

## useful commands

```bash
# view live logs
docker compose logs -f covas-memory

# restart after code changes
docker compose up -d --build covas-memory

# pull a new Ollama model
docker exec -it ollama ollama pull phi3:mini

# backup the database
docker cp covas-memory:/data/covas_memory.db ./backup_$(date +%F).db

# full teardown (keeps volumes)
docker compose down

# full teardown including volumes (⚠ deletes all memories)
docker compose down -v
```

---

## troubleshooting

| symptom | likely cause | fix |
|---------|-------------|-----|
| `/health` times out | container not running | `docker compose up -d` |
| Ollama shown as unreachable on status page | model not pulled | `docker exec -it ollama ollama pull phi3:mini` |
| `"Ollama returned invalid JSON"` errors | LLM hallucinating non-JSON | normal for small models; check `/errors` — usually recovers |
| memories not appearing after ingest | async processing lag | wait ~30 s; check `docker compose logs -f covas-memory` |
| gaming PC can't reach this service | firewall / IP mismatch | confirm `MEMORY_SERVICE_URL` in `covas_memory_client.py`; test with `curl http://server-ip:8100/health` from the gaming PC |

---

## related

- **[covas-local-ai-project](https://github.com/Miglet15/covas-local-ai-project/tree/dev)** — the main server this talks to. the AI bridge, lore injection, COVAS:NEXT integration, status page — all lives there
- [COVAS:NEXT](https://github.com/RatherRude/Elite-Dangerous-AI-Integration) — the actual Elite Dangerous integration that started all this
- [Ollama](https://ollama.com/) — local model runtime used for memory extraction
- [Elite Dangerous](https://www.elitedangerous.com/) — the game

---

*Elite Dangerous and related assets are property of Frontier Developments.*
