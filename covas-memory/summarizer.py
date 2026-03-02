import httpx
import json
import re
import os
from typing import List
from models import MemoryRecord, EntityRecord, EDMissionRecord

OLLAMA_URL  = os.getenv("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "phi3:mini")

# ── Category sanitizer ────────────────────────────────────────────────────────

VALID_CATEGORIES = {"general", "elite_dangerous", "person", "place", "preference", "task"}

def _sanitize_category(value: str) -> str:
    """
    Guard against the model returning pipe-separated values like 'general|elite_dangerous'.
    Splits on pipe, comma, slash, or space and returns the first valid category found.
    Falls back to 'general' if nothing matches.
    """
    if not value:
        return "general"
    for token in re.split(r"[|,/ ]+", value.strip().lower()):
        token = token.strip()
        if token in VALID_CATEGORIES:
            return token
    return "general"

# ── Prompts ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a memory extraction assistant for COVAS, an AI co-pilot system used in the space game Elite Dangerous.
Your job is to read a session transcript and extract structured memory records.

You must respond ONLY with valid JSON — no preamble, no explanation, no markdown fences.

Extract all meaningful information into one or more memory records. Each record covers a distinct topic or event.
For Elite Dangerous content, extract mission details as precisely as possible.

IMPORTANT: The "category" field must be exactly ONE value chosen from this list:
  general, elite_dangerous, person, place, preference, task
Do NOT combine categories. Do NOT use pipe characters or slashes. Pick the single best fit.

JSON schema you must return:
{
  "memories": [
    {
      "category": "elite_dangerous",
      "topic": "short label (max 8 words)",
      "summary": "clear concise summary (2-4 sentences max)",
      "emotional_tone": "neutral|positive|negative|tense|excited|frustrated",
      "entities": [
        {"name": "...", "entity_type": "person|place|faction|ship|other", "context": "brief context"}
      ],
      "ed_mission": null
    }
  ]
}

For ed_mission field, only populate if the memory involves an Elite Dangerous mission. Schema:
{
  "mission_id": "if mentioned",
  "mission_type": "Delivery|Assassination|Massacre|Mining|Courier|Rescue|other",
  "giver": "NPC or station name",
  "target": "target name or faction",
  "origin_system": "...",
  "origin_station": "...",
  "destination_system": "...",
  "destination_station": "...",
  "reward": "credit amount if mentioned",
  "status": "active|complete|failed",
  "notes": "anything else relevant"
}
"""

def _call_ollama(prompt: str) -> str:
    """Send prompt to Ollama and return the raw response text."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "system": SYSTEM_PROMPT,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 2048
        }
    }
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        resp.raise_for_status()
        return resp.json()["response"]


def extract_memories(session_id: str, raw_text: str) -> List[MemoryRecord]:
    """Main entry point — takes raw session text, returns list of MemoryRecord."""

    prompt = f"""Session transcript to process:

---
{raw_text}
---

Extract all memories from this transcript as described. Return only JSON."""

    raw_response = _call_ollama(prompt)

    # Clean up in case model adds markdown fences despite instructions
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Ollama returned invalid JSON: {e}\nRaw: {raw_response[:500]}")

    records = []
    for mem in parsed.get("memories", []):
        entities = [EntityRecord(**e) for e in mem.get("entities", [])]
        ed_mission = None
        if mem.get("ed_mission"):
            try:
                ed_mission = EDMissionRecord(**mem["ed_mission"])
            except Exception:
                pass  # Don't fail the whole record over a bad mission parse

        records.append(MemoryRecord(
            session_id=session_id,
            category=_sanitize_category(mem.get("category", "general")),
            topic=mem.get("topic", "Untitled"),
            summary=mem.get("summary", ""),
            emotional_tone=mem.get("emotional_tone"),
            entities=entities,
            ed_mission=ed_mission
        ))

    return records
