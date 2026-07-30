"""
带长期记忆的 AI 对话系统
v0.5.0
"""
import os
import sys
import json
import time
import uuid
import logging
from pathlib import Path
from datetime import datetime

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
logger = logging.getLogger(__name__)

# 检查依赖
try:
    import chromadb
    from chromadb.utils import embedding_functions
    from openai import OpenAI
except ImportError as e:
    print(f"❌ 缺少依赖: {e}")
    print("请运行: pip install -r requirements.txt")
    sys.exit(1)

# 路径配置（支持跨平台 + 环境变量）
MEMORY_DIR = Path(os.environ.get("HERMES_MEMORY_DIR", Path.home() / ".hermes" / "memory_db"))
MEMORY_CONFIG_FILE = Path(os.environ.get("HERMES_MEMORY_CONFIG", Path.home() / ".hermes" / "memory_config.json"))
CHAT_CONFIG_FILE = Path(os.environ.get("HERMES_CHAT_CONFIG", Path.home() / ".hermes" / "chat_config.json"))
COLLECTION_NAME = os.environ.get("HERMES_COLLECTION_NAME", "hermes_conversations")

# 默认配置
DEFAULT_MEMORY_CONFIG = {
    "embedding_backend": "local",  # local | openai
    "openai_api_key": "",
    "embedding_model": "text-embedding-3-small",
    "collection_name": COLLECTION_NAME,
    "max_memories": 1000,
    "max_memory_items": 5,
    "max_memory_chars": 200,
    "relevance_threshold": 0.3  # 相似度阈值，低于此值不注入
}

DEFAULT_CHAT_CONFIG = {
    "api_base": "https://open.bigmodel.cn/api/paas/v4/",
    "api_key": "",
    "model": "glm-4.5-flash",
    "temperature": 0.3,
    "max_tokens": 2000,
    "system_prompt": "你是一个有记忆的 AI 助手。你会参考历史记忆来回答问题。"
}


def _mask_key(key: str) -> str:
    """脱敏 API Key，仅显示前 6 位和后 4 位"""
    if not key or len(key) < 10:
        return "***"
    return f"{key[:6]}...{key[-4:]}"


def _get_chat_api_key(config: dict) -> str:
    """
    获取对话 API Key：优先环境变量，其次配置文件。
    注意：为避免泄露，不要在终端打印完整 key。
    """
    return os.environ.get("HERMES_CHAT_API_KEY", config.get("api_key", ""))


def load_json(path, defaults):
    """加载 JSON 配置"""
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return {**defaults, **json.load(f)}
        except json.JSONDecodeError as e:
            logger.error(f"配置 JSON 格式错误 {path}: {e}")
        except Exception as e:
            logger.warning(f"读取配置失败 {path}: {e}")
    return defaults.copy()


def save_json(path, data):
    """保存 JSON 配置"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def create_embedding_function(config):
    """根据配置创建 embedding 函数"""
    backend = config.get("embedding_backend", "local")

    if backend == "openai":
        api_key = config.get("openai_api_key", "") or os.environ.get("HERMES_OPENAI_API_KEY", "")
        if not api_key:
            raise ValueError("OpenAI embedding backend 需要配置 openai_api_key 或环境变量 HERMES_OPENAI_API_KEY")
        return embedding_functions.OpenAIEmbeddingFunction(
            api_key=api_key,
            model_name=config.get("embedding_model", "text-embedding-3-small")
        )
    else:
        return embedding_functions.DefaultEmbeddingFunction()


def init_memory():
    """初始化记忆系统"""
    config = load_json(MEMORY_CONFIG_FILE, DEFAULT_MEMORY_CONFIG)

    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(MEMORY_DIR))
        embedding_fn = create_embedding_function(config)

        collection = client.get_or_create_collection(
            name=config.get("collection_name", COLLECTION_NAME),
            embedding_function=embedding_fn,
            metadata={"hnsw:space": "cosine"}
        )
        return client, collection, config
    except Exception as e:
        logger.error(f"记忆系统初始化失败: {e}")
        return None, None, config


def _content_hash(text: str) -> str:
    """计算内容 SHA256 哈希"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def add_memory(collection, content, memory_type="general"):
    """添加记忆（带去重）"""
    if not content or not content.strip():
        return None

    if collection is None:
        logger.warning("记忆系统未连接，跳过写入")
        return None

    try:
        text = content.strip()
        content_hash_value = _content_hash(text)

        # 先查是否已存在相同内容（通过 metadata hash 精确匹配）
        existing = collection.get(
            where={"hash": content_hash_value},
            limit=1
        )
        if existing and existing.get("ids"):
            logger.debug("内容已存在，跳过重复写入")
            return existing["ids"][0]

        metadata = {
            "timestamp": datetime.now().isoformat(),
            "type": memory_type,
            "hash": content_hash_value
        }
        memory_id = f"mem_{uuid.uuid4().hex}"

        collection.add(
            documents=[text],
            metadatas=[metadata],
            ids=[memory_id]
        )
        return memory_id
    except Exception as e:
        logger.error(f"添加记忆失败: {e}")
        return None


def search_memories(collection, query, n_results=3, relevance_threshold=0.3):
    """搜索相关记忆（带阈值过滤）"""
    if not query or not collection:
        return []

    try:
        results = collection.query(
            query_texts=[query.strip()],
            n_results=n_results
        )

        memories = []
        if results and results.get("documents"):
            for i, doc in enumerate(results["documents"][0]):
                metadata = results["metadatas"][0][i] if results.get("metadatas") else {}
                distance = results["distances"][0][i] if results.get("distances") else 0

                # ChromaDB cosine 距离范围为 [0, 2]，
                #  similarity = max(0.0, 1 - distance/2)，避免负值
                similarity = max(0.0, 1 - distance / 2)

                # 阈值过滤：低于 threshold 的不返回
                if similarity < relevance_threshold:
                    logger.debug(f"跳过低相似度记忆: {similarity:.2f} < {relevance_threshold}")
                    continue

                memories.append({
                    "content": doc,
                    "timestamp": metadata.get("timestamp", "")[:19],
                    "type": metadata.get("type", "general"),
                    "relevance": similarity,
                    "distance": distance
                })
        return memories
    except Exception as e:
        logger.error(f"搜索记忆失败: {e}")
        return []


def truncate_text(text, max_chars=200):
    """截断文本到最大字符数"""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


def build_prompt_with_memories(memories, user_message, system_prompt, max_items=5, max_chars=200):
    """把记忆拼接到 prompt 里（带长度控制）"""
    if not memories:
        return system_prompt

    memories = memories[:max_items]

    memory_context = "\n## 相关记忆\n"
    for i, mem in enumerate(memories, 1):
        truncated = truncate_text(mem["content"], max_chars)
        memory_context += f"{i}. [{mem['timestamp']}] {truncated}\n"
    memory_context += "\n请参考以上记忆来回答用户的问题。\n"

    return system_prompt + memory_context


def chat_with_memory(api_base, api_key, model, user_message, memories,
                     system_prompt, temperature=0.3, max_tokens=2000,
                     max_memory_items=5, max_memory_chars=200):
    """带记忆的对话"""
    full_system_prompt = build_prompt_with_memories(
        memories, user_message, system_prompt,
        max_items=max_memory_items,
        max_chars=max_memory_chars
    )

    try:
        client = OpenAI(
            api_key=api_key,
            base_url=api_base
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": full_system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=temperature,
            max_tokens=max_tokens
        )

        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"LLM 对话失败: {e}")
        return f"❌ 对话失败: {e}"


def print_memories(memories):
    """打印记忆"""
    if not memories:
        print("   📭 无相关记忆")
        return
    for i, mem in enumerate(memories, 1):
        print(f"   {i}. [{mem['timestamp']}] {mem['content'][:80]}")
        print(f"      相似度: {mem['relevance']:.1%}")


def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("""
🧠 带记忆的 AI 对话系统 v0.5.0

用法:
  python memory_ai.py init-memory             初始化记忆系统
  python memory_ai.py init-chat               初始化对话配置
  python memory_ai.py chat                    交互式对话（带记忆）
  python memory_ai.py ask <问题>              单次提问（带记忆）
  python memory_ai.py remember <内容>         添加记忆
  python memory_ai.py config                  查看配置

环境变量:
  HERMES_MEMORY_DIR       记忆库目录 (默认: ~/.hermes/memory_db)
  HERMES_COLLECTION_NAME  集合名称 (默认: hermes_conversations)
  HERMES_EXPORT_DIR       导出目录 (默认: ~/.hermes/exports)
  HERMES_CHAT_API_KEY     对话 API Key（优先于配置文件）
  HERMES_OPENAI_API_KEY   OpenAI Embedding API Key

示例:
  python memory_ai.py init-memory
  python memory_ai.py init-chat
  python memory_ai.py chat
  python memory_ai.py ask "我应该学什么编程语言？"
  python memory_ai.py remember "用户在香港做 AI 开发"
        """)
        return

    command = sys.argv[1].lower()

    # 初始化记忆系统
    if command == "init-memory":
        print("🔧 初始化记忆系统...")
        config = load_json(MEMORY_CONFIG_FILE, DEFAULT_MEMORY_CONFIG)

        print(f"Embedding 模式: {config.get('embedding_backend', 'local')}")
        print("  1. local (本地，无需 API Key)")
        print("  2. openai (需要 OpenAI API Key)")

        backend_choice = input("选择模式 [1]: ").strip()
        if backend_choice == "2":
            config["embedding_backend"] = "openai"
            config["openai_api_key"] = input("请输入 OpenAI API Key: ").strip()
            config["embedding_model"] = input(f"Embedding 模型 [text-embedding-3-small]: ").strip() or "text-embedding-3-small"
        else:
            config["embedding_backend"] = "local"
            config.pop("openai_api_key", None)

        save_json(MEMORY_CONFIG_FILE, config)
        print("✅ 记忆系统配置已保存")

        # 测试
        try:
            _, collection, _ = init_memory()
            if collection:
                print("✅ ChromaDB 连接成功")
                print(f"📁 存储路径: {MEMORY_DIR}")
                print(f"📚 集合名称: {COLLECTION_NAME}")
            else:
                print("❌ 连接失败")
        except Exception as e:
            print(f"❌ 测试失败: {e}")

    # 初始化对话配置
    elif command == "init-chat":
        print("🔧 初始化对话配置...")
        config = load_json(CHAT_CONFIG_FILE, DEFAULT_CHAT_CONFIG)
        print(f"当前 API 地址: {config.get('api_base')}")
        print(f"当前模型: {config.get('model')}")

        if not config.get("api_key"):
            config["api_key"] = input("请输入 API Key: ").strip()

        api_base = input(f"API 地址 [{config.get('api_base')}]: ").strip()
        if api_base:
            config["api_base"] = api_base

        model = input(f"模型名称 [{config.get('model')}]: ").strip()
        if model:
            config["model"] = model

        save_json(CHAT_CONFIG_FILE, config)
        print("✅ 对话配置已保存")
        print("⚠️  安全提示：api_key 已明文保存。如需更安全，可使用环境变量 HERMES_CHAT_API_KEY")

    # 交互式对话
    elif command == "chat":
        _, collection, mem_config = init_memory()
        chat_config = load_json(CHAT_CONFIG_FILE, DEFAULT_CHAT_CONFIG)
        api_key = _get_chat_api_key(chat_config)

        if not api_key:
            print("❌ 请先运行 init-chat 配置 API Key，或设置环境变量 HERMES_CHAT_API_KEY")
            return

        max_items = mem_config.get("max_memory_items", 5)
        max_chars = mem_config.get("max_memory_chars", 200)
        relevance_threshold = mem_config.get("relevance_threshold", 0.3)

        print(f"""
🧠 带记忆的 AI 对话系统 v0.5.0
模型: {chat_config['model']}
记忆: {'✅ 已连接' if collection else '❌ 未连接'}
记忆条数限制: {max_items} 条
相似度阈值: {relevance_threshold:.0%}
输入 'quit' 退出，输入 'remember xxx' 添加记忆
        """)

        messages = [{"role": "system", "content": chat_config["system_prompt"]}]

        while True:
            user_input = input("\n你: ").strip()

            if not user_input:
                continue

            if user_input.lower() in ["quit", "exit", "退出"]:
                print("👋 再见！")
                break

            # 特殊命令：添加记忆
            if user_input.startswith("remember "):
                content = user_input[9:]
                mem_id = add_memory(collection, content)
                if mem_id:
                    print(f"✅ 已记住: {content}")
                else:
                    print("❌ 添加记忆失败（记忆系统未连接）")
                continue

            # 搜索相关记忆（带阈值过滤）
            memories = search_memories(
                collection, user_input, n_results=max_items,
                relevance_threshold=relevance_threshold
            )

            # 显示检索到的记忆
            if memories:
                print(f"\n📚 找到 {len(memories)} 条相关记忆:")
                print_memories(memories)

            # 构建带记忆的 prompt
            full_system_prompt = build_prompt_with_memories(
                memories, user_input, chat_config["system_prompt"],
                max_items=max_items,
                max_chars=max_chars
            )

            # 调用 LLM
            try:
                client = OpenAI(
                    api_key=api_key,
                    base_url=chat_config["api_base"]
                )

                response = client.chat.completions.create(
                    model=chat_config["model"],
                    messages=[
                        {"role": "system", "content": full_system_prompt},
                        {"role": "user", "content": user_input}
                    ],
                    temperature=chat_config.get("temperature", 0.3),
                    max_tokens=chat_config.get("max_tokens", 2000)
                )

                answer = response.choices[0].message.content
                print(f"\nAI: {answer}")

                # 自动保存对话到记忆（带去重）
                if collection:
                    add_memory(collection, f"用户: {user_input}", "dialogue")
                    add_memory(collection, f"AI: {answer}", "dialogue")

            except Exception as e:
                logger.error(f"对话失败: {e}")
                print(f"❌ 对话失败: {e}")

    # 单次提问
    elif command == "ask":
        if len(sys.argv) < 3:
            print("❌ 请提供问题")
            return

        question = " ".join(sys.argv[2:])
        _, collection, mem_config = init_memory()
        chat_config = load_json(CHAT_CONFIG_FILE, DEFAULT_CHAT_CONFIG)
        api_key = _get_chat_api_key(chat_config)

        if not api_key:
            print("❌ 请配置 API Key")
            return

        max_items = mem_config.get("max_memory_items", 5)
        max_chars = mem_config.get("max_memory_chars", 200)
        relevance_threshold = mem_config.get("relevance_threshold", 0.3)

        # 搜索记忆（带阈值过滤）
        memories = search_memories(
            collection, question, n_results=max_items,
            relevance_threshold=relevance_threshold
        )

        # 调用 LLM
        answer = chat_with_memory(
            chat_config["api_base"],
            api_key,
            chat_config["model"],
            question,
            memories,
            chat_config["system_prompt"],
            chat_config.get("temperature", 0.3),
            chat_config.get("max_tokens", 2000),
            max_memory_items=max_items,
            max_memory_chars=max_chars
        )

        print(f"\n问题: {question}")
        print(f"\n答案: {answer}")

        # 自动保存（带去重）
        if collection:
            add_memory(collection, f"用户: {question}", "dialogue")
            add_memory(collection, f"AI: {answer}", "dialogue")

    # 添加记忆
    elif command == "remember":
        if len(sys.argv) < 3:
            print("❌ 请提供记忆内容")
            return

        content = " ".join(sys.argv[2:])
        _, collection, _ = init_memory()
        if collection:
            mem_id = add_memory(collection, content)
            if mem_id:
                print(f"✅ 已记住: {content}")
            else:
                print("❌ 添加记忆失败")
        else:
            print("❌ 记忆系统未初始化")

    # 列出所有记忆
    elif command == "list":
        try:
            client = chromadb.PersistentClient(path=str(MEMORY_DIR))
            config = load_json(MEMORY_CONFIG_FILE, DEFAULT_MEMORY_CONFIG)
            embedding_fn = create_embedding_function(config)
            collection = client.get_collection(
                name=config.get("collection_name", COLLECTION_NAME),
                embedding_function=embedding_fn
            )

            results = collection.get(limit=100)
            if results and results.get("documents"):
                print(f"\n📚 共有 {len(results['documents'])} 条记忆:\n")
                for i, doc in enumerate(results["documents"]):
                    meta = results["metadatas"][i] if results.get("metadatas") else {}
                    ts = meta.get("timestamp", "?")[:19]
                    print(f"{i+1}. [{ts}] {doc[:100]}")
            else:
                print("📭 暂无记忆")
        except Exception as e:
            logger.error(f"列出记忆失败: {e}")
            print(f"❌ 列出记忆失败: {e}")

    # 查看配置
    elif command == "config":
        print("📋 记忆系统配置 v0.5.0")
        mem_config = load_json(MEMORY_CONFIG_FILE, DEFAULT_MEMORY_CONFIG)
        chat_config = load_json(CHAT_CONFIG_FILE, DEFAULT_CHAT_CONFIG)

        print("\n[记忆配置]")
        for k, v in mem_config.items():
            if k == "openai_api_key" and v:
                v = _mask_key(v)
            print(f"  {k}: {v}")

        print("\n[对话配置]")
        for k, v in chat_config.items():
            if k == "api_key" and v:
                v = _mask_key(v)
            print(f"  {k}: {v}")

        env_api_key = os.environ.get("HERMES_CHAT_API_KEY")
        if env_api_key:
            print(f"\n[环境变量] HERMES_CHAT_API_KEY: {_mask_key(env_api_key)} (active)")

        env_openai_key = os.environ.get("HERMES_OPENAI_API_KEY")
        if env_openai_key:
            print(f"[环境变量] HERMES_OPENAI_API_KEY: {_mask_key(env_openai_key)} (active)")

        print(f"\n📁 记忆存储: {MEMORY_DIR}")
        print(f"📄 记忆配置: {MEMORY_CONFIG_FILE}")
        print(f"📄 对话配置: {CHAT_CONFIG_FILE}")
        print(f"📚 集合名称: {COLLECTION_NAME}")

    else:
        print(f"❌ 未知命令: {command}")


if __name__ == "__main__":
    main()
