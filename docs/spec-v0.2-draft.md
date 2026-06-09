# Membox — 项目规格说明书 v0.2(草案)

> **版本**: 0.2.0-draft · **状态**: Draft — 待评审 · **许可**: MIT
> **取代**: spec.md 0.1.0(评审通过后本文件内容并入 `docs/spec.md`)
> **兼容性**: 0.1 的全部公开 API(`ingest` / `query` / `list-*`)与表结构保持可用;0.2 为增量扩展 + 一次受控 schema 迁移。

---

## 0. 修订动机

0.1 把 Membox 定位成"知识图谱 + RAG 检索层"。实际目标是一个**带生命周期的分层记忆系统**:

1. **两层记忆** — 全局(agent 的整体性、跨项目记忆)与项目(项目工作过程本身的记忆)两个层级,且支持把项目记忆中可复用的内容(行为习惯、解题方式、流程思路)**提炼**到全局层。
2. **项目代码地图(AST,扩展功能)** — 对项目构建结构化 code map,并通过 commit diff **增量更新**,无需全量重扫。
3. **自动化记忆维护** — 基于 session 和 hook 自动捕获,带**写入门控**(判断什么该记、什么不该记),并把记忆**反馈**到后续工作(被使用的强化、长期未用的衰减)。

0.1 缺少支撑以上目标的五个原语,本版补齐:

| 原语 | 服务目标 |
|---|---|
| Scope 分层(全局库 / 项目库) | 目标 1 |
| 记忆类型分类(fact / episode / procedure / preference) | 目标 1、3 |
| Provenance(来源种类 + 文件/commit/session 引用) | 目标 2、3 |
| 撤销 / 按来源失效 API | 目标 2 |
| 使用反馈元数据(use_count / last_used_at / confidence) | 目标 3 |

以及一个架构决策:**捕获 / 消化分离**(§7),化解"hook 必须快"与"LLM 抽取慢、零守护进程"之间的冲突。

---

## 1. 项目定位(继承 0.1,措辞修订)

Membox 是一个**本地化的分层记忆系统**,面向 coding agent(Claude Code、Cursor、Cline、Aider 等)提供统一的记忆服务。知识图谱与 RAG 是其检索机制,而非全部定位。

核心主张(不变):

- **Hands-on 实现** — 不依赖 Neo4j / Weaviate / Pinecone 等外部服务,全部逻辑 Python + SQLite 手写
- **CLI 优先** — 命令行工具交付;agent 通过 skill 文件 + hook 接入,无需 MCP / HTTP 服务
- **零外部服务、零守护进程** — SQLite 文件级存储
- **Agent 共享** — 多 agent 通过同一 SQLite 文件共享记忆(WAL + 跨进程安全)

新增主张:

- **记忆有生命周期** — 写入要过门控,使用会被强化,长期不用会衰减归档;记忆不是只增不减的日志
- **一切可溯源、可撤销** — 每条记忆知道自己从哪来(session / 文件 / commit / 提炼),派生事实可随来源失效

## 2. 核心概念

### 2.1 两层 Scope

| Scope | 存储位置 | 内容 | 生命周期 |
|---|---|---|---|
| **global** | `~/.membox/global.db` | agent 的整体性记忆:用户偏好、行为习惯、通用解题方式、跨项目流程与思路 | 跟随用户,长期 |
| **project** | `<project>/.membox/memory.db` | 本项目工作过程的记忆:架构事实、历史决策、踩坑记录、进行中的任务上下文 | 跟随仓库,项目删则记忆删 |

设计决策:**两个独立 DB 文件,而非单库加 scope 列**。理由:

- 项目记忆随仓库走(团队可选择 commit `.membox/` 或 gitignore),全局记忆绝不泄漏进仓库
- 生命周期天然隔离,无需跨 scope 的级联清理
- 与 Claude Code 的全局 / 项目 CLAUDE.md 两层先例一致,用户心智模型现成
- 复用 0.1 的 `KnowledgeStore`(一个实例一个文件),改动面最小

**项目库定位**:从 cwd 向上查找最近的 `.membox/memory.db`(类似 `.git` 发现);`--db` 显式传路径时绕过发现逻辑(兼容 0.1 用法)。`membox init` 在当前项目创建 `.membox/`。

**检索合并**:默认 `--scope all` —— 同时查询两库,结果标注来源层;冲突时项目层优先(更具体)。写入必须指定单一 scope(默认 project;`distill` 是唯一自动跨层写入的通道)。

### 2.2 记忆类型

| type | 含义 | 典型来源 | 主要去向 |
|---|---|---|---|
| `fact` | 客观事实:"X 项目用 Neo4j 存图" | 文档 ingest、codemap | 三元组图谱索引 |
| `episode` | 情节:一次调试过程、一次决策及其前因后果 | session hook 捕获 | 项目层;distill 的原料 |
| `procedure` | 流程 / 方法:"发布前先跑 X 再跑 Y"、解题套路 | distill 提炼、手动 | 全局层为主 |
| `preference` | 偏好 / 习惯:"该用户偏好 rebase 不用 merge" | distill 提炼、手动 | 全局层为主 |

数据模型含义:**`memories` 表是记忆的真相存储;entity/relation 三元组图谱降级为检索索引层**。fact 类记忆同时被抽取为三元组进图谱(0.1 行为不变);episode / procedure / preference 主要以记忆单元形态存在,可选择性挂少量索引实体(便于 BFS 召回),但不强行拆成三元组(避免程序性 / 情节性内容失真)。

### 2.3 Provenance(溯源)

每条记忆必须携带:

- `source_kind` — `manual` | `hook` | `file` | `commit` | `distill` | `ci`
- `source_ref` — 对应引用:文件路径、commit sha、session id、来源记忆 id 列表(distill)
- `created_at` / `updated_at`

价值:① 增量更新 — "文件 X 变了 → 撤销所有 `source_ref = X` 的派生事实再重建";② 提炼审计 — 全局层的 procedure 能链回它从哪些项目情节归纳而来;③ 信任分级 — 人工写入与 hook 自动捕获的置信度起点不同。

### 2.4 生命周期与反馈

```
captured(inbox) → gated(门控) → active → (使用强化 ←→ 衰减) → archived → 可清除
                       ↘ rejected(不入库,留审计计数)            ↗
                                  retracted(来源失效 / 手动撤销)
```

- **强化**:`recall` / `query` 命中并返回的记忆 `use_count += 1`、刷新 `last_used_at`
- **衰减**:`consolidate` 时执行 — `confidence` 按 `last_used_at` 距今时长衰减;低于阈值(默认 0.3)转 `archived`(不再参与默认检索,可查可恢复)
- **撤销**:`retracted` 状态保留行(审计),默认检索与图谱索引中剔除

## 3. 数据模型

### 3.1 迁移策略

- `PRAGMA user_version` 管理 schema 版本;`KnowledgeStore` 打开时自动执行向前迁移(0 → 2)
- 0.1 → 0.2:`documents` 表保留并扩展为 `memories`(`ALTER TABLE ... RENAME` + 加列,旧行默认 `type='fact', status='active', source_kind='manual'`);`entities` / `relations` / `entity_aliases` / `relation_evidence` 结构不变(`relation_evidence.doc_id` 语义变为指向 `memories.id`)
- 迁移在事务内执行,失败回滚;迁移前自动备份 `<db>.bak`

### 3.2 表结构(每个 scope 的 DB 同构)

```sql
-- 记忆单元(0.1 的 documents 升级而来;真相存储)
CREATE TABLE memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT    NOT NULL DEFAULT 'fact',
                  -- fact | episode | procedure | preference
    content       TEXT    NOT NULL,
    summary       TEXT    NOT NULL DEFAULT '',     -- 一句话摘要,供 recall 预览与去重
    source        TEXT    NOT NULL DEFAULT '',     -- 0.1 遗留字段,保留兼容
    source_kind   TEXT    NOT NULL DEFAULT 'manual',
                  -- manual | hook | file | commit | distill | ci
    source_ref    TEXT    NOT NULL DEFAULT '',     -- 文件路径 / commit sha / session id / 'mem:1,2,3'
    status        TEXT    NOT NULL DEFAULT 'active',
                  -- active | archived | retracted
    confidence    REAL    NOT NULL DEFAULT 1.0,
    use_count     INTEGER NOT NULL DEFAULT 0,
    last_used_at  TEXT,
    embedding     BLOB,                            -- 摘要向量,供语义去重与召回
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT
);
CREATE INDEX idx_mem_status      ON memories(status);
CREATE INDEX idx_mem_source_ref  ON memories(source_kind, source_ref);
CREATE INDEX idx_mem_type        ON memories(type);

-- 原始捕获收件箱(hook 写入,消化后标记;捕获/消化分离的载体)
CREATE TABLE inbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payload      TEXT    NOT NULL,                 -- 原始文本(observation)
    kind         TEXT    NOT NULL DEFAULT '',      -- 自由标签:tool_result / user_msg / outcome ...
    session_id   TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    digested_at  TEXT                              -- NULL = 待消化
);
CREATE INDEX idx_inbox_pending ON inbox(digested_at) WHERE digested_at IS NULL;

-- 实体 / 别名 / 关系 / 证据:同 0.1,关系证据指向 memories
-- (entities, entity_aliases, relations 三表 DDL 与 0.1 一致,从略)
CREATE TABLE relation_evidence (
    relation_id INTEGER NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
    memory_id   INTEGER NOT NULL REFERENCES memories(id)  ON DELETE CASCADE,
    PRIMARY KEY (relation_id, memory_id)
);
```

### 3.3 Code Map 库(扩展功能,独立文件)

AST 派生的代码地图是**可重算的缓存**,与经验性记忆生命周期不同,放独立文件 `<project>/.membox/codemap.db`,失效策略可以粗暴(文件变更即整文件重建)而不污染记忆层。

```sql
-- 图谱部分复用 entities/relations 同构 schema(predicate: defines / has_method / calls / imports)
-- 增量账本:
CREATE TABLE code_files (
    path          TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,          -- 文件内容 hash,跳过未变文件
    last_commit   TEXT NOT NULL DEFAULT '',
    indexed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE codemap_meta (
    key   TEXT PRIMARY KEY,               -- 'last_indexed_commit' 等
    value TEXT NOT NULL
);
```

增量更新协议(`membox codemap update`):

1. 读 `codemap_meta.last_indexed_commit`;无则等价于全量 `build`
2. `git diff --name-status <last>..HEAD` 取变更文件列表(支持 A/M/D/R)
3. 对每个 D/M/R-旧路径:撤销 codemap 库中所有 `source_ref = <path>` 的实体关系(整文件粒度)
4. 对每个 A/M/R-新路径:tree-sitter 重新解析入库,刷新 `code_files`
5. 更新 `last_indexed_commit`;全程不触碰 `memory.db`

代码图谱实体在 `recall` 时可与记忆层结果合并返回(标注 `source: codemap`),供 agent 查"模块结构 + 相关历史决策"一体化上下文。

## 4. 架构设计

### 4.1 模块布局(0.1 基础上增量)

```
src/membox/
├── schema.py        # +Memory, Observation, GateDecision, RecallResult 模型
├── store.py         # +memories/inbox CRUD、retract、reinforce、decay、迁移
├── extract.py       # 不变(Protocol + Dummy + OpenAI)
├── embed.py         # 不变
├── normalize.py     # 不变
├── gate.py          # 新:写入门控(SalienceGate Protocol + LLM/启发式实现)
├── distill.py       # 新:项目层 → 全局层提炼
├── lifecycle.py     # 新:consolidate 编排(消化 inbox、衰减、归档)
├── scopes.py        # 新:scope 解析(全局路径、项目发现、双库句柄)
├── agent.py         # MemoryAgent 扩展:observe/recall/consolidate/distill 入口
├── cli.py           # 新命令注册;仍然 presentation-only
├── ast_parser.py    # tree-sitter 解析(Phase 8',输出结构三元组)
└── codemap.py       # 新:codemap build/update 编排(git diff 增量)
```

分层原则不变:storage / LLM 调用 / 编排 / CLI 严格分离。`gate` 与 `distill` 依赖注入 LLM(Protocol),测试用假实现。

### 4.2 关键设计决策(新增部分)

| 决策 | 选择 | 理由 |
|---|---|---|
| Scope 实现 | 双 DB 文件 | 生命周期隔离、随仓库走、零跨层泄漏(§2.1) |
| 真相存储 | `memories` 表;图谱为索引 | 程序性/情节性记忆不强拆三元组 |
| hook 延迟 | 捕获/消化分离(inbox) | hook 路径零 LLM 调用,<50ms 返回(§7) |
| 消化时机 | 机会主义:SessionStart / 显式 consolidate | 守住零守护进程约束 |
| 写入门控 | `SalienceGate` Protocol | 可注入 LLM 门控或纯启发式;测试不依赖 API |
| 撤销粒度 | 按 `(source_kind, source_ref)` 或按 id | 支撑 diff 增量与人工纠错 |
| codemap 隔离 | 独立 `codemap.db` | 可重算缓存不污染记忆生命周期 |
| schema 演进 | `PRAGMA user_version` + 自动迁移 + `.bak` | 用户无感升级,失败可回滚 |

## 5. 接口设计

### 5.1 CLI 命令面(0.1 命令全部保留,新增如下)

```bash
# ---- scope 与初始化 ----
membox init                                  # 在当前项目创建 .membox/
# 所有命令接受 --scope {project|global|all};写命令默认 project,读命令默认 all
# --db 显式路径仍然可用(0.1 兼容,绕过 scope 发现)

# ---- 捕获(hook 调用;廉价,无 LLM)----
membox observe "<原始观察文本>" --kind outcome --session <id>

# ---- 消化(机会主义触发或手动)----
membox consolidate [--scope project] [--dry-run]
#   1) 取 inbox 未消化批次 → LLM 抽取候选记忆
#   2) SalienceGate 逐条判定: save / merge(并入已有记忆) / skip
#   3) fact 类同时抽三元组入图谱索引
#   4) 执行衰减与归档
#   --dry-run 打印门控决策不落库

# ---- 检索(带反馈)----
membox recall "<当前任务上下文>" [--scope all] [--types fact,procedure] [--budget 2000]
#   双库召回(别名→embedding→BFS 同 0.1)+ memories 语义召回,按 confidence×相关度排序,
#   组装成预算内的 prompt 上下文;命中项 use_count++ / last_used_at 刷新
#   (0.1 的 membox query 保留为 fact 图谱专用检索,内部走同一通道)

# ---- 提炼 ----
membox distill [--dry-run] [--min-episodes 3]
#   扫描项目层 episode/procedure → LLM 归纳可泛化的 procedure/preference
#   → 写入全局层,source_kind=distill,source_ref=来源记忆 id 列表
#   --dry-run 输出候选供人工确认;默认要求确认后写入

# ---- 撤销与维护 ----
membox retract --id <N> | --source <ref> [--kind file]   # 状态置 retracted,联动剔除图谱索引
membox forget --archived --older-than 90d                 # 物理清除归档项(显式、可选)

# ---- Code Map(扩展)----
membox codemap build <path> [--language python]
membox codemap update [--since <sha>]        # 默认从 last_indexed_commit 起
membox codemap query "<问题>"                # 也并入 recall --scope all 的结果
```

### 5.2 Python API(增量)

```python
agent = MemoryAgent(extractor=..., embedder=..., gate=...,   # gate 可选,默认启发式
                    scope="auto")  # auto = 项目发现 + 全局;或显式 db_path(0.1 兼容)

agent.observe(text, kind="", session_id="") -> int            # inbox append
agent.consolidate(dry_run=False) -> ConsolidateReport         # 消化+门控+衰减
agent.recall(context, scope="all", types=None, budget=2000) -> RecallResult
agent.distill(dry_run=True) -> list[DistillCandidate]
agent.retract(memory_id=None, source_ref=None) -> int
# 0.1 的 ingest/query/list_* 全部保留,语义不变
```

### 5.3 SalienceGate Protocol

```python
class SalienceGate(Protocol):
    def judge(self, candidate: Memory, similar: list[Memory]) -> GateDecision: ...
    # GateDecision: action ∈ {save, merge, skip} + target_id(merge 时) + reason
```

门控判据(LLM 实现的 prompt 准则;启发式实现取可计算子集):

1. **新颖性** — 与已有记忆(embedding top-k)语义重复 → merge 或 skip
2. **可复用性** — 一次性细节(临时路径、当次报错文本)→ skip;可泛化结论 → save
3. **时效性** — 描述瞬态状态的(“正在跑 CI”)→ skip
4. **可执行性** — preference/procedure 必须包含"下次怎么做"才 save
5. 默认怀疑:判不准 → skip(宁漏勿滥,错过的还会再发生,垃圾记忆污染检索)

## 6. Hook 集成(目标 3 的接入故事)

0.1 的接入故事只有"skill 文件教 agent 调 CLI"——依赖 agent 记得调用,恰是记忆系统最不该依赖的。0.2 以 hook 为主、skill 为辅:

| Hook 时机 | 调用 | 延迟预算 |
|---|---|---|
| SessionStart | `membox recall "<项目名+分支+近期任务>" --budget 2000` 注入上下文;随后机会主义 `membox consolidate`(inbox 积压超阈值时) | recall <200ms;consolidate 后台可慢 |
| Stop / 任务完成 | `membox observe "<本轮结论/教训摘要>" --kind outcome` | <50ms(纯 SQLite append) |
| SessionEnd | `membox observe --kind session_summary` | <50ms |
| PostToolUse(可选,默认关) | 高信号工具结果摘要 observe | <50ms |

要点:**hook 路径上没有任何 LLM 调用**——observe 是纯 append;consolidate 含 LLM 但放在 SessionStart 这种用户本来就在等待初始化的时机,且可被 `--dry-run` / 配置关停。skill 文件(Phase 9)仍提供:教 agent 主动 `recall` 补充上下文、手动 `observe` 重要结论。

**反馈闭环**:recall 注入 → 记忆被用 → use_count/last_used_at 强化 → 排序上浮;未被召回的逐次衰减 → 归档。门控的 reject 计数保留,供调参审计。

## 7. 质量要求(0.1 基础上新增)

测试新增场景(继续零外部 API,gate/extract/embed 全用假实现):

- **scope 隔离** — 项目库写入不出现在全局库;`--scope all` 合并去重;项目发现(子目录向上查找)
- **门控** — save/merge/skip 三路径;merge 后原记忆 evidence 合并;默认怀疑路径
- **生命周期** — 强化计数;衰减到阈值转 archived;archived 不进默认召回;retract 后图谱索引同步剔除
- **撤销** — 按 source_ref 批量撤销;relation 失去全部 evidence 后不再被召回
- **迁移** — 0.1 库文件打开自动迁到 0.2,数据无损,`.bak` 生成
- **inbox** — observe 并发 append(多进程);consolidate 幂等(重复跑不重复入库)
- **codemap 增量** — build 后改一个文件 + commit → update 只重解析该文件;删除文件 → 其实体关系消失
- **跨进程安全** — find_or_create 在多进程下无 IntegrityError 泄漏(0.1 评审已发现,修复随 phases 1-7)

覆盖率 ≥ 80%、mypy strict、ruff 全量 —— 不变。

## 8. Roadmap 重排建议(评审通过后写入 roadmap.md)

```
Phase 8'  存储演进:memories/inbox 表 + 迁移 + retract/reinforce/decay + scopes 双库     ← 一切的地基
Phase 9'  捕获与消化:observe + consolidate + SalienceGate(启发式 + LLM 双实现)
Phase 10' 检索与反馈:recall(双库合并、预算、强化)+ 衰减归档闭环
Phase 11' 提炼:distill(项目 episode → 全局 procedure/preference,dry-run 确认制)
Phase 12' Hook 接入 + skill 文件(原 Phase 9 扩展:hooks 配置样例 + 文档)
Phase 13' Code Map:ast_parser + codemap build/update(原 Phase 8,改为增量优先设计)
Phase 14' 打磨发布(原 Phase 10)
```

排序原则:**schema 原语先行**(8' 加列便宜,做完 13' 再加要二次迁移);AST 后置(它是扩展功能,且依赖 8' 的 provenance/retract 原语才能做增量)。

## 9. 开放问题(需要决策)

1. **consolidate 的 LLM 成本控制** — inbox 批量消化按 token 预算分批?积压超限时丢弃最旧原始观察(原始观察可丢,已消化记忆不可)?(建议:保留最近 N 条 + 总 token 上限,超限先进先出丢弃)
2. **`.membox/` 是否默认 gitignore** — 团队共享项目记忆(commit)vs 个人记忆(ignore)。(建议:`membox init` 默认写入 `.gitignore`,提供 `--shared` 选项不写)
3. **全局库的并发** — 多项目多 session 同时写全局库,0.1 的跨进程安全修复必须先合入。(依赖项,非问题)
4. **recall 排序公式** — `score = α·相关度 + β·confidence + γ·recency` 的权重定多少?(建议:先 0.6/0.25/0.15 起步,门控 reject 审计数据攒够后再调)
5. **codemap 支持语言顺序** — Python 先行(自举),其后 TS/Go?(按用户实际项目分布定)
