# shared.knowledge_graph — Level 5 Cross-bot KG

Unified knowledge store with pgvector semantic search. Lives in **resale DB**
(`RESALE_DATABASE_URL`).

## Quick start

```python
from shared.knowledge_graph import KG

# Lookup aliases (hot path — 100ms timeout, falls back to provided hardcoded)
candidates = KG.get_aliases("address opera", category="building_alias",
                            fallback=BUILDING_ALIASES.get("address opera", ["address opera"]))

# Semantic search
hits = KG.search("address residences dubai opera", category="building_alias", k=5)

# Add new knowledge (admin approval flow auto-triggers)
KG.add(
    category="building_alias",
    payload={"key": "address beach resort", "aliases": ["Address Beach Resort"],
             "canonical": "Address Residences Jumeirah Resort & Spa"},
    description="User search pattern 'address beach resort' → canonical Address Jumeirah Resort",
    source_bot="resale-bot",
)
```

## Schema migration

```bash
RESALE_DATABASE_URL=... python -c "from shared.knowledge_graph._db import ensure_schema; ensure_schema()"
```

## Seed (idempotent, auto-approved curated set)

```bash
RESALE_DATABASE_URL=... python -m shared.knowledge_graph.seed
```

## Daily cron — auto-discover from empty queries

Register in your existing scheduler:

```bash
0 4 * * *  cd /app && python -m shared.knowledge_graph.auto_discover
```

## Telegram approval callbacks

The bot that receives admin Telegram callbacks must dispatch `kg:*`:

```python
from shared.knowledge_graph.approval import handle_callback
if callback_data.startswith("kg:"):
    result = handle_callback(callback_data, actor="vadim")
```

## Daily audit section

```python
from shared.knowledge_graph.audit_section import render_section
report += "\n\n" + render_section()
```

## Categories

- `building_alias` — payload `{key, aliases[], canonical}`
- `area_alias`     — payload `{key, aliases[], canonical}`
- `edge_case`      — free payload + `key`
- `workflow_fix`   — `{symptom, fix}`
- `parser_pattern` — PSI rule
- `faq`            — `{question, answer}`
- `response_template` — `{slug, text}`

## Rules

- Embeddings: Gemini text-embedding-004 (free 1500/day). Fallback: hash-based
  pseudo-embedding (search degrades but never crashes).
- Hot-path safety: `KG.search`/`KG.get_aliases` use `statement_timeout=100ms`
  and silently return `[]`/`fallback` on timeout.
- Cache: 5min in-process per (category, key) tuple.
- Auto-approve only seeds & curated payloads — everything from bots goes
  through Telegram approval.
