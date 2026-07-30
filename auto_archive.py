"""
Hermes 会话自动存档到 ChromaDB
v0.3.0
"""
import os
import sys
import json
import hashlib
import logging
from datetime import datetime
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
logger = logging.getLogger(__name__)

# 路径配置（支持跨平台 + 环境变量）
MEMORY_DIR = Path(os.environ.get("HERMES_MEMORY_DIR", Path.home() / ".hermes" / "memory_db"))
EXPORT_DIR = Path(os.environ.get("HERMES_EXPORT_DIR", Path.home() / ".hermes" / "exports"))
STATE_FILE = EXPORT_DIR / "archive_state.json"
COLLECTION_NAME = os.environ.get("HERMES_COLLECTION_NAME", "hermes_conversations")


def init_chromadb():
    """初始化 ChromaDB（本地 embedding）"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(MEMORY_DIR))
    embedding_fn = embedding_functions.DefaultEmbeddingFunction()

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine"}
    )

    return client, collection


def _acquire_lock(lock_path):
    """获取文件锁（跨平台）"""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        return fd
    except FileExistsError:
        return None
    except Exception as e:
        logger.debug(f"获取锁失败: {e}")
        return None


def _release_lock(fd, lock_path):
    """释放文件锁"""
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(lock_path)
        except OSError:
            pass


def load_state():
    """加载存档状态"""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"状态文件损坏，将重新创建: {e}")
        except Exception as e:
            logger.warning(f"读取状态文件失败: {e}")
    return {"archived_hashes": [], "last_run": None}


def save_state(state):
    """保存存档状态（原子写入 + 文件锁）"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = str(STATE_FILE) + ".lock"

    fd = _acquire_lock(lock_path)
    if fd is None:
        logger.warning("另一个进程正在写入状态文件，跳过本次状态更新")
        return False

    try:
        temp_path = str(STATE_FILE) + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, str(STATE_FILE))
        return True
    except Exception as e:
        logger.error(f"保存状态文件失败: {e}")
        return False
    finally:
        _release_lock(fd, lock_path)


def parse_jsonl(path):
    """解析 JSONL 文件"""
    sessions = []
    line_num = 0

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    sessions.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning(f"跳过无效行 {line_num} in {path.name}: {e}")
    except Exception as e:
        logger.error(f"读取文件失败 {path}: {e}")

    return sessions


def extract_convos(session, max_messages=100):
    """从会话提取对话"""
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
    """计算内容 MD5 哈希"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def archive(max_per_session=100, dry_run=False):
    """主存档函数"""
    print("🧠 Hermes 会话自动存档 v0.3.0")
    print("=" * 50)
    logger.info(f"记忆库: {MEMORY_DIR}")
    logger.info(f"导出目录: {EXPORT_DIR}")
    logger.info(f"集合名称: {COLLECTION_NAME}")

    # 找导出文件
    export_files = sorted(EXPORT_DIR.glob("hermes_sessions*.jsonl"))
    if not export_files:
        print(f"❌ 没找到导出文件 {EXPORT_DIR}/hermes_sessions*.jsonl")
        print(f"   先运行: hermes sessions export {EXPORT_DIR}/hermes_sessions.jsonl")
        return

    print(f"📁 导出文件: {[f.name for f in export_files]}")

    # 加载所有会话
    sessions = []
    for f in export_files:
        sessions.extend(parse_jsonl(f))
    print(f"📊 总会话: {len(sessions)}")

    if not sessions:
        print("✅ 没有会话需要处理")
        return

    # 提取对话
    all_convos = []
    for s in sessions:
        convos = extract_convos(s, max_messages=max_per_session)
        all_convos.extend(convos)
    print(f"💬 提取对话: {len(all_convos)}")

    if not all_convos:
        print("✅ 没有对话内容")
        return

    # 加载状态并去重
    state = load_state()
    archived_hashes = set(state.get("archived_hashes", []))

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
    try:
        _, collection = init_chromadb()
    except Exception as e:
        logger.error(f"ChromaDB 初始化失败: {e}")
        sys.exit(1)

    # 分批写入，只有成功才更新状态
    BATCH = 50
    total = 0
    successful_hashes = []
    failed_batches = 0

    for i in range(0, len(unique_convos), BATCH):
        batch = unique_convos[i:i + BATCH]
        ids, docs, metas = [], [], []

        for h, c in batch:
            ts = c.get("timestamp")
            if isinstance(ts, (int, float)):
                ts_str = datetime.fromtimestamp(ts).isoformat()[:19]
            else:
                ts_str = str(ts)[:19] if ts else "?"

            # 复合 ID：保留 session 信息，同时保证全局唯一
            ids.append(f"c_{c['session_id'][:12]}_{h}")
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
            successful_hashes.extend([h for h, _ in batch])
            print(f"   📦 已存档 {total}/{len(unique_convos)}")
        except Exception as e:
            failed_batches += 1
            logger.error(f"批次 {i//BATCH + 1} 写入失败: {e}")
            logger.error(f"   失败的 ID: {ids[:3]}{'...' if len(ids) > 3 else ''}")
            print(f"   ⚠️ 批次 {i//BATCH + 1} 失败，已跳过")

    # 只更新成功的哈希
    if successful_hashes:
        archived_hashes.update(successful_hashes)
        state["archived_hashes"] = list(archived_hashes)
        state["last_run"] = datetime.now().isoformat()

        if save_state(state):
            logger.info(f"状态已更新：成功 {len(successful_hashes)} 条，失败 {failed_batches} 批")
        else:
            logger.error("状态文件保存失败，下次运行时会重试这些条目")

    # 统计
    try:
        total_count = collection.count()
    except Exception:
        total_count = "?"

    print(f"\n✅ 存档完成")
    print(f"   📦 本次: {total} 条")
    print(f"   📚 总计: {total_count} 条")
    print(f"   💾 路径: {MEMORY_DIR}")

    if failed_batches > 0:
        print(f"   ⚠️  有 {failed_batches} 批失败，已保留哈希待下次重试")


def stats():
    """显示统计"""
    try:
        _, collection = init_chromadb()
    except Exception as e:
        logger.error(f"ChromaDB 初始化失败: {e}")
        return

    state = load_state()
    try:
        total = collection.count()
    except Exception:
        total = "?"

    hashes = len(state.get("archived_hashes", []))
    last = state.get("last_run", "从未")

    print("📊 记忆库统计")
    print(f"   📚 记忆总数: {total}")
    print(f"   🔑 已去重哈希: {hashes}")
    print(f"   🕐 上次执行: {last}")
    print(f"   💾 存储路径: {MEMORY_DIR}")
    print(f"   📄 状态文件: {STATE_FILE}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Hermes 会话自动存档 v0.3.0")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    parser.add_argument("--stats", action="store_true", help="查看统计")
    parser.add_argument("--max", type=int, default=100, help="每会话最多取多少条")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    parser.add_argument("-q", "--quiet", action="store_true", help="安静模式")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)
    elif args.quiet:
        logger.setLevel(logging.WARNING)

    if args.stats:
        stats()
    else:
        archive(max_per_session=args.max, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
