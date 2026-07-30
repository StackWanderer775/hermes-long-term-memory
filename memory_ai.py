"""
带长期记忆的 AI 对话系统
结合 ChromaDB 记忆 + OpenAI 兼容 API
"""
import os
import sys
import json
import time
from datetime import datetime

# 检查依赖
try:
    import chromadb
    from chromadb.utils import embedding_functions
    from openai import OpenAI
except ImportError as e:
    print(f"❌ 缺少依赖: {e}")
    print("请运行: pip install chromadb openai")
    sys.exit(1)

# 配置路径
MEMORY_DIR = os.path.expanduser("~/.hermes/memory_db")
CONFIG_FILE = os.path.expanduser("~/.hermes/memory_config.json")
CHAT_CONFIG_FILE = os.path.expanduser("~/.hermes/chat_config.json")

# 默认配置
DEFAULT_MEMORY_CONFIG = {
    "openai_api_key": "",
    "embedding_model": "text-embedding-3-small",
    "collection_name": "hermes_memory",
    "max_memories": 1000
}

DEFAULT_CHAT_CONFIG = {
    "api_base": "https://open.bigmodel.cn/api/paas/v4/",
    "api_key": "",
    "model": "glm-4.5-flash",
    "temperature": 0.3,
    "max_tokens": 2000,
    "system_prompt": "你是一个有记忆的 AI 助手。你会参考历史记忆来回答问题。"
}


def load_json(path, defaults):
    """加载 JSON 配置"""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return {**defaults, **json.load(f)}
    return defaults.copy()


def save_json(path, data):
    """保存 JSON 配置"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def init_memory():
    """初始化记忆系统"""
    config = load_json(CONFIG_FILE, DEFAULT_MEMORY_CONFIG)
    
    if not config.get("openai_api_key"):
        print("❌ 请先配置记忆系统 API Key")
        print("   python memory_ai.py init-memory")
        return None, None
    
    try:
        client = chromadb.PersistentClient(path=MEMORY_DIR)
        embedding_fn = embedding_functions.OpenAIEmbeddingFunction(
            api_key=config["openai_api_key"],
            model_name=config.get("embedding_model", "text-embedding-3-small")
        )
        collection = client.get_or_create_collection(
            name=config.get("collection_name", "hermes_memory"),
            embedding_function=embedding_fn,
            metadata={"hnsw:space": "cosine"}
        )
        return client, collection
    except Exception as e:
        print(f"❌ 记忆系统初始化失败: {e}")
        return None, None


def add_memory(collection, content, memory_type="general"):
    """添加记忆"""
    if not content or not content.strip():
        return None
    
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "type": memory_type
    }
    memory_id = f"mem_{int(time.time() * 1000)}"
    
    collection.add(
        documents=[content.strip()],
        metadatas=[metadata],
        ids=[memory_id]
    )
    return memory_id


def search_memories(collection, query, n_results=3):
    """搜索相关记忆"""
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
                memories.append({
                    "content": doc,
                    "timestamp": metadata.get("timestamp", "")[:19],
                    "type": metadata.get("type", "general"),
                    "relevance": 1 - distance
                })
        return memories
    except Exception as e:
        print(f"搜索记忆失败: {e}")
        return []


def build_prompt_with_memories(memories, user_message, system_prompt):
    """把记忆拼接到 prompt 里"""
    # 构建记忆上下文
    memory_context = ""
    if memories:
        memory_context = "\n## 相关记忆\n"
        for i, mem in enumerate(memories, 1):
            memory_context += f"{i}. [{mem['timestamp']}] {mem['content']}\n"
        memory_context += "\n请参考以上记忆来回答用户的问题。\n"
    
    # 构建完整 prompt
    full_system_prompt = system_prompt + memory_context
    
    return full_system_prompt


def chat_with_memory(api_base, api_key, model, user_message, memories, 
                     system_prompt, temperature=0.3, max_tokens=2000):
    """带记忆的对话"""
    # 构建带记忆的 system prompt
    full_system_prompt = build_prompt_with_memories(memories, user_message, system_prompt)
    
    # 调用 LLM
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
        return f"❌ 对话失败: {e}"


def print_memories(memories):
    """打印记忆"""
    if not memories:
        print("   📭 无相关记忆")
        return
    for i, mem in enumerate(memories, 1):
        print(f"   {i}. [{mem['timestamp']}] {mem['content'][:80]}")
        print(f"      相关性: {mem['relevance']:.1%}")


def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("""
🧠 带记忆的 AI 对话系统

用法:
  python memory_ai.py init-memory             初始化记忆系统
  python memory_ai.py init-chat               初始化对话配置
  python memory_ai.py chat                    交互式对话（带记忆）
  python memory_ai.py ask <问题>              单次提问（带记忆）
  python memory_ai.py remember <内容>         添加记忆
  python memory_ai.py config                  查看配置

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
        config = load_json(CONFIG_FILE, DEFAULT_MEMORY_CONFIG)
        config["openai_api_key"] = input("请输入 OpenAI API Key (用于 Embedding): ").strip()
        save_json(CONFIG_FILE, config)
        print("✅ 记忆系统配置已保存")
        
        # 测试
        try:
            client, collection = init_memory()
            print("✅ ChromaDB 连接成功")
            print(f"📁 存储路径: {MEMORY_DIR}")
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
    
    # 交互式对话
    elif command == "chat":
        # 初始化
        memory_client, collection = init_memory()
        chat_config = load_json(CHAT_CONFIG_FILE, DEFAULT_CHAT_CONFIG)
        
        if not chat_config.get("api_key"):
            print("❌ 请先运行 init-chat 配置 API Key")
            return
        
        print(f"""
🧠 带记忆的 AI 对话系统
模型: {chat_config['model']}
记忆: {'✅ 已连接' if collection else '❌ 未连接'}
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
                continue
            
            # 搜索相关记忆
            memories = search_memories(collection, user_input, n_results=3)
            
            # 显示检索到的记忆（调试用）
            if memories:
                print(f"\n📚 找到 {len(memories)} 条相关记忆:")
                print_memories(memories)
            
            # 构建带记忆的 prompt
            full_system_prompt = build_prompt_with_memories(
                memories, user_input, chat_config["system_prompt"]
            )
            
            # 调用 LLM
            try:
                client = OpenAI(
                    api_key=chat_config["api_key"],
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
                
                # 自动保存对话到记忆
                add_memory(collection, f"用户: {user_input}", "dialogue")
                add_memory(collection, f"AI: {answer}", "dialogue")
                
            except Exception as e:
                print(f"❌ 对话失败: {e}")
    
    # 单次提问
    elif command == "ask":
        if len(sys.argv) < 3:
            print("❌ 请提供问题")
            return
        
        question = " ".join(sys.argv[2:])
        memory_client, collection = init_memory()
        chat_config = load_json(CHAT_CONFIG_FILE, DEFAULT_CHAT_CONFIG)
        
        # 搜索记忆
        memories = search_memories(collection, question, n_results=3)
        
        # 调用 LLM
        answer = chat_with_memory(
            chat_config["api_base"],
            chat_config["api_key"],
            chat_config["model"],
            question,
            memories,
            chat_config["system_prompt"],
            chat_config.get("temperature", 0.3),
            chat_config.get("max_tokens", 2000)
        )
        
        print(f"\n问题: {question}")
        print(f"\n答案: {answer}")
        
        # 自动保存
        if collection:
            add_memory(collection, f"用户: {question}", "dialogue")
            add_memory(collection, f"AI: {answer}", "dialogue")
    
    # 添加记忆
    elif command == "remember":
        if len(sys.argv) < 3:
            print("❌ 请提供记忆内容")
            return
        
        content = " ".join(sys.argv[2:])
        _, collection = init_memory()
        if collection:
            mem_id = add_memory(collection, content)
            if mem_id:
                print(f"✅ 已记住: {content}")
        else:
            print("❌ 记忆系统未初始化")
    
    # 列出所有记忆
    elif command == "list":
        client = chromadb.PersistentClient(path=MEMORY_DIR)
        config = load_json(CONFIG_FILE, DEFAULT_MEMORY_CONFIG)
        embedding_fn = embedding_functions.OpenAIEmbeddingFunction(
            api_key=config["openai_api_key"],
            model_name=config.get("embedding_model", "text-embedding-3-small")
        )
        collection = client.get_collection(
            name=config.get("collection_name", "hermes_memory"),
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
    
    # 查看配置
    elif command == "config":
        print("📋 记忆系统配置:")
        mem_config = load_json(CONFIG_FILE, DEFAULT_MEMORY_CONFIG)
        chat_config = load_json(CHAT_CONFIG_FILE, DEFAULT_CHAT_CONFIG)
        
        print("\n[记忆配置]")
        for k, v in mem_config.items():
            if k == "openai_api_key" and v:
                v = v[:10] + "..." + v[-4:]
            print(f"  {k}: {v}")
        
        print("\n[对话配置]")
        for k, v in chat_config.items():
            if k == "api_key" and v:
                v = v[:10] + "..." + v[-4:]
            print(f"  {k}: {v}")
        
        print(f"\n📁 记忆存储: {MEMORY_DIR}")
        print(f"📄 记忆配置: {CONFIG_FILE}")
        print(f"📄 对话配置: {CHAT_CONFIG_FILE}")
    
    else:
        print(f"❌ 未知命令: {command}")


if __name__ == "__main__":
    main()