# 数据集派生许可判定服务

为研究团队提供「数据集派生许可」的登记与判定能力。基于 **Python 3.11 + FastAPI + SQLite**，单容器即可运行，数据持久化在 Docker Volume 中。

## 判定规则

- **版本登记**：登记一个数据集版本时，可固定引用多个**已经存在**的父版本；父版本引用在创建之后不可修改（服务不提供任何修改/删除父引用的接口，重复登记同一版本号返回 `409`）。父引用构成一个不可变的 DAG。
- **许可变更**：可对任一已登记版本提交许可变更，记录其 **按 UTC 时刻生效** 的允许用途全集（后一次变更整体覆盖前一次，不做用途增量合并）。
- **判定口径**：查询「给定版本 + 用途 + UTC 时刻」时——
  1. 取该版本**自身及全部祖先**（沿固定父引用展开、自动去重，支持菱形继承）；
  2. 对其中每个版本，取**不晚于查询时刻的最近一次**许可变更作为该版本当时的有效许可；
  3. 对各版本的允许用途取**交集**；
  4. **任一版本在该时刻没有有效许可，或其有效许可不包含所申请用途，即拒绝**；
  5. 响应返回：`allowed` 可否使用、`basis` 各版本所依据的变更、`blockers` 所有阻断版本（含原因）、`allowed_uses_intersection` 交集。
- **幂等与冲突**：
  - 相同**变更标识**且语义内容（版本、生效时刻、允许用途集合；用途顺序/重复不影响）完全相同的重复提交，返回**同一结果**（HTTP `200`，`replayed=true`）；
  - 标识相同而内容不同 → `409 conflict`；
  - 同一版本在同一生效时刻出现不同变更 → `409 conflict`；
  - 所有时间必须**带时区**（如 `...Z`、`+08:00`），服务统一按 UTC 归一化；传入无时区时间返回 `422`。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与异常映射
│   ├── service.py     # 核心业务逻辑（登记 / 变更 / 判定 / 幂等冲突）
│   ├── database.py    # SQLite 连接与建表
│   ├── schemas.py     # 请求/响应模型
│   ├── seed.py        # 幂等的示例数据
│   └── config.py      # 环境变量配置
├── tests/test_api.py  # 端到端测试（pytest + TestClient）
├── docker/entrypoint.sh
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

前置：已安装 Docker 与 Compose 插件（`docker compose version` 可用）。

```bash
# 构建镜像、初始化依赖与数据库（首次启动自动建表并写入示例数据）并后台启动
docker compose up -d --build

# 查看日志
docker compose logs -f

# 停止
docker compose down            # 保留数据卷
docker compose down -v         # 同时删除数据卷（清空数据）
```

启动完成后（默认约 1–2 秒）：

| 入口 | 地址 |
| --- | --- |
| 服务 Base URL | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/health> |
| Swagger UI（可在线调用） | <http://localhost:8000/docs> |
| OpenAPI 文档 | <http://localhost:8000/openapi.json> |

> 宿主机端口由环境变量 `APP_PORT` 控制（默认 `8000`），容器内固定监听 `8000`。数据存放在命名卷 `license-data`（容器内 `/data`）。

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_HOST` | `0.0.0.0` | uvicorn 监听地址，一般无需修改 |
| `APP_PORT` | `8000` | **宿主机映射端口**（同时作为容器内监听端口）；改为如 `8080` 后访问 <http://localhost:8080> |
| `DB_PATH` | `/data/licenses.db` | SQLite 数据库文件路径（位于持久化卷内） |
| `INIT_DB` | `true` | 启动时自动建表（`CREATE TABLE IF NOT EXISTS`，幂等） |
| `SEED_DEMO` | `true` | 数据库为空时写入示例数据；对已有数据的数据库自动跳过。生产环境可设为 `false` |
| `CONFIG_ECHO` | `true` | 启动时在日志打印解析后的配置 |

临时改端口或关闭示例数据：

```bash
APP_PORT=8080 SEED_DEMO=false docker compose up -d --build
```

## HTTP API

### 1. 登记版本（固定父版本引用，不可修改）

`POST /versions`

```bash
curl -X POST http://localhost:8000/versions \
  -H 'Content-Type: application/json' \
  -d '{
    "version_id": "dataset-c-v1",
    "parent_ids": ["demo-root-a", "demo-root-b"],
    "note": "由两个上游数据集派生"
  }'
```

- `parent_ids` 中每个版本必须已存在，否则 `404`；
- `version_id` / `change_id` 省略时服务自动生成 UUID；
- 父引用创建后不可修改：再次提交相同 `version_id` 返回 `409`，且不存在任何编辑接口。

### 2. 提交许可变更（幂等）

`POST /license-changes`

```bash
curl -X POST http://localhost:8000/license-changes \
  -H 'Content-Type: application/json' \
  -d '{
    "change_id": "chg-c-1",
    "version_id": "dataset-c-v1",
    "effective_at": "2026-03-01T00:00:00Z",
    "allowed_uses": ["research", "review"]
  }'
```

- 首次创建返回 `201`；相同 `change_id` + 相同内容重放返回 `200` 且 `replayed=true`，`change` 内容与首次完全一致；
- 同标识不同内容、或同版本同生效时刻的不同变更返回 `409`；
- `allowed_uses` 是该时刻起允许用途的**全集**（覆盖语义）；用途去重、顺序无关。

### 3. 判定可否使用

`GET /permissions/check?version_id=...&use=...&at=...`

```bash
curl -s "http://localhost:8000/permissions/check?version_id=demo-derived-1&use=research&at=2026-03-01T00:00:00Z"
```

允许时：

```json
{
  "version_id": "demo-derived-1",
  "use": "research",
  "at": "2026-03-01T00:00:00Z",
  "allowed": true,
  "closure": ["demo-derived-1", "demo-root-a", "demo-root-b"],
  "basis": [ { "version_id": "...", "change_id": "...", "effective_at": "...", "allowed_uses": ["..."] } ],
  "blockers": [],
  "allowed_uses_intersection": ["research"]
}
```

拒绝时 `allowed=false`，`blockers` 列出**全部**阻断版本，`reason` 取值：

- `no_valid_license`：该版本在查询时刻没有任何不晚于该时刻的变更（包括尚未登记任何变更、或所有变更都在未来生效）；
- `use_not_allowed`：该版本的最近有效变更不包含所申请用途（同时回传所依据的 `change_id`、`effective_at` 与当时的 `allowed_uses`）。

被祖先拒绝的示例：

```bash
# demo-root-a 在 2026-06-01 起仅允许 research，故 7 月对 demo-derived-1 申请 review 被阻断
curl -s "http://localhost:8000/permissions/check?version_id=demo-derived-1&use=review&at=2026-07-01T00:00:00Z"
# -> allowed=false, blockers 含 demo-root-a / use_not_allowed
```

### 4. 查询接口

| 方法与路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `GET /versions` | 列出全部版本（含父引用与变更） |
| `GET /versions/{version_id}` | 版本详情，不存在返回 `404` |
| `GET /license-changes/{change_id}` | 按标识查询变更 |

错误响应统一形如：

```json
{ "error": "conflict", "message": "……", "details": { } }
```

## 内置示例数据（SEED_DEMO=true 时）

| 版本 | 父版本 | 变更 |
| --- | --- | --- |
| `demo-root-a` | — | 2026-01-01 起允许 `research, review`；2026-06-01 起仅 `research` |
| `demo-root-b` | — | 2026-01-01 起允许 `research, commercial` |
| `demo-derived-1` | a, b | 2026-02-01 起允许 `research, review, commercial`（实际许可 = 三者交集，仅 `research`） |
| `demo-no-license` | — | 无任何变更，始终以 `no_valid_license` 阻断 |

一键体验（启动后）：

```bash
bash examples/curl-examples.sh
```

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 初始化依赖即数据库建表在应用启动时自动完成（默认 DB_PATH=/data/licenses.db，
# 本地运行建议指向可写路径）：
DB_PATH=./data/licenses.db SEED_DEMO=true uvicorn app.main:app --reload --port 8000

# 运行测试
pip install -r requirements-dev.txt
pytest -q
```

测试覆盖：父引用存在性与不可变、祖先展开（含菱形去重）、用途交集、无有效许可阻断、最近变更生效、幂等重放、两类 409 冲突、时区校验与示例数据初始化。
