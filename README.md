# 生物多样性调查数据 API

基于 **FastAPI + SQLite（SQLAlchemy 2.x）+ Docker Compose** 的调查数据管理服务。
研究员上传观测记录（样地、UTC 日期、原始物种名、数量）和版本化分类表
（名称、上位类群、接受名、同物异名）；发布调查批次时按分类表解析名称，
遇到环路、未知名或同名对应多个接受名则**整批待核**；合法记录按
**样地 + 接受种**汇总，保留原名与分类表版本。分类表修订只使受影响批次过期，
重发布成功后才替换结果，失败时继续提供上一份完整版本。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
```

启动后打开交互式文档：<http://localhost:8000/docs>
首次启动且数据库为空时自动写入演示数据（可用环境变量 `SEED_DEMO=0` 关闭）。
SQLite 数据文件保存在命名卷 `biodiversity-data` 中，容器重建不丢数据。

### 本地运行（无 Docker 时）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 自测
./scripts/selfcheck.sh        # 或 python3 -m pytest tests/ -v
```

## 核心业务规则

### 名称解析（`app/taxonomy.py`）

1. **未知名 unknown**：原始名（含各表行声明的同物异名）在分类表中找不到。
2. **同名歧义 ambiguous**：同一名称映射到多个不同接受名。
3. **环路 cycle**：沿 `accepted_name` 链向上解析时出现重复节点
   （自环 `A -> A` 视为稳定接受名，不算环路）。
4. 合法名称沿链解析到最终接受名；链尾指向表中不存在的名称（悬挂引用）时，
   以链尾名称本身作为接受名。
5. 名称匹配大小写/首尾空白不敏感；同一原始名在一批内只解析一次并复用结果。

### 批次状态机

| 状态 | 含义 |
| --- | --- |
| `DRAFT` | 草稿，从未成功发布，可继续上传观测 |
| `PUBLISHED` | 发布成功，结果有效 |
| `NEEDS_REVIEW` | 最近一次发布失败（整批待核），可补传数据后重发 |
| `STALE` | 分类表修订导致旧结果过期，**旧结果仍完整可查** |

- 发布时先在内存完成整批解析，**全部合法才写入结果**（单事务提交），
  失败则回滚，旧 `ResolvedObservation` 一行不动 → 始终提供上一份完整版本。
- 新分类表上传时，先按解析规则一次性算出新旧两版之间
  **最终接受种改变或变得无法解析的原始名集合**（链式解析使“间接改名”
  也会被捕获），再只用这些名称经集合查询定位仍引用上一版的批次，
  标为 `STALE` 并在响应中返回 `stale_batch_ids`；
  不再逐批读取明细、逐名重新解析。新版本与过期标记**同一事务提交**，
  JSON 与 CSV 上传共用这条路径。
- 重发布成功 → 批次的名称关联随明细一并替换并回到 `PUBLISHED`；
  重发布失败 → 保持待核，过期判定仍按上次成功发布的明细，
  旧结果继续可查。与重发交错时，以上传事务提交前已经生效的发布数据为准。

### 汇总口径

按 `plot_code + accepted_name` 分组，输出总数量、记录条数、
涉及的原始名集合与观测日期集合；每条解析结果都带
`taxonomy_version_id`，可追溯分类表版本。

## API 一览（共 12 个路径，详见 /docs）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/taxonomy/versions` | 上传新版分类表（JSON） |
| POST | `/taxonomy/versions/csv` | 上传新版分类表（CSV，`synonyms` 用 `;` 分隔） |
| GET | `/taxonomy/versions` | 分类表版本列表 |
| GET | `/taxonomy/versions/{id}` | 版本详情（含全部行） |
| POST | `/batches` | 创建调查批次 |
| GET | `/batches` / `/batches/{id}` | 批次列表/详情 |
| POST | `/batches/{id}/observations` | 上传观测记录（JSON 批量，重复自动跳过） |
| POST | `/batches/{id}/observations/csv` | 上传观测记录（CSV） |
| GET | `/batches/{id}/observations` | 查看原始观测 |
| POST | `/batches/{id}/publish` | 发布（`?taxonomy_version_id=` 可选，默认最新版） |
| GET | `/batches/{id}/issues` | 最近一次发布的核校问题 |
| GET | `/batches/{id}/results` | 批次结果（汇总 + 明细，STALE 也可查） |
| GET | `/results` | 跨批次查询，支持 `plot_code` / `accepted_name` / `include_stale` |

### 最小调用示例

```bash
# 1. 上传分类表（或用 examples/taxonomy_v1.csv 走 CSV 接口）
curl -X POST localhost:8000/taxonomy/versions -H 'Content-Type: application/json' -d '{
  "rows": [
    {"name":"喜鹊","higher_taxon":"雀形目","accepted_name":"Pica pica","synonyms":["Pica serica"]},
    {"name":"Pica pica","higher_taxon":"雀形目","accepted_name":"Pica pica","synonyms":[]}
  ]}'

# 2. 建批次、传观测、发布、查结果
curl -X POST localhost:8000/batches -H 'Content-Type: application/json' -d '{"name":"秋季调查"}'
curl -X POST localhost:8000/batches/1/observations -H 'Content-Type: application/json' -d '{
  "observations":[
    {"plot_code":"P-01","observed_on":"2026-11-01","raw_species_name":"喜鹊","quantity":3},
    {"plot_code":"P-01","observed_on":"2026-11-01","raw_species_name":"Pica serica","quantity":2}
  ]}'
curl -X POST localhost:8000/batches/1/publish
curl localhost:8000/batches/1/results
```

## 项目结构

```
app/
  config.py            环境配置（DATABASE_URL / SEED_DEMO）
  database.py          引擎、会话、建表
  models.py            ORM：分类表版本/行、批次、观测、解析结果、发布问题
  schemas.py           Pydantic 请求/响应模型（/docs 示例来源）
  taxonomy.py          名称索引与解析（unknown/ambiguous/cycle）、STALE 判定
  services.py          发布事务、结果汇总
  csv_io.py            CSV 解析
  routers_*.py         三类路由（分类表 / 批次观测 / 发布查询）
  seed.py              演示数据（正常批次、待核批次、草稿、修订后 STALE）
  main.py              FastAPI 入口
tests/                 27 个测试（11 个解析/差异单测 + 15 个 API 集成 + 1 个旧库升级）
examples/              可直接上传的 CSV 样例
scripts/selfcheck.sh   一键自测
Dockerfile, docker-compose.yml
```

## 已实现 / 已验证 / 未完成

### 已实现

- 观测记录与版本化分类表的 JSON / CSV 双通道上传，重复观测自动跳过。
- 名称解析全量规则：异名归并、未知名、同名歧义、环路（含自环特例）、
  悬挂引用、大小写/空白归一。
- 整批待核：任一记录无法解析则不产生任何结果，问题可经 `/issues` 查询。
- 按样地 + 接受种汇总，保留原始名集合、日期集合与分类表版本。
- 分类表修订的精准过期：只 STALE 受影响批次；过期判定先算版本间差异
  名称集合再定位批次（查询数与批次数解耦）；重发布成功才替换结果，
  失败保留上一份完整版本。
- 旧 SQLite 数据卷可直接升级：无 schema 变更，`init_db` 幂等，
  老库文件上的旧批次按新逻辑正确标记。
- 跨批次查询与过滤、健康检查、OpenAPI `/docs` 演示、启动演示数据。
- Docker Compose（命名卷持久化 + healthcheck）与本地运行两种方式。

### 已验证（27 个自动化测试 + 真实 uvicorn 冒烟，全部通过）

- 单测：异名/接受名链、未知名、歧义、有向环路、自环、悬挂引用、解析缓存；
  版本间差异计算（直接/间接改名、变得无法解析、新增名不追踪、规范化 key）。
- 集成：完整发布汇总流、重复跳过、三类待核场景、STALE 精准判定与旧结果保留、
  失败重发布不破坏旧结果、修复后重发布替换结果、全局过滤、CSV 双通道、
  状态机拦截（已发布批次拒绝再上传）、前置条件 4xx；
  **间接改名**（名称行未动、链上接受名变化）精准过期并返回 `stale_batch_ids`；
  **重发交错**（v1 发布 → v2 过期 → 重发成功/失败 → v3 上传）判定以提交前
  生效的发布数据为准；**版本与过期标记原子提交**（标记失败整体回滚）。
- 旧库升级：裸 SQL 构造的上一版数据卷，新代码启动后旧批次正确标记、
  旧结果保留、重发后明细与版本引用一并替换。
- 性能对照（200 批次 × 20 明细，上传新版）：旧实现 203 条 SQL、4400 次名单
  解析；新实现 6 条 SQL、解析量只随分类表规模增长，两者判定结果一致。
- 冒烟：真实进程下 `/health`、`/docs`、`/openapi.json` 均 200；
  演示种子呈现 PUBLISHED / NEEDS_REVIEW / DRAFT / STALE 四种状态；
  STALE 批次旧结果可查、重发布后接受名与版本号正确替换；
  JSON 与 CSV 上传均返回正确的 `stale_batch_ids`。

### 未完成 / 已知限制

- 当前环境没有 Docker daemon，`docker compose up` 未在本机实际执行，
  仅校验了 compose 文件结构与 Dockerfile 语法逻辑；请在有 Docker 的环境验证。
- 过期判定以“仍引用上一版（上传前的最新版）的批次”为范围：重发失败或
  中间版本未影响而停留在更早版本的批次，不会在后续每次上传时重复判定
  （它们已处于 STALE/NEEDS_REVIEW 或结果与上一版一致；成功重发后即回到
  最新判定路径）。
- 没有鉴权/多租户与分页（数据量为调查级，结果查询默认全量返回）。
- 未提供观测记录的删除/编辑接口（业务规则上 PUBLISHED/STALE 批次冻结，
  修订走新批次；如需更正待核批次数据，目前仅支持追加）。
- 未做并发发布压测；SQLite 单库适合单机/小团队场景，高并发应换 PostgreSQL
  （代码已按可替换 `DATABASE_URL` 编写，模型使用通用 SQLAlchemy 类型）。
