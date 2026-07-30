# Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Hermes Agent                              │
│                                                             │
│  ┌──────────────┐    ┌─────────────────────────────┐       │
│  │ Built-in     │    │  External Memory Skill       │       │
│  │ Memory       │    │  (hermes-long-term-memory)   │       │
│  │ MEMORY.md    │    │                              │       │
│  │ USER.md      │    │  - Triggers on context need  │       │
│  └──────────────┘    │  - Queries ChromaDB          │       │
│                      │  - Injects results into reply│       │
│                      └──────────┬──────────────────┘       │
│                                 │                           │
│  ┌──────────────────────────────▼───────────────────┐       │
│  │         ChromaDB Persistent Storage              │       │
│  │  Path: ~/.hermes/memory_db/                      │       │
│  │  Collection: hermes_conversations                 │       │
│  │  Embedding: all-MiniLM-L6-v2 (local, 384-dim)    │       │
│  └──────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
        ▲ 自动导出 + 存档                    ▲ 按需检索
    Hermes cron (6h)                     execute_code
    auto_archive.py                      ChromaDB query
```

## Data Flow

### 1. Auto-Archive Pipeline (Write Path)

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ Hermes Cron  │────▶│ Export       │────▶│ Parse        │
│ (every 6h)   │     │ JSONL        │     │ JSONL        │
└──────────────┘     └──────────────┘     └──────┬───────┘
                                                  │
                    ┌──────────────┐              │
                    │ Extract      │◀─────────────┘
                    │ Conversations│
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │ Deduplicate  │
                    │ (SHA256 hash)   │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │ Embed &      │
                    │ Store        │
                    │ (ChromaDB)   │
                    └──────────────┘
```

**Script**: `auto_archive.py`

**Input**: `~/.hermes/exports/hermes_sessions*.jsonl` (exported by `hermes sessions export`)

**Output**: ChromaDB collection `hermes_conversations`

**Steps**:
1. Read exported JSONL files
2. Parse each line as a session object
3. Extract user/assistant conversations (skip tool messages, empty content)
4. Merge consecutive same-role messages
5. Deduplicate by full SHA256 hash of content
6. Generate unique IDs: `c_{session_id[:12]}_{sha256_hash}`
7. Batch insert into ChromaDB with metadata (timestamp, role, session_id)
8. Update archive state only for successfully written batches; failed batches retain hashes for retry

### 2. Memory Retrieval Pipeline (Read Path)

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ User Query   │────▶│ Skill        │────▶│ Execute      │
│              │     │ Detects      │     │ Python Code  │
│              │     │ Context Need │     │ (ChromaDB)   │
└──────────────┘     └──────────────┘     └──────┬───────┘
                                                  │
                    ┌──────────────┐              │
                    │ Format       │◀─────────────┘
                    │ Results      │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │ Inject into  │
                    │ System Prompt│
                    │ (自然融入)    │
                    └──────────────┘
```

**Trigger**: Skill detects context need (references to past projects, "之前说的", etc.)

**Query**: `collection.query(query_texts=[user_message], n_results=5)`

**Filter**: Only inject results above `relevance_threshold` (default `0.3`); lower-similarity memories are discarded to avoid polluting the prompt.

**Injection**: Weave memories naturally into answer (no "According to my memory...")

## Component Details

### ChromaDB Storage

- **Path**: `~/.hermes/memory_db/`
- **Collection**: `hermes_conversations`
- **Embedding**: `DefaultEmbeddingFunction` (all-MiniLM-L6-v2, 384-dim, local)
- **Distance metric**: Cosine

### Document Schema

```json
{
  "id": "c_mrt9amjpt4gb_5a104...",
  "document": "用户: 我应该学什么编程语言？",
  "metadata": {
    "timestamp": "2026-07-31 10:05:00",
    "role": "user",
    "session_id": "mrt9amjpt4gbvz",
    "hash": "5a104..."
  }
}
```

### Archive State

```json
{
  "archived_hashes": [
    "5a104...",
    "b3c201..."
  ],
  "last_run": "2026-07-31T10:30:00"
}
```

### Cron Schedule

- **Job ID**: `e21800feb2ae`
- **Schedule**: every 6 hours
- **Script**: `~/.hermes/scripts/auto_archive.py`
- **Mode**: `--no-agent` (stdout delivered)
- **Trigger**: Hermes Gateway ticker

## Memory Lifecycle

```
Creation
  │
  ├─ User asks question
  ├─ Assistant replies
  │
  ▼
Archival (within 6 hours)
  │
  ├─ Cron triggers auto_archive.py
  ├─ Conversation exported to JSONL
  ├─ Parsed and deduplicated
  ├─ Embedded and stored in ChromaDB
  │
  ▼
Retrieval (when needed)
  │
  ├─ User asks related question
  ├─ Skill detects context need
  ├─ ChromaDB query executed
  ├─ Relevant memories injected
  │
  ▼
Response (with memory context)
  │
  └─ User receives natural answer
```

## Design Decisions

### Why ChromaDB?

- Zero-config persistent vector store
- Local-first, no cloud dependency
- Built-in embedding function support
- Good enough for personal use (< 100k memories)

### Why Local Embedding?

- No API key required
- No network latency
- No cost
- Privacy: text never leaves the machine

**Tradeoff**: all-MiniLM-L6-v2 is English-trained. Chinese semantic search is weaker than English.

### Why Cron + Export?

- Hermes doesn't expose real-time session streaming
- `sessions export` is the only reliable way to get full conversation history
- Cron provides reliable, durable scheduling

**Tradeoff**: 6-hour delay between conversation and archival.

### Why SHA256 Hash Deduplication?

- Fast, deterministic
- Guaranteed uniqueness for identical content
- Survives across sessions and exports

## Scaling Considerations

Current design supports:
- **~10,000 conversations** before batching becomes noticeable
- **~100,000 messages** before ChromaDB needs optimization
- **6-hour archival delay** acceptable for personal use

For larger scale:
- Consider HNSW index optimization
- Implement incremental export (only new sessions)
- Add session-level TTL for old conversations
