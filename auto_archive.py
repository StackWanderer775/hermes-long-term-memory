"""
Hermes 会话自动存档到 ChromaDB
修复版：全局去重，避免重复 ID
"""
import os
import sys
import json
import hashlib
from datetime import datetime
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

# 路径
MEMORY_DIR = Path.home() / ".hermes" / "memory_db"
EXPORT_DIR = Path("D:/agent")
STATE_FILE = EXPORT_DIR / "archive_state.json"
COLLECTION_NAME = "hermes_conversations"


def init_chromadb():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(MEMORY_DIR))
    embedding_fn = embedding_functions.DefaultEmbeddingFunction()
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine"}
    )
    return client, collection


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"archived_hashes": [], "last_run": None}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def parse_jsonl(path):
    sessions = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                sessions.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return sessions


def extract_convos(session, max_messages=100):
    """提取对话，限制数量"""
    messages = session.get("messages", [])
    if not messages:
        return []
    
    # 只取最近的消息
    recent = messages[-max_messages:]
    
    convos = []
    cur_role = None
    cur_parts = []
    cur_ts = None
    
    for msg in recent:
        role = msg.get("role", "?")
        content = (msg.get("content") or "").strip()
        if not content or role == "tool":
            continue
        
        ts = msg.get("timestamp")
        
        if role == cur_role:
            cur_parts.append(content)
        else:
            if cur_role and cur_parts:
                convos.append({
                    "role": cur_role,
                    "content": " ".join(cur_parts),
                    "timestamp": cur_ts,
                    "session_id": session.get("id", "?")
                })
            cur_role = role
            cur_parts = [content]
            cur_ts = ts
    
    if cur_role and cur_parts:
        convos.append({
            "role": cur_role,
            "content": " ".join(cur_parts),
            "timestamp": cur_ts,
            "session_id": session.get("id", "?")
        })
    
    return convos


def content_hash(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def archive(max_per_session=100, dry_run=False):
    print("🧠 Hermes 会话自动存档")
    print("=" * 50)
    
    # 找导出文件
    export_files = list(EXPORT_DIR.glob("hermes_sessions*.jsonl"))
    if not export_files:
        print("❌ 没找到导出文件 D:/agent/hermes_sessions*.jsonl")
        print("   先运行: hermes sessions export D:/agent/hermes_sessions.jsonl")
        return
    
    print(f"📁 导出文件: {[f.name for f in export_files]}")
    
    # 加载所有会话
    sessions = []
    for f in export_files:
        sessions.extend(parse_jsonl(f))
    print(f"📊 总会话: {len(sessions)}")
    
    # 提取对话
    all_convos = []
    for s in sessions:
        convos = extract_convos(s, max_messages=max_per_session)
        all_convos.extend(convos)
    print(f"💬 提取对话: {len(all_convos)}")
    
    # 加载状态
    state = load_state()
    archived_hashes = set(state.get("archived_hashes", []))
    
    # 全局去重
    seen_hashes = set()
    unique_convos = []
    for c in all_convos:
        h = content_hash(c["content"])
        if h in seen_hashes or h in archived_hashes:
            continue
        seen_hashes.add(h)
        unique_convos.append((h, c))
    
    print(f"✨ 新增对话: {len(unique_convos)} (去重后)")
    
    if not unique_convos:
        print("✅ 没有新内容，跳过")
        return
    
    if dry_run:
        print("\n[DRY RUN] 只展示，不写入")
        for i, (h, c) in enumerate(unique_convos[:10]):
            ts = c.get("timestamp", "?")
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
            print(f"  {i+1}. [{c['role']}] {c['content'][:60]}... ({ts})")
        if len(unique_convos) > 10:
            print(f"  ... 还有 {len(unique_convos) - 10} 条")
        return
    
    # 初始化 ChromaDB
    _, collection = init_chromadb()
    
    # 分批写入
    BATCH = 50
    total = 0
    
    for i in range(0, len(unique_convos), BATCH):
        batch = unique_convos[i:i + BATCH]
        ids, docs, metas = [], [], []
        
        for h, c in batch:
            ts = c.get("timestamp")
            if isinstance(ts, (int, float)):
                ts_str = datetime.fromtimestamp(ts).isoformat()[:19]
            else:
                ts_str = str(ts)[:19] if ts else "?"
            
            # 用完整哈希作为 ID（唯一）
            ids.append(f"c_{h}")
            docs.append(c["content"])
            metas.append({
                "timestamp": ts_str,
                "role": c["role"],
                "session_id": c["session_id"],
                "hash": h
            })
        
        try:
            collection.add(documents=docs, metadatas=metas, ids=ids)
            total += len(batch)
            print(f"   📦 已存档 {total}/{len(unique_convos)}")
        except Exception as e:
            print(f"   ⚠️ 批次 {i//BATCH + 1} 失败: {e}")
    
    # 更新状态
    archived_hashes.update([h for h, _ in unique_convos])
    state["archived_hashes"] = list(archived_hashes)
    state["last_run"] = datetime.now().isoformat()
    save_state(state)
    
    # 统计
    total_count = collection.count()
    print(f"\n✅ 存档完成")
    print(f"   📦 本次: {total} 条")
    print(f"   📚 总计: {total_count} 条")
    print(f"   💾 路径: {MEMORY_DIR}")


def stats():
    _, collection = init_chromadb()
    state = load_state()
    total = collection.count()
    hashes = len(state.get("archived_hashes", []))
    last = state.get("last_run", "从未")
    print("📊 记忆库统计")
    print(f"   📚 记忆总数: {total}")
    print(f"   🔑 已去重哈希: {hashes}")
    print(f"   🕐 上次执行: {last}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    parser.add_argument("--stats", action="store_true", help="查看统计")
    parser.add_argument("--max", type=int, default=100, help="每会话最多取多少条")
    args = parser.parse_args()
    
    if args.stats:
        stats()
    else:
        archive(max_per_session=args.max, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
