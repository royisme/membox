# Membox — 实现路线图

> 基于 [spec.md](./spec.md)，接口先行，自顶向下：先搭完整骨架（CLI → Agent → 各子模块 Protocol），再逐个填充实现。

## Phase 0 — 项目骨架 ✅

已有脚手架 + 运行时依赖。

- [x] `pyproject.toml` 配置（typer, rich, pydantic）
- [x] pre-commit hooks
- [x] GitHub Actions CI
- [x] CLI 入口点注册（`membox` 命令可用）
- [x] 最小 `cli.py`（`version` 命令）
- [x] 可选依赖组（`openai`, `tree-sitter`）

## Phase 1 — 完整框架骨架

**目标**：从 CLI 到最底层，所有模块、所有函数签名、所有 Protocol 全部就位。函数体可以是 stub，但 **import 链完全串通**，`membox --help` 输出所有命令。

```
cli.py                    ← typer 命令，每个命令调用 agent
  └→ agent.py             ← MemoryAgent 类，编排各子模块
       ├→ schema.py       ← Pydantic 模型（Entity, Relation, Document, Triple, HopResult）
       ├→ store.py        ← KnowledgeStore 类（Protocol + stub 方法）
       ├→ normalize.py    ← normalize_predicate()（stub）
       ├→ extract.py      ← LLMExtractor Protocol + DummyExtractor
       └→ embed.py        ← Embedder Protocol + DummyEmbedder
```

### 1.1 数据模型 `schema.py`

- [ ] `Entity` — 实体模型（id, name, type, embedding, created_at）
- [ ] `EntityAlias` — 别名模型（entity_id, alias）
- [ ] `Relation` — 关系模型（id, source_id, target_id, predicate）
- [ ] `Document` — 文档模型（id, content, source, created_at）
- [ ] `Evidence` — 证据模型（relation_id, document_id）
- [ ] `Triple` — 提取结果（source, predicate, target, source_type, target_type）
- [ ] `HopResult` — BFS 单跳结果（entity, relation, via_entities, evidences）

### 1.2 协议定义 `protocols` 分散到各模块

- [ ] `store.py` — `KnowledgeStore` 类，所有方法签名就位（stub 实现）
- [ ] `extract.py` — `LLMExtractor` Protocol + `DummyExtractor` 完整实现
- [ ] `embed.py` — `Embedder` Protocol + `DummyEmbedder` 完整实现
- [ ] `normalize.py` — `normalize_predicate()` stub

### 1.3 编排层 `agent.py`

- [ ] `MemoryAgent.__init__(store, extractor, embedder, db_path)`
- [ ] `ingest(text, source)` — 调用 extractor → normalize → store.find_or_create → store.add_relation
- [ ] `query(question, max_hops)` — 调用 store.bfs_query → 组装 prompt
- [ ] `list_entities()` / `list_relations()` — 代理到 store

### 1.4 CLI 层 `cli.py`

- [ ] `membox ingest` — 读文本，调 agent.ingest
- [ ] `membox ingest-file` — 读文件，调 agent.ingest
- [ ] `membox query` — 传问题，调 agent.query
- [ ] `membox list-entities` — 调 agent.list_entities，rich 表格输出
- [ ] `membox list-relations` — 调 agent.list_relations，rich 表格输出
- [ ] 所有命令支持 `--db` / `--help`

### 1.5 导出 `__init__.py`

- [ ] 导出 `MemoryAgent`, `OpenAIExtractor`, `OpenAIEmbedder` 等公开 API

**验证**：
- `uv run membox --help` 输出完整命令列表
- `uv run mypy src` 零错误（所有签名类型完整）
- `uv run pytest tests/` — skeleton 测试通过（stub 不崩溃）
- **import 链从 cli → agent → store/extract/embed 全部串通**

## Phase 2 — 存储实现

**目标**：填充 `KnowledgeStore` 中所有 stub 方法的真实实现。

- [ ] 建表 DDL（entities, entity_aliases, relations, documents, relation_evidence）
- [ ] `PRAGMA foreign_keys=ON` + WAL 模式
- [ ] 实体 CRUD：`insert_entity` / `find_entity_by_name` / `list_entities`
- [ ] 别名 CRUD：`add_alias` / `find_entity_by_alias`
- [ ] 关系 CRUD：`insert_relation`（UNIQUE 去重）/ `list_relations`
- [ ] 文档 CRUD：`insert_document` / `get_document`
- [ ] 证据 CRUD：`add_evidence` / `get_evidence_for_relation`
- [ ] 测试：建表、外键约束、三元组 UNIQUE 去重、evidence 多对多

**验证**：CLI 命令 `membox ingest "test"` 能写入 SQLite，`membox list-entities` 能读出。

## Phase 3 — 谓词归一化实现

**目标**：填充 `normalize_predicate()` 的真实实现。

- [ ] 内置中英文同义词字典（`developed`/`develop`/`开发` → `develops` 等）
- [ ] lowercase + 字典查找，未命中则原样返回 lowercase
- [ ] 测试：中英文同义词 + 未知谓词 pass-through

**验证**：`membox ingest "A 开发了 B"` → 关系谓词存储为 `develops`。

## Phase 4 — 实体消歧实现

**目标**：填充 `find_or_create_entity()` 的三层级联消歧。

- [ ] 别名精确匹配
- [ ] 同类型 embedding cosine ≥ 0.85 匹配
- [ ] 新建
- [ ] 字符串回退（无 embedder 时：精确 + 大小写归一化）
- [ ] 测试
  - [ ] 字符串精确去重 / 大小写去重
  - [ ] embedding 同义词去重
  - [ ] 反例：无关实体不合并
  - [ ] 并发同名实体（8 线程同时 find_or_create，最终只有 1 条）

**验证**：重复 ingest 同一实体不会创建多条记录。

## Phase 5 — 多跳检索实现

**目标**：填充 `bfs_query()` 的真实实现。

- [ ] BFS 从种子实体扩展，`max_hops` 可调
- [ ] 每跳记录路径、关联实体、关系、证据原文
- [ ] 测试
  - [ ] 2-hop 召回 C、不召回 D
  - [ ] 3-hop 召回 D
  - [ ] 上下文聚合（路径完整还原）
  - [ ] 溯源原文还原

**验证**：`membox query "X 和 Y 是什么关系？" --max-hops 2` 返回带溯源的结果。

## Phase 6 — 并发安全加固

**目标**：多 agent 同时写入不冲突。

- [ ] per-thread SQLite connection（`threading.local()`）
- [ ] WAL 模式已开启（Phase 2）
- [ ] `RLock` 守护 `find_or_create_entity` 临界区
- [ ] 测试
  - [ ] 多线程并发写入（5 线程 × 10 写入，无错误，计数精确）
  - [ ] 并发同名实体（Phase 4 已有，此处验证 RLock 正确性）

**验证**：并发测试无错误、无死锁。

## Phase 7 — OpenAI 集成

**目标**：接入真实 LLM，替换 Dummy 实现。

- [ ] `src/membox/extract.py` — `OpenAIExtractor` 实现
- [ ] `src/membox/embed.py` — `OpenAIEmbedder` 实现
- [ ] `examples/demo.py` — 端到端 demo 脚本
- [ ] 手动验证：灌入真实文档 → 查询返回有意义的结果

**验证**：`OPENAI_API_KEY=sk-... uv run python examples/demo.py` 跑通。

## Phase 8 — 代码结构分析（tree-sitter）

**目标**：通过 AST 解析提取代码结构化知识。

- [ ] `src/membox/ast_parser.py`
  - [ ] tree-sitter 集成，按需加载语言 grammar
  - [ ] 提取结构三元组：`module --defines--> class` / `class --has_method--> method` / `method --calls--> function`
  - [ ] CLI 命令：`membox analyze-src <path> --language <lang>`
- [ ] 先支持 Python grammar
- [ ] 测试：Python 源文件解析 / 模块依赖图 / class 结构

**验证**：对自身代码库运行 `membox analyze-src src/`，查询能召回模块结构。

## Phase 9 — Skill 文件

**目标**：为 coding agent 编写 skill 指令文件。

- [ ] `skills/membox-skill.md` — 通用 skill 模板
  - [ ] 安装说明
  - [ ] 命令参考
  - [ ] 使用场景示例
- [ ] 手动验证：agent 读取 skill 后能正确调用 CLI

**验证**：skill 注入 agent 上下文，agent 能自主完成 ingest + query。

## Phase 10 — 打磨与发布

- [ ] README.md 更新
- [ ] 文档补全（docstring 覆盖所有公开 API）
- [ ] 覆盖率达标（≥ 80%）
- [ ] `uv run mypy src` 零错误
- [ ] `uv run ruff check .` 零警告
- [ ] 版本号更新

---

## 构建顺序

```
Phase 0 骨架 ✅
    │
Phase 1 完整框架（接口先行，所有模块 stub 串通）
    │
    ├→ Phase 2 存储实现
    ├→ Phase 3 归一化实现
    ├→ Phase 4 消歧实现 ──→ Phase 6 并发加固
    ├→ Phase 5 多跳检索实现
    │
    └→ Phase 7 OpenAI 集成
         │
         ├→ Phase 8 tree-sitter（可并行）
         ├→ Phase 9 Skill 文件（可并行）
              │
              └→ Phase 10 发布
```

**原则**：Phase 1 之后，每个 Phase 只做一件事——**填充 Phase 1 预留的 stub**。不改签名，不改 import，不改架构。
