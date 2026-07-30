# Hermes Long-Term Memory

> 为 Hermes Agent 打造的长期记忆系统。基于 ChromaDB + 本地 Embedding，自动将对话存档，让 AI 拥有跨会话的长期记忆。

[English](#english) | [中文](#中文)

---

## 中文

### 是什么？

这是一个**外挂记忆系统**，给 Hermes Agent 添加跨会话的长期记忆能力。

```
你 ↔ Hermes 对话
    ↓ 每 6 小时自动导出
hermes sessions export
    ↓ 解析、去重、向量化
auto_archive.py
    ↓ 存入向量数据库
ChromaDB (~/.hermes/memory_db)
    ↓ 语义检索
memory_ai.py → 带记忆的回答
```

### 核心特性

- **自动存档**：通过 Hermes cron 定时导出对话并存入 ChromaDB
- **语义搜索**：基于 ChromaDB 向量搜索，理解语义而非关键词
- **完全本地**：Embedding 模型本地运行，数据不离开你的电脑
- **零外部依赖**：默认不需要注册任何服务，不需要 API Key
- **即插即用**：安装 skill 后自动生效，对话自动触发记忆检索

### 架构

```
┌─────────────────────────────────────────┐
│           Hermes Agent                  │
│  ┌─────────────┐    ┌───────────────┐  │
│  │ 内置记忆    │    │ 外挂记忆 Skill │  │
│  │ MEMORY.md   │    │ (本仓库)      │  │
│  └─────────────┘    └───────┬───────┘  │
│                            │           │
│  ┌─────────────────────────▼───────┐  │
│  │      ChromaDB 向量数据库        │  │
│  │    ~/.hermes/memory_db/        │  │
│  │    collection: conversations   │  │
│  └─────────────────────────────────┘  │
└─────────────────────────────────────────┘
        ↑ 自动存档              ↑ 按需检索
    Hermes cron              execute_code
    auto_archive.py          ChromaDB query
```

### 组件说明

| 组件 | 文件 | 职责 |
|---|---|---|
| 记忆检索 Skill | `skills/hermes-long-term-memory/SKILL.md` | 告诉 Hermes 何时/如何查询记忆 |
| 自动存档脚本 | `auto_archive.py` | 解析导出的会话，存入 ChromaDB |
| 对话脚本 | `memory_ai.py` | 独立运行的带记忆的聊天程序 |
| 存档状态 | `~/.hermes/exports/archive_state.json` | 记录已存档的对话哈希，避免重复 |

### 快速开始

#### 前置要求

- Hermes 0.19+
- Python 3.11+
- pip

#### 1. 安装依赖

```powershell
pip install -r requirements.txt
```

#### 2. 安装 Skill

```powershell
# 方式1：直接复制到 skills 目录
cp -r skills/hermes-long-term-memory C:\Users\<you>\AppData\Local\hermes\skills\

# 方式2：通过 Hermes CLI
hermes skills install path/to/skills/hermes-long-term-memory
```

#### 3. 导出历史会话（可选，首次需要）

```powershell
# 导出所有历史会话到 JSONL
hermes sessions export --format jsonl ~/.hermes/exports/hermes_sessions.jsonl
```

#### 4. 首次存档

```powershell
python auto_archive.py
```

#### 5. 设置自动存档

```powershell
# 创建 cron 任务（每 6 小时自动存档）
hermes cron create \
  --name "自动存档对话到记忆库" \
  --script "scripts/auto_archive.py" \
  --no-agent \
  --deliver local \
  "every 6h" \
  "自动将 Hermes 对话存档到 ChromaDB 记忆库"
```

#### 6. 开始使用

安装 skill 后，Hermes 会在相关对话中自动查询记忆库。你无需做任何额外操作。

### 记忆检索流程

```
用户提问
    ↓
Hermes 检测到可能需要记忆
    ↓
执行 Python 查询 ChromaDB
    ↓
找到相关记忆（相似度参考阈值可自行调整）
    ↓
将记忆融入回答
    ↓
用户看到自然回答（看不到检索过程）
```

### 目录结构

```
hermes-long-term-memory/
├── README.md                  # 本文件
├── ARCHITECTURE.md            # 架构详解
├── HOW_IT_WORKS.md            # 工作原理
├── memory_ai.py               # 独立聊天程序（带记忆检索）
├── auto_archive.py            # 自动存档脚本
├── requirements.txt           # Python 依赖
├── LICENSE                    # MIT 许可证
└── skills/                    # Hermes skill 目录
    └── hermes-long-term-memory/
        └── SKILL.md           # Hermes skill 定义
```

### 工作原理

详见 [HOW_IT_WORKS.md](HOW_IT_WORKS.md)

### 配置

记忆库默认位置：`~/.hermes/memory_db/`

可通过环境变量修改：

```powershell
# Windows PowerShell
$env:HERMES_MEMORY_DIR = "D:/custom/memory_db"

# Linux/macOS
export HERMES_MEMORY_DIR="$HOME/.hermes/memory_db"
```

可用环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HERMES_MEMORY_DIR` | `~/.hermes/memory_db` | 记忆库目录 |
| `HERMES_COLLECTION_NAME` | `hermes_conversations` | ChromaDB 集合名 |
| `HERMES_EXPORT_DIR` | `~/.hermes/exports` | 会话导出目录 |
| `HERMES_MEMORY_CONFIG` | `~/.hermes/memory_config.json` | 记忆配置路径 |
| `HERMES_CHAT_CONFIG` | `~/.hermes/chat_config.json` | 对话配置路径 |
| `HERMES_CHAT_API_KEY` | 空 | 对话 API Key，优先于配置文件 |
| `HERMES_OPENAI_API_KEY` | 空 | OpenAI Embedding API Key |

### Embedding 模式

支持两种 embedding 模式：

| 模式 | 说明 | 需要 |
|---|---|---|
| `local` | 本地运行，无需 API Key | 首次下载约 79MB |
| `openai` | 使用 OpenAI 兼容 API | 需要 API Key |

在 `memory_ai.py init-memory` 时选择，或手动编辑 `~/.hermes/memory_config.json`。

### 安全说明

- 所有数据**仅存储在本地**
- 不会发送到任何第三方服务
- Embedding 模型在本地运行
- 你可以随时删除 `memory_db` 文件夹清除所有记忆
- **API Key 建议使用环境变量**，避免明文保存在配置文件中
- 如必须保存到文件，请确保文件权限适当（如仅当前用户可读）

### 故障排查

| 问题 | 解决 |
|---|---|
| ChromaDB 导入失败 | `pip install -r requirements.txt` |
| 记忆检索不到结果 | 确保已运行 `auto_archive.py` |
| cron 任务不执行 | 检查 `hermes cron status`，Gateway 需运行 |
| 中文语义不准 | 当前 Embedding 模型英文训练，中文精度有限 |
| 路径错误 | 检查环境变量 `HERMES_EXPORT_DIR` |
| 状态文件损坏 | 删除 `archive_state.json`，下次运行会自动重建 |

### 对比 Hermes 原生记忆插件

| 特性 | 本系统 | 原生插件（mem0/hindsight 等） |
|---|---|---|
| 部署难度 | 低 | 中-高 |
| 外部依赖 | 无 | 多数需要注册/API Key |
| 数据主权 | 100% 本地 | 部分在云端 |
| 自动存档 | ✅ cron | ❌ 不明显 |
| 知识图谱 | ❌ | ✅ hindsight |
| 中文支持 | ⚠️ 一般 | ⚠️ 取决于模型 |

---

## English

### What?

A long-term memory system for Hermes Agent. Uses ChromaDB + local Embedding to automatically archive conversations, giving AI cross-session memory.

### Quick Start

```powershell
pip install -r requirements.txt
python auto_archive.py
```

### Documentation

- [Architecture](ARCHITECTURE.md) - System design and data flow
- [How It Works](HOW_IT_WORKS.md) - Detailed implementation

### License

MIT

---

## 贡献

欢迎提交 Issue 和 PR。

## 相关项目

- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [ChromaDB](https://github.com/chroma-core/chroma)
