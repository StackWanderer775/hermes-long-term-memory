---
name: hermes-long-term-memory
description: >
  Add persistent long-term memory to Hermes using ChromaDB + local embedding,
  with automatic session archiving via Hermes cron. Use when the user asks to
  "give Hermes memory", "archive conversations", "build an external memory system",
  "let Hermes remember past chats", or "integrate memory with LLM".
  Covers setup, export, auto-archive, chat integration, and provider pitfalls.
version: 1.0.0
---

# Hermes Long-Term Memory (ChromaDB + Auto Archive)

Turn Hermes into an agent with durable memory: conversations are exported,
deduplicated, embedded locally, and stored in ChromaDB. A Hermes cron job
keeps the memory store current without manual intervention.

## Architecture

```
Hermes conversation
        ↓
hermes sessions export  →  JSONL
        ↓
auto_archive.py  →  ChromaDB (~/.hermes/memory_db)
        ↓
memory_ai.py  →  retrieve memories → build prompt → call LLM
```

- **Storage:** ChromaDB `PersistentClient` at `~/.hermes/memory_db`
- **Embedding:** local `DefaultEmbeddingFunction` only (`all-MiniLM-L6-v2`, 384-dim). Do not use `OpenAIEmbeddingFunction`; BigModel does not expose a compatible `/v1/embeddings` for `text-embedding-3-small`, and local embedding is free and offline-capable.
- **LLM:** any OpenAI-compatible chat API (BigModel, Agnes, etc.)
- **Schedule:** Hermes `cron create` with `--script auto_archive.py --no-agent`
- **Collection name:** `hermes_conversations` — this must match between `auto_archive.py` and `memory_ai.py`.
- **Deduplication:** SHA256 content hash, with failed-batch retry and atomic state updates.
- **Concurrency:** `archive_state.json` updates use file locking via `portalocker` when available.

## Prerequisites

- Hermes 0.19+ on Windows
- Python deps: `chromadb`, `openai`
- Hermes CLI path:
  `C:\Users\<user>\.hermes-web-ui\desktop-runtime\hermes\<ver>\win-x64\python\Scripts\hermes.CMD`

## Step 1 — Install dependencies

```powershell
pip install chromadb openai
```

## Step 2 — Create the memory+LLM chat script

Create `D:\agent\memory_ai.py`. It should:

1. Load chat config from `~/.hermes/chat_config.json`
2. Load memory config from `~/.hermes/memory_config.json`
3. Initialize ChromaDB with `DefaultEmbeddingFunction` (**local embedding only**)
4. On each user turn:
   - Search memories with `collection.query(query_texts=[user_message], n_results=3)`
   - Prepend matched memories into the system prompt
   - Call the chat LLM
   - Auto-save both user and assistant turns as memories

Key implementation notes:
- **Embedding backend**: use local `DefaultEmbeddingFunction` only. Do **not** use
  `OpenAIEmbeddingFunction`; BigModel does not expose `/v1/embeddings` for
  `text-embedding-3-small`, and the local ONNX model is free and offline-capable.
- Collection name **must** be `hermes_conversations` to match `auto_archive.py`.
- Use `openai.OpenAI(api_key=..., base_url=...)` for BigModel-compatible chat APIs.
- `build_prompt_with_memories()` should inject a `## 相关记忆` block.
- Guard all `collection` calls with `if collection is None:` and fail gracefully.

## Step 3 — Create the auto-archive script

Create `D:\agent\auto_archive.py`. It should:

1. Read exported Hermes session files from a configurable directory, defaulting to `Path.home() / ".hermes" / "exports"` or `D:/agent` on Windows
2. Parse each line as a session JSON object
3. Extract user/assistant turns, skip tool messages and empty content
4. Deduplicate by **full MD5 hash** of content across all sessions
5. Batch-insert into ChromaDB collection `hermes_conversations`
6. Persist archive state to a configurable path, defaulting to `D:/agent/archive_state.json` on Windows or `~/.hermes/archive_state.json`

Critical implementation rules:
- **Paths must be configurable.** Read `EXPORT_DIR`, `STATE_FILE`, and `MEMORY_DIR` from environment variables first; fall back to cross-platform defaults. Do not hardcode `D:/agent` only.
- **IDs must be globally unique.** Use the full content hash, e.g.
  `c_{session_id[:12]}_{full_md5_hash}`. Truncated hashes collide across sessions.
- **State file must be JSON-serializable.** Store hashes in a `list`, not a `set`.
- **Batch inserts fail on duplicate IDs.** Deduplicate before calling `collection.add()`.
- **Guard collection usage.** If `init_memory()` returns `None`, stop archiving and print a clear error instead of crashing later.

## Step 4 — Export sessions manually (one-time)

```powershell
# Export all sessions to the configured export directory
& "C:\Users\<user>\.hermes-web-ui\desktop-runtime\hermes\<ver>\win-x64\python\Scripts\hermes.CMD" `
  sessions export --format jsonl D:/agent/hermes_sessions.jsonl
```

Re-run this command whenever the user wants to backfill new conversations.

## Step 5 — Run the first archive

```powershell
python D:\agent\auto_archive.py
```

Verify:
```powershell
python D:\agent\auto_archive.py --stats
```

## Step 6 — Schedule automatic archiving

Use Hermes cron to run the archive script on a schedule:

```powershell
# Create cron job (every 6 hours)
& "C:\Users\<user>\.hermes-web-ui\desktop-runtime\hermes\<ver>\win-x64\python\Scripts\hermes.CMD" `
  cron create `
  --name "自动存档对话到记忆库" `
  --script "scripts/auto_archive.py" `
  --no-agent `
  --deliver local `
  "every 6h" `
  "自动将 Hermes 对话存档到 ChromaDB 记忆库"
```

Notes:
- `--no-agent` runs the script directly; stdout is delivered to `local`.
- The script path is relative to `~/.hermes/scripts/`.
- Verify with: `hermes cron list` and `hermes cron status`.

## Step 7 — Chat with memory

```powershell
# Interactive memory-aware chat
python D:\agent\memory_ai.py chat

# One-shot question
python D:\agent\memory_ai.py ask "用户之前提到过什么项目？"

# Manual memory
python D:\agent\memory_ai.py remember "用户下周一要交项目报告"

# List all memories
python D:\agent\memory_ai.py list

# View config
python D:\agent\memory_ai.py config
```

## Provider quirks

| Provider | Embedding support | Notes |
|----------|-------------------|-------|
| BigModel (`open.bigmodel.cn`) | ❌ No `/v1/embeddings` | Use local `DefaultEmbeddingFunction` instead. |
| OpenAI | ✅ `text-embedding-3-small` | Works if configured; local is faster and free. |
| Agnes AI | ❓ Untested | Prefer local embedding unless user explicitly wants API. |
| Groq | ❌ No embedding endpoint | Use local embedding. |

## Troubleshooting

- **"Expected IDs to be unique"** → You truncated the hash. Use the full MD5.
- **"Object of type set is not JSON serializable"** → State file uses `set`; switch to `list`.
- **BigModel embedding returns 400** → BigModel does not expose OpenAI-compatible
  `/v1/embeddings`. Switch to ChromaDB's local `DefaultEmbeddingFunction`.
- **Memory search returns irrelevant results** → The local model (`all-MiniLM-L6-v2`)
  is English-trained. Chinese queries may have lower relevance. Consider adding
  a Chinese embedding provider if precision matters.
- **Cron job not firing** → Check `hermes cron status`. Gateway must be running.
- **No new sessions exported** → Re-run `hermes sessions export`; Hermes only exports
  on demand; it does not watch for new sessions automatically.
- **Collection name mismatch** → `memory_ai.py` and `auto_archive.py` must both use
  `hermes_conversations`. If one uses `hermes_memory`, retrieval will appear empty
  even though archiving succeeded.
- **Paths hardcoded to D:/agent** → Set `EXPORT_DIR` and `MEMORY_DIR` environment
  variables, or edit the defaults to a cross-platform location like
  `Path.home() / ".hermes"`.
- **JSON parse errors are silent** → `parse_jsonl()` now prints the line number and
  error for bad JSON lines. Re-export the session if the source file is corrupted.

## Comparison with Hermes native memory plugins

Hermes ships 8 external memory plugins. This custom ChromaDB approach is
another option, not a replacement for them.

| Plugin | Type | Local? | Cost | Notable feature | Setup friction |
|--------|------|--------|------|-----------------|----------------|
| **This skill** | ChromaDB + local embedding | ✅ 100% local | Free | Auto-archive via cron, zero external deps | Low |
| holographic | SQLite + FTS5 | ✅ Local | Free | FTS5 full-text search, trust scoring | Low |
| hindsight | Knowledge graph + retrieval | ✅ Local or cloud | Free tier | Entity resolution, multi-strategy retrieval, knowledge graph | Medium |
| byterover | Hierarchical knowledge tree | Mixed | Free | Fuzzy text + LLM-driven search | Medium |
| openviking | Context DB (ByteDance) | Local server | Free | Filesystem hierarchy, auto extraction | High |
| mem0 | Server-side fact extraction | Cloud | Paid tiers | Semantic + hybrid retrieval | Medium |
| retaindb | Cloud memory API | Cloud | $20/mo | Vector + BM25 + Reranking | Medium |
| supermemory | Semantic long-term memory | Cloud/self-hosted | Paid/self-hosted | Profile recall, full-session ingest | Medium |
| honcho | Cross-session user modeling | Cloud/self-hosted | Paid | Multi-pass dialectic reasoning | High |

**When to use this skill over native plugins:**
- User wants zero external accounts/API keys
- User wants full data locality
- User wants fastest possible setup

**When to prefer a native plugin:**
- Need knowledge graph / entity relationships → hindsight
- Need FTS5 exact-match search → holographic
- Need cloud-backed multi-device sync → mem0/supermemory

## Real test results

First archive run on sample data:
- Sessions parsed: 14
- Conversations extracted: 143
- After dedup: 138
- Successfully archived: 138
- Collection count after run: 438

Semantic search relevance on test queries (English-trained local model):
- "OpenOPC 安装在哪里" → 68%
- "Pi 的 Hermes 版本" → 59%
- "dev.to API Key" → 57%
- "用什么浏览器做 Web 任务" → 52%

Chinese queries work but the local `all-MiniLM-L6-v2` is English-trained,
so precision can be lower than English queries. For higher Chinese recall,
consider pairing with a Chinese-capable embedding provider.

## Known limitations

1. **Export is one-shot.** `hermes sessions export` does not auto-watch for new
   sessions. The cron job archives whatever export files exist at run time.
   Re-export manually for backfill, or wrap export + archive in one script.
2. **Short fragments dominate early recall.** The first archive contains many
   short user echoes and assistant fragments. Recall improves as the store
   accumulates longer, more distinctive turns.
3. **Not a native memory provider.** Hermes does not treat this collection as
   its default memory. Retrieval is skill-driven, not system-prompt-driven.
4. **No knowledge graph or FTS5.** Unlike hindsight/holographic, this skill
   does not build entity graphs or support exact-match full-text search.

## References

- `references/native-plugin-comparison.md` — detailed notes on the 8 Hermes native memory plugins, setup friction, and when to switch from this custom skill to a native alternative.
- `references/session-notes.md` — implementation pitfalls discovered during setup: BigModel embedding failure, duplicate ID collisions, JSON serialization issues, cron integration details, and user preferences for integration depth.
- `references/testing-results.md` — real archive metrics, Chinese semantic search relevance scores, embedding model behavior, and verified failure-mode fixes.
- `references/github-repo.md` — published GitHub repository metadata and reproduction steps for future releases.

## User Preferences

- **Transparent integration preferred**: User chose option C over B. Memory should feel seamless; do not expose technical terms like "ChromaDB", "embedding", or "retrieval" in user-facing replies.
- **Public release preferred**: When asked to publish, create a public GitHub repo under StackWanderer775 with README + architecture + implementation docs, then push from the local project directory.

## Pitfalls

- **Hermes config writes can corrupt YAML.** `hermes config set custom_providers '[...]'`
  stores the array as a quoted YAML string, not a list. Long arrays get truncated to `[`.
  Always back up `config.yaml` before bulk edits.
- **Profile mismatch.** `config.yaml` is per-profile. If Hermes Studio shows `chuqi`,
  edits to `default\config.yaml` have no effect. Check `hermes config show`.
- **Studio restart required.** `config.yaml` and `auth.json` are not hot-reloaded.
  Fully exit the tray icon and relaunch.
- **Export does not auto-watch.** `hermes sessions export` is a one-shot command.
  The cron job in Step 6 only archives; it does not re-export. Re-export manually
  or wrap export + archive in a single script if backfill is needed.
- **Local embedding downloads ~79MB on first use.** ChromaDB caches the ONNX model
  under `~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/`. First run may take ~90s.
