# Membox — 实现路线图

> 基于 [spec.md](./spec.md) 拆解，自底向上构建：框架 → 入口 → 功能 → 依赖扩展 → 插件。

## Layer 1 — 框架（Foundation）

> 数据模型、存储层、核心协议。不依赖任何外部服务，纯 SQLite + Python。

### Phase 0 — 项目骨架 ✅

已有脚手架 + 运行时依赖。

- [x] `pyproject.toml` 配置（typer, rich, pydantic）
- [x] pre-commit hooks
- [x] GitHub Actions CI
- [x] CLI 入口点注册（`membox` 命令可用）
- [x] 最小 `cli.py`（`version` 命令）
- [x] 可选依赖组（`openai`, `tree-sitter`）

### Phase 1 — 数据模型与存储层

**目标**：能建表、能 CRUD，不涉及任何 LLM。

- [ ] `src/membox/schema.py` — Pydantic 模型（Entity, Relation, Document, Evidence）
- [ ] `src/membox/store.py` — SQLite 存储层
  - [ ] 建表 DDL（entities, entity_aliases, relations, documents, relation_evidence）
  - [ ] `PRAGMA foreign_keys=ON` + WAL 模式
  - [ ] 实体 CRUD：`insert_entity` / `find_entity_by_name` / `list_entities`
  - [ ] 别名 CRUD：`add_alias` / `find_entity_by_alias`
  - [ ] 关系 CRUD：`insert_relation`（UNIQUE 去重）/ `list_relations`
  - [ ] 文档 CRUD：`insert_document` / `get_document`
  - [ ] 证据 CRUD：`add_evidence` / `get_evidence_for_relation`
- [ ] 测试：建表、外键约束、三元组 UNIQUE 去重、evidence 多对多

**验证**：`uv run pytest tests/ -v` 全绿，无外部依赖。

### Phase 2 — 谓词归一化

**目标**：语义相同的谓词归一化为标准形式。

- [ ] `src/membox/normalize.py`
  - [ ] `normalize_predicate(predicate: str) -> str`
  - [ ] 内置中英文同义词字典（`developed`/`develop`/`开发` → `develops` 等）
  - [ ] lowercase + 字典查找，未命中则原样返回 lowercase
- [ ] 测试：谓词归一化各种 case

**验证**：单元测试覆盖中英文同义词 + 未知谓词 pass-through。

### Phase 3 — 核心协议定义

**目标**：定义 LLM / Embedding / Extractor 的 Protocol 接口 + Dummy 实现，后续功能模块依赖协议而非实现。

- [ ] `src/membox/protocols.py`
  - [ ] `LLMExtractor` Protocol：`extract(text: str) -> list[Triple]`
  - [ ] `Embedder` Protocol：`embed(text: str) -> list[float]`
  - [ ] `DummyExtractor`：基于规则的假实现
  - [ ] `DummyEmbedder`：基于规则的假实现（如哈希映射到固定维度向量）
- [ ] 测试：Dummy 实现返回值类型正确

**验证**：Protocol 定义完毕，所有后续功能模块可依赖接口而非实现。

---

## Layer 2 — 入口（Entry Points）

> CLI 命令和 Python API。先搭骨架命令，功能由 Layer 3 填充。

### Phase 4 — CLI 骨架

**目标**：所有 CLI 命令注册就位，内部调用 placeholder，后续功能填入后自动生效。

- [ ] `src/membox/cli.py` 扩展
  - [ ] `membox ingest` — 摄入文本
  - [ ] `membox ingest-file` — 摄入文件
  - [ ] `membox query` — 查询记忆
  - [ ] `membox list-entities` — 列出实体
  - [ ] `membox list-relations` — 列出关系
  - [ ] 所有命令支持 `--db` / `--help`
- [ ] `pyproject.toml` 更新描述
- [ ] rich 格式化输出（表格展示实体/关系）
- [ ] 测试（`typer.testing.CliRunner`）
  - [ ] 各命令 `--help` 输出正确
  - [ ] placeholder 调用不报错

**验证**：`uv run membox --help` 输出完整命令列表。

### Phase 5 — MemoryAgent 入口

**目标**：Python API 骨架，内部编排层，串联 store + protocols。

- [ ] `src/membox/agent.py` — `MemoryAgent` 类
  - [ ] `__init__(extractor, embedder, db_path)`
  - [ ] `ingest(text, source=None)` — 提取三元组 → 消歧 → 入库
  - [ ] `query(question, max_hops=2)` — 种子定位 → BFS → 组装 prompt
  - [ ] `list_entities()` / `list_relations()`
- [ ] `src/membox/__init__.py` — 导出公开 API
- [ ] CLI 命令接入 MemoryAgent（Phase 4 的 placeholder 替换为真实调用）

**验证**：用 DummyExtractor + DummyEmbedder 跑通 ingest → query 端到端。

---

## Layer 3 — 功能（Features）

> 核心业务逻辑，填充 Layer 2 入口背后的实现。

### Phase 6 — 实体消歧

**目标**：同名/近义实体自动合并，不重复创建。

- [ ] `src/membox/store.py` 扩展
  - [ ] `find_or_create_entity(name, type, embedder)` — 三层级联消歧
    1. 别名精确匹配
    2. 同类型 embedding cosine ≥ 0.85
    3. 新建
  - [ ] 字符串回退（无 embedder 时：精确 + 大小写归一化）
- [ ] 测试
  - [ ] 字符串精确去重
  - [ ] 大小写去重（`Python` vs `python`）
  - [ ] embedding 同义词去重
  - [ ] 反例：无关实体不合并
  - [ ] 并发同名实体（8 线程同时 find_or_create，最终只有 1 条）

**验证**：消歧测试全绿，并发无错误。

### Phase 7 — 多跳检索

**目标**：从种子实体出发 BFS 扩展，返回带溯源的结构化上下文。

- [ ] `src/membox/store.py` 扩展
  - [ ] `bfs_query(seed_entity_ids, max_hops) -> list[HopResult]`
  - [ ] 每跳记录路径、关联实体、关系、证据原文
- [ ] 测试
  - [ ] 2-hop 召回 C、不召回 D
  - [ ] 3-hop 召回 D
  - [ ] 上下文聚合（路径完整还原）
  - [ ] 溯源原文还原

**验证**：精确控制图谱数据，验证 BFS 行为正确。

### Phase 8 — 并发安全加固

**目标**：多 agent 同时写入不冲突。

- [ ] per-thread SQLite connection（`threading.local()`）
- [ ] WAL 模式已开启（Phase 1）
- [ ] `RLock` 守护 `find_or_create_entity` 临界区
- [ ] 测试
  - [ ] 多线程并发写入（5 线程 × 10 写入，无错误，计数精确）
  - [ ] 并发同名实体（Phase 6 已有，此处验证 RLock 正确性）

**验证**：并发测试无错误、无死锁。

---

## Layer 4 — 依赖扩展（Dependency Extensions）

> 接入真实外部服务，替换 Dummy 实现。

### Phase 9 — OpenAI 集成

**目标**：接入真实 LLM，可跑端到端 demo。

- [ ] `src/membox/extract.py` — `OpenAIExtractor` 实现
- [ ] `src/membox/embed.py` — `OpenAIEmbedder` 实现
- [ ] `examples/demo.py` — 端到端 demo 脚本
- [ ] 手动验证：灌入真实文档 → 查询返回有意义的结果

**验证**：`OPENAI_API_KEY=sk-... uv run python examples/demo.py` 跑通。

---

## Layer 5 — 插件（Plugins）

> 可选增强，不影响核心功能。

### Phase 10 — 代码结构分析（tree-sitter）

**目标**：通过 AST 解析提取代码结构化知识，补充纯文本 RAG 的不足。

- [ ] `src/membox/ast_parser.py`
  - [ ] tree-sitter 集成，按需加载语言 grammar
  - [ ] 提取结构三元组：`module --defines--> class` / `class --has_method--> method` / `method --calls--> function`
  - [ ] CLI 命令：`membox analyze-src <path> --language <lang>`
- [ ] 先支持 Python grammar，后续按需加 TypeScript / Go / Rust
- [ ] 提取结果直接入图谱（复用 Layer 1-3 的存储与消歧）
- [ ] 测试
  - [ ] Python 源文件解析
  - [ ] 模块依赖图提取
  - [ ] class 结构提取

**验证**：对自身代码库运行 `membox analyze-src src/`，查询能召回模块结构。

### Phase 11 — Skill 文件

**目标**：为各 coding agent 编写 skill 指令文件，教会 agent 使用 `membox` CLI。

- [ ] `skills/membox-skill.md` — 通用 skill 模板
  - [ ] 安装说明（`pip install membox` 或 `uv tool install`）
  - [ ] 命令参考（ingest / query / list）
  - [ ] 使用场景示例（项目初始化时灌入文档、编码时查询架构决策）
- [ ] 适配特定 agent 的 skill 变体（如需）
- [ ] 手动验证：agent 读取 skill 后能正确调用 CLI

**验证**：将 skill 文件注入 agent 上下文，agent 能自主完成 ingest + query。

---

## Layer 6 — 发布（Release）

### Phase 12 — 打磨与发布

- [ ] README.md 更新（安装、CLI 用法、API 示例、skill 接入）
- [ ] 文档补全（docstring 覆盖所有公开 API）
- [ ] 覆盖率达标（≥ 80%）
- [ ] `uv run mypy src` 零错误
- [ ] `uv run ruff check .` 零警告
- [ ] 版本号更新（`0.1.0` → `1.0.0` 或 `0.1.0` 首个可用版）

---

## 构建顺序

```
Layer 1 框架
  Phase 0 骨架 ✅
  Phase 1 存储 ──→ Phase 2 归一化 ──→ Phase 3 协议
                                            │
Layer 2 入口                                │
  Phase 4 CLI 骨架 ←────────────────────────┘
       │
  Phase 5 MemoryAgent ←── 依赖 Phase 1-4
       │
Layer 3 功能                                │
  Phase 6 实体消歧 ←── 依赖 Phase 3 (Embedder Protocol)
       │
  Phase 7 多跳检索 ←── 依赖 Phase 1 (store)
       │
  Phase 8 并发加固 ←── 依赖 Phase 6 (find_or_create)
       │
Layer 4 依赖扩展                             │
  Phase 9 OpenAI ←── 替换 Phase 3 的 Dummy 实现
       │
Layer 5 插件（可并行）
  Phase 10 tree-sitter
  Phase 11 Skill 文件
       │
Layer 6 发布
  Phase 12 打磨
```

**关键依赖链**：Phase 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 12

**可并行**：Phase 10 / 11 在 Phase 9 之后可并行推进。
