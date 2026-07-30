# How It Works

## 1. Auto-Archive: `auto_archive.py`

This script reads exported Hermes sessions and stores them in ChromaDB.

### Input

```powershell
hermes sessions export --format jsonl D:/agent/hermes_sessions.jsonl
```

This creates a JSONL file where each line is a session object:

```json
{
  "id": "mrt9amjpt4gbvz",
  "model": "step-3.7-flash",
  "started_at": 1784553898.0869305,
  "messages": [
    {"role": "user", "content": "你好", "timestamp": 1784553898.089},
    {"role": "assistant", "content": "你好！有什么可以帮你的？", "timestamp": 1784553900.123},
    ...
  ]
}
```

### Processing

```python
# 1. Parse JSONL
sessions = parse_jsonl("D:/agent/hermes_sessions.jsonl")

# 2. Extract conversations
for session in sessions:
    convos = extract_convos(session)
    # Skips: tool messages, empty content
    # Merges: consecutive same-role messages

# 3. Deduplicate
seen_hashes = set()
unique_convos = []
for c in convos:
    h = md5(c["content"])
    if h not in seen_hashes:
        seen_hashes.add(h)
        unique_convos.append(c)

# 4. Store in ChromaDB
collection.add(
    documents=[c["content"] for c in unique_convos],
    metadatas=[{
        "timestamp": ts,
        "role": c["role"],
        "session_id": c["session_id"],
        "hash": h
    } for c in unique_convos],
    ids=[f"c_{c['session_id'][:12]}_{h}" for c in unique_convos]
)
```

### Why MD5 Hash as ID?

ChromaDB requires unique IDs. Using the full MD5 hash of content ensures:
- Global uniqueness across all sessions
- Idempotency: re-running the script won't create duplicates
- Fast deduplication: O(1) hash lookup

## 2. Memory Retrieval: Skill Trigger

The skill `hermes-long-term-memory/SKILL.md` defines when to query memory:

```yaml
triggers:
  - User asks about past projects
  - User references previous discussions
  - User asks "remember" or "之前说的"
  - Question could benefit from prior context
```

When triggered, Hermes executes:

```python
import chromadb
from chromadb.utils import embedding_functions
from pathlib import Path

client = chromadb.PersistentClient(path=str(Path.home() / '.hermes' / 'memory_db'))
collection = client.get_collection(
    name='hermes_conversations',
    embedding_function=embedding_functions.DefaultEmbeddingFunction()
)

results = collection.query(
    query_texts=[user_question],
    n_results=5
)

# Filter by relevance
for i, doc in enumerate(results['documents'][0]):
    relevance = 1 - results['distances'][0][i]
    if relevance > 0.3:
        # Use this memory
        pass
```

## 3. Memory Injection: Natural Language Weaving

Retrieved memories are injected into the system prompt:

```python
memory_context = "\n## 相关记忆\n"
for i, mem in enumerate(memories, 1):
    memory_context += f"{i}. [{mem['timestamp']}] {mem['content']}\n"

system_prompt = base_system_prompt + memory_context
```

**Critical rule**: The model is instructed to weave memories naturally, NOT to say "According to my memory...".

Example:
- ❌ Bad: "According to my memory from 2026-07-26, you have a Raspberry Pi..."
- ✅ Good: "你之前提到有个 Raspberry Pi 作为 Hermes 服务器..."

## 4. Cron Automation

Hermes cron runs `auto_archive.py` every 6 hours:

```powershell
hermes cron create \
  --name "自动存档对话到记忆库" \
  --script "scripts/auto_archive.py" \
  --no-agent \
  --deliver local \
  "every 6h" \
  "自动将 Hermes 对话存档到 ChromaDB 记忆库"
```

**Mode**: `--no-agent` means the script runs directly without LLM involvement. Its stdout is delivered to the local log.

**Durability**: Cron jobs survive Hermes restarts. The Gateway ticker (every 8s) checks for due jobs.

## 5. State Management

`archive_state.json` tracks what's been archived:

```json
{
  "archived_hashes": [
    "5a104a3e8c...",
    "b3c201d4f9..."
  ],
  "last_run": "2026-07-31T10:30:00"
}
```

This enables:
- **Idempotency**: Re-running won't duplicate entries
- **Incremental archival**: Only new conversations are processed
- **Auditability**: You can see when the last archive ran

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│ 1. User Conversation                                        │
│    User: "What is OpenOPC?"                                 │
│    Assistant: "OpenOPC is an AI company system..."          │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. Cron Trigger (every 6h)                                  │
│    hermes cron tick → auto_archive.py                       │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. Export                                                   │
│    hermes sessions export → hermes_sessions.jsonl           │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. Parse & Extract                                          │
│    - Parse JSONL                                            │
│    - Extract user/assistant turns                           │
│    - Skip tool messages, empty content                      │
│    - Merge consecutive same-role messages                   │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ 5. Deduplicate                                              │
│    - MD5 hash of content                                    │
│    - Check against archive_state.json                       │
│    - Skip if already archived                               │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ 6. Embed & Store                                            │
│    - Local embedding (all-MiniLM-L6-v2)                     │
│    - Batch insert into ChromaDB                             │
│    - Update archive_state.json                              │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ 7. Future Retrieval                                         │
│    User: "How do I use OpenOPC?"                            │
│    Skill triggers → ChromaDB query → relevance filter       │
│    → Inject into prompt → Natural answer                    │
└─────────────────────────────────────────────────────────────┘
```

## Key Implementation Details

### Deduplication Strategy

```python
def content_hash(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def is_archived(conv, state):
    h = content_hash(conv["content"])
    return h in state.get("archived_hashes", [])
```

**Why MD5?**
- Fast: O(n) for single hash
- Deterministic: same content = same hash
- Collision-resistant for personal use (< 1M memories)
- 32-char hex string, easy to store/compare

### Batch Insert

```python
BATCH_SIZE = 50

for i in range(0, len(convos), BATCH_SIZE):
    batch = convos[i:i + BATCH_SIZE]
    collection.add(
        documents=[c["content"] for c in batch],
        metadatas=[...],
        ids=[f"c_{c['session_id'][:12]}_{content_hash(c['content'])}" for c in batch]
    )
```

**Why batch?**
- ChromaDB performs better with batch inserts
- Memory efficient for large exports
- Allows progress tracking

### Relevance Filtering

```python
relevance = 1 - distance  # ChromaDB returns cosine distance
if relevance > 0.3:  # 30% threshold
    # Use this memory
```

**Why 30%?**
- Balances recall vs precision
- Too low: too many irrelevant memories
- Too high: misses useful context
- Adjust based on your needs

## Performance

| Metric | Current | Notes |
|---|---|---|
| Embedding speed | ~100 texts/sec | Local ONNX runtime |
| Query speed | <100ms | For < 10k memories |
| Archive speed | ~500 convos/sec | Batch insert |
| Storage per memory | ~200-500 bytes | Text + metadata + vector |
| Memory DB size | ~50MB | For 438 memories |

## Failure Modes

### What happens if...

**Cron job fails?**
- Next tick will retry
- No data loss (JSONL export persists)
- Check `hermes cron runs` for errors

**ChromaDB is locked?**
- Script catches exception, logs error
- Skips that batch, continues with next
- Next cron tick will retry

**Export file missing?**
- Script exits with error message
- No partial writes to ChromaDB
- User must re-run `hermes sessions export`

**Memory query returns nothing?**
- Skill continues without memory context
- No error shown to user
- Next archive will capture new conversations
