# Membox — 实现路线图

> 基于 [spec.md](./spec.md) 拆解，每阶段产出可运行、可测试的增量。

## Phase 0 — 项目骨架 ✅

已有脚手架：uv + ruff + mypy + pytest + pre-commit + CI。

- [x] `pyproject.toml` 配置
- [x] pre-commit hooks
- [x] GitHub Actions CI
- [x] 项目目录结构
- [ ] 运行时依赖：`typer`, `rich`, `pydantic` 加入 pyproject.toml
- [ ] 可选依赖组：`openai`, `tree-sitter`

## Phase 1 — 数据模型与存储层

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

## Phase 2 — 谓词归一化

**目标**：语义相同的谓词归一化为标准形式。

- [ ] `src/membox/normalize.py`
  - [ ] `normalize_predicate(predicate: str) -> str`
  - [ ] 内置中英文同义词字典（`developed`/`develop`/`开发` → `develops` 等）
  - [ ] lowercase + 字典查找，未命中则原样返回 lowercase
- [ ] 测试：谓词归一化各种 case

**验证**：单元测试覆盖中英文同义词 + 未知谓词 pass-through。

## Phase 3 — 实体消歧

**目标**：同名/近义实体自动合并，不重复创建。

- [ ] `src/membox/embed.py` — Embedding Protocol
  - [ ] `Embedder` Protocol：`embed(text: str) -> list[float]`
  - [ ] `DummyEmbedder`：测试用假实现
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

## Phase 4 — LLM 知识提取

**目标**：从自然语言文档中自动提取三元组。

- [ ] `src/membox/extract.py`
  - [ ] `LLMExtractor` Protocol：`extract(text: str) -> list[Triple]`
  - [ ] `OpenAIExtractor` 实现（调用 OpenAI API，返回结构化三元组）
  - [ ] `DummyExtractor`：测试用假实现（规则提取或固定返回）
- [ ] `src/membox/schema.py` 补充 `Triple` 模型
- [ ] 测试
  - [ ] DummyExtractor 提取验证
  - [ ] 提取结果入库（与 Phase 3 消歧联动）
  - [ ] evidence 正确挂载到 relation

**验证**：用 DummyExtractor 端到端跑通 ingest，无 API 调用。

## Phase 5 — 多跳检索

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

## Phase 6 — MemoryAgent 统一入口

**目标**：封装完整 ingest → query 流程，对外暴露简洁 Python API。

- [ ] `src/membox/agent.py` — `MemoryAgent` 类
  - [ ] `__init__(extractor, embedder, db_path)`
  - [ ] `ingest(text, source=None)` — 提取三元组 → 消歧 → 入库
  - [ ] `query(question, max_hops=2)` — 种子定位 → BFS → 组装 prompt
  - [ ] `list_entities()` / `list_relations()`
- [ ] `src/membox/__init__.py` — 导出公开 API
- [ ] 集成测试（用 DummyExtractor + DummyEmbedder）
  - [ ] ingest → query 端到端
  - [ ] 并发写入（5 线程 × 10 写入，无错误，计数精确）

**验证**：`uv run pytest tests/ -v` 全部绿，零外部依赖。

## Phase 7 — CLI 层

**目标**：用 typer 暴露 CLI 命令，agent 通过 shell 调用使用。

- [ ] `src/membox/cli.py` — typer `app`
  - [ ] `membox ingest` — 摄入文本
  - [ ] `membox ingest-file` — 摄入文件
  - [ ] `membox query` — 查询记忆
  - [ ] `membox list-entities` — 列出实体
  - [ ] `membox list-relations` — 列出关系
  - [ ] 所有命令支持 `--db` / `--help`
- [ ] `pyproject.toml` 注册 `[project.scripts]` 入口点
- [ ] rich 格式化输出（表格展示实体/关系、高亮查询结果）
- [ ] 测试（使用 `typer.testing.CliRunner`）
  - [ ] 各命令 happy path
  - [ ] `--help` 输出正确
  - [ ] 错误处理（数据库不存在、空查询等）

**验证**：`uv run membox --help` 输出完整命令列表。

## Phase 8 — 并发安全加固

**目标**：多 agent 同时写入不冲突。

- [ ] per-thread SQLite connection（`threading.local()`）
- [ ] WAL 模式已开启（Phase 1）
- [ ] `RLock` 守护 `find_or_create_entity` 临界区
- [ ] 测试
  - [ ] 多线程并发写入
  - [ ] 并发同名实体（Phase 3 已有，此处验证 RLock 正确性）

**验证**：并发测试无错误、无死锁。

## Phase 9 — OpenAI 集成与 Live Demo

**目标**：接入真实 LLM，可跑端到端 demo。

- [ ] `src/membox/extract.py` — `OpenAIExtractor` 完整实现
- [ ] `src/membox/embed.py` — `OpenAIEmbedder` 完整实现
- [ ] `examples/demo.py` — 端到端 demo 脚本
- [ ] 手动验证：灌入真实文档 → 查询返回有意义的结果

**验证**：`OPENAI_API_KEY=sk-... uv run python examples/demo.py` 跑通。

## Phase 10 — Skill 文件

**目标**：为各 coding agent 编写 skill 指令文件，教会 agent 使用 `membox` CLI。

- [ ] `skills/membox-skill.md` — 通用 skill 模板
  - [ ] 安装说明（`pip install membox` 或 `uv tool install`）
  - [ ] 命令参考（ingest / query / list）
  - [ ] 使用场景示例（项目初始化时灌入文档、编码时查询架构决策）
- [ ] 适配特定 agent 的 skill 变体（如需）
- [ ] 手动验证：agent 读取 skill 后能正确调用 CLI

**验证**：将 skill 文件注入 agent 上下文，agent 能自主完成 ingest + query。

## Phase 11 — 代码结构分析（tree-sitter）

**目标**：通过 AST 解析提取代码结构化知识，补充纯文本 RAG 的不足。

- [ ] `src/membox/ast_parser.py`
  - [ ] tree-sitter 集成，按需加载语言 grammar
  - [ ] 提取结构三元组：`module --defines--> class` / `class --has_method--> method` / `method --calls--> function`
  - [ ] CLI 命令：`membox analyze-src <path> --language <lang>`
- [ ] 先支持 Python grammar，后续按需加 TypeScript / Go / Rust
- [ ] 提取结果直接入图谱（复用 Phase 1-6 的存储与消歧）
- [ ] 测试
  - [ ] Python 源文件解析
  - [ ] 模块依赖图提取
  - [ ] class 结构提取

**验证**：对自身代码库运行 `membox analyze-src src/`，查询能召回模块结构。

## Phase 12 — 打磨与发布

- [ ] README.md 更新（安装、CLI 用法、API 示例、skill 接入）
- [ ] 文档补全（docstring 覆盖所有公开 API）
- [ ] 覆盖率达标（≥ 80%）
- [ ] `uv run mypy src` 零错误
- [ ] `uv run ruff check .` 零警告
- [ ] 版本号更新（`0.1.0` → `1.0.0` 或 `0.1.0` 首个可用版）

---

## 依赖关系

```
Phase 0 ──→ Phase 1 ──→ Phase 2 ──→ Phase 3 ──→ Phase 4 ──→ Phase 6
                  │                                          │
                  └────→ Phase 5 ─────────────────────────────┘
                            │
                       Phase 7 (CLI)
                            │
                       Phase 8 (并发)
                            │
                       Phase 9 (OpenAI)
                            │
                       Phase 10 (Skill)
                       Phase 11 (AST，可与 10 并行)
                            │
                       Phase 12 (发布)
```

- Phase 0-6 是核心，必须顺序推进
- Phase 7（CLI）依赖 Phase 6
- Phase 8（并发）可在 Phase 3 之后穿插，不阻塞主线
- Phase 9（OpenAI）依赖 Phase 7（CLI 先就绪）
- Phase 10（Skill）和 Phase 11（AST）可并行，都依赖 Phase 9
- Phase 12 最后做
