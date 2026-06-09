# Membox — 项目规格说明书

> **版本**: 0.1.0 · **状态**: Draft · **许可**: MIT

## 1. 项目定位

Membox 是一个**本地化的知识图谱 + RAG 记忆层**，面向 **coding agent**（如 Cursor、Copilot、Cline、Aider 等）提供统一的记忆服务。

核心主张：

- **Hands-on 实现** — 不依赖 Neo4j / Weaviate / Pinecone 等外部服务，全部逻辑用 Python + SQLite 手写，开发者可以完全理解和掌控每一行代码
- **CLI 优先** — 以命令行工具形式交付，coding agent 通过 **skill 文件**（指令文档）学会调用 shell 命令来使用，无需 MCP 或 HTTP 服务
- **零外部服务** — SQLite 文件级存储，无需启动数据库进程，适合单机开发环境
- **Agent 共享** — 多个 coding agent 通过同一个 SQLite 数据库文件共享记忆，避免各自维护碎片化的上下文

## 2. 目标用户与场景

| 角色 | 场景 |
|------|------|
| Coding Agent（Cursor/Copilot/Cline/Aider…） | 写代码时查询项目架构、历史决策、API 用法 |
| 开发者本人 | 通过 agent 灌入文档后检索，或直接用 Python API 查询 |
| CI/CD Pipeline | 自动提取 commit message / PR description 中的知识并入库 |

## 3. 核心功能

### 3.1 知识图谱存储

以**实体-关系-实体**三元组为核心数据模型：

```
(Entity) --[predicate]--> (Entity)
```

- **实体**：项目、技术、模块、概念、人名等，支持别名
- **关系**：带谓词的有向边，如 `uses`、`develops`、`depends_on`
- **证据溯源**：每条关系可挂载多个文档片段作为证据来源

### 3.2 文档摄入与知识提取

接收自然语言文档，通过 LLM 自动提取实体和关系三元组：

```
文档 → LLM 提取 → 三元组 → 入库（去重 + 消歧）
```

### 3.3 多跳检索

从种子实体出发，沿关系边做 BFS 扩展，`max_hops` 可调：

```
seed → 1-hop neighbors → 2-hop neighbors → ... → max_hops
```

检索结果包含完整路径和溯源原文，组装成结构化 prompt 返回。

### 3.4 实体消歧

三层级联策略，避免同一概念被重复建为不同实体：

1. **别名表精确匹配** — 别名表命中直接合并
2. **Embedding 相似度** — 同类型实体间 cosine ≥ 0.85 视为同一
3. **新建** — 前两层均未命中则创建新实体

### 3.5 谓词归一化

将语义相同的谓词归一化为标准形式：

- `developed` / `develop` / `开发` → `develops`
- lowercase + 中英文同义词字典

## 4. 架构设计

### 4.1 技术栈

| 维度 | 选型 | 理由 |
|------|------|------|
| 语言 | Python 3.13 | coding agent 生态通用语言 |
| 存储 | SQLite（WAL 模式） | 零运维，文件级存储，跨进程共享 |
| CLI | **typer** + rich | 类型注解即命令定义，自动 help / shell completion；rich 格式化输出提升可读性 |
| 类型校验 | Pydantic | 数据模型校验与序列化 |
| LLM 接口 | Protocol（Protocol Class） | 可注入任意 LLM 实现，测试不依赖 API |
| Embedding | Protocol（Protocol Class） | 可注入任意 Embedding 实现，无 embedding 时回退字符串去重 |
| 代码分析（可选） | **tree-sitter** | 多语言 AST 解析，提取结构化代码知识（函数签名、class 结构、import 依赖） |
| Agent 接入 | **skill 文件** | 非 MCP / 非 HTTP；agent 读取 skill 指令后自行调用 `membox` CLI 命令 |

### 4.2 数据模型

```sql
-- 实体
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    type        TEXT    NOT NULL DEFAULT 'thing',
    embedding   BLOB,                       -- float32 vector
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 实体别名
CREATE TABLE entity_aliases (
    entity_id   INTEGER NOT NULL REFERENCES entities(id),
    alias       TEXT    NOT NULL,
    PRIMARY KEY (entity_id, alias)
);

-- 关系（三元组）
CREATE TABLE relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES entities(id),
    target_id   INTEGER NOT NULL REFERENCES entities(id),
    predicate   TEXT    NOT NULL,
    UNIQUE(source_id, target_id, predicate)  -- 三元组去重
);

-- 文档（原始文本）
CREATE TABLE documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT    NOT NULL,
    source      TEXT,                        -- 来源标识（文件路径、URL 等）
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 关系-文档证据（多对多）
CREATE TABLE relation_evidence (
    relation_id  INTEGER NOT NULL REFERENCES relations(id),
    document_id  INTEGER NOT NULL REFERENCES documents(id),
    PRIMARY KEY (relation_id, document_id)
);
```

### 4.3 核心模块

```
src/membox/
├── __init__.py          # 包入口，暴露公开 API
├── schema.py            # Pydantic 模型定义
├── store.py             # SQLite 存储层（建表、CRUD、BFS 检索）
├── extract.py           # LLM 知识提取（Protocol + OpenAI 实现）
├── embed.py             # Embedding 计算（Protocol + OpenAI 实现）
├── normalize.py         # 谓词归一化、同义词字典
├── agent.py             # MemoryAgent — 内部编排层
├── cli.py               # typer CLI 入口（ingest / query / list 命令）
├── ast_parser.py        # tree-sitter 代码分析（可选模块）
└── py.typed             # PEP 561 标记
```

### 4.4 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 并发安全 | per-thread connection + WAL + `RLock` | SQLite WAL 允许读写并发；`RLock` 保护 find-or-create 临界区 |
| 外键约束 | `PRAGMA foreign_keys=ON` | SQLite 默认不开启，需显式启用 |
| 三元组唯一性 | `UNIQUE(source_id, target_id, predicate)` | 同一对实体间同一谓词只允许一条边 |
| LLM/Embedding 解耦 | Protocol | 可注入假实现，测试完全不依赖外部 API |
| 无 embedding 时 | 回退到字符串精确 + 大小写归一化去重 | 保证无 OpenAI key 时核心功能可用 |
| Agent 接入方式 | skill 文件（CLI 指令文档） | agent 读取 skill 后自行调 shell 命令，无需 MCP / HTTP |
| CLI 框架 | typer + rich | 类型注解即接口定义，agent 看 `--help` 即可学会使用 |
| 代码结构分析 | tree-sitter（可选） | 多语言 AST 解析，提取模块依赖 / 调用图等结构化知识 |

## 5. 接口设计

### 5.1 CLI 命令（主要接口，面向 coding agent）

Agent 通过 skill 文件学会以下命令，无需理解 Python API：

```bash
# 摄入文本
membox ingest "codebase-rag 用 Python 实现" --source "README.md"

# 摄入文件
membox ingest-file docs/architecture.md --db memory.db

# 查询记忆
membox query "项目用了哪些技术？" --max-hops 2

# 列出实体
membox list-entities --db memory.db

# 列出关系
membox list-relations --db memory.db

# 分析源码结构（tree-sitter，可选）
membox analyze-src src/ --language python --db memory.db
```

所有命令支持 `--help`，agent 可自行发现用法。

### 5.2 Python API（高级用法）

```python
from membox import MemoryAgent, OpenAIExtractor, OpenAIEmbedder

agent = MemoryAgent(
    extractor=OpenAIExtractor(client),   # 必需
    embedder=OpenAIEmbedder(client),     # 可选；不传则回退字符串去重
    db_path="memory.db",                 # SQLite 文件路径
)
```

### 5.3 核心方法

```python
# 摄入文档 → 自动提取三元组并入库
agent.ingest(text: str, source: str | None = None) -> None

# 查询 → 从种子实体 BFS 扩展，返回结构化 prompt
agent.query(question: str, max_hops: int = 2) -> str

# 查看图谱中所有实体
agent.list_entities() -> list[Entity]

# 查看图谱中所有关系
agent.list_relations() -> list[Relation]
```

## 6. 质量要求

### 6.1 测试覆盖

测试不依赖任何外部 API（LLM / Embedding 均使用假实现），覆盖以下场景：

- **实体消歧** — 字符串精确去重 / 大小写去重 / embedding 同义词去重 / 反例（无关实体不合并）
- **关系去重** — `UNIQUE` 约束 + evidence 多对多
- **谓词归一化** — developed / develop / 开发 → develops
- **多跳检索** — 2-hop 召回验证 / 3-hop 召回验证 / 不相关实体不召回
- **上下文聚合** — 多跳路径的完整上下文还原
- **溯源** — 从关系反查原文
- **并发安全** — 多线程写入无错误、计数精确、同名实体最终唯一
- **外键约束** — 实际生效验证

### 6.2 代码质量

| 工具 | 用途 | 配置 |
|------|------|------|
| Ruff | lint + format | target py313, line-length 100 |
| mypy | 类型检查 | strict mode |
| pytest | 测试 | importlib mode, strict markers |
| pre-commit | Git hooks | ruff, trailing whitespace, large files, merge conflicts |
| CI (GitHub Actions) | 持续集成 | 自动 lint + type check + test |

### 6.3 覆盖率

最低 80%（`fail_under = 80`），`show_missing = true`。

## 7. 依赖

### 运行时

- `pydantic` — 数据模型校验
- `typer` — CLI 框架（类型注解即命令定义）
- `rich` — 终端格式化输出

### 可选

- `openai` — OpenAI API 客户端（仅 live demo / 真实 LLM 提取时需要）
- `tree-sitter` — 多语言 AST 解析（仅代码结构分析时需要）

### 开发

- `pytest >= 8` / `pytest-cov >= 6` — 测试
- `ruff >= 0.11` — lint + format
- `mypy >= 1.15` — 类型检查
- `pre-commit >= 4` — Git hooks

## 8. 扩展方向

### 已规划（见 roadmap.md）

| 方向 | 说明 | 阶段 |
|------|------|------|
| Skill 文件 | 为各 coding agent 编写 skill 指令文件，教会 agent 使用 CLI | Phase 9 |
| 代码结构分析 | tree-sitter 多语言 AST 解析，提取模块依赖 / 调用图 / class 结构 | Phase 10 |

### 远期可选

| 方向 | 说明 | 触发条件 |
|------|------|----------|
| 向量索引升级 | `find_similar_entity` 换用 sqlite-vss / FAISS / Lance | 实体量 > ~10 万 |
| 谓词自动聚类 | embedding 聚类自动发现同义谓词 | 谓词种类爆炸时 |
| 置信度与审计 | entities 加 `confidence` / `merged_from` 字段 | 需要人工审核时 |
| Hybrid Retrieval | BM25 over `documents.content` + 向量检索 | 纯图谱召回不足时 |
| 时间衰减 | `relation_evidence` 加 `confidence` / `extracted_at` | 需要知识时效性时 |
