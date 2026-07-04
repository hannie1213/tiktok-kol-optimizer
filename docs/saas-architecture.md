# SaaS 化架构与商业化路线

本文档说明 dev 分支完成的「JSON 文件 → 数据库」多租户 SaaS 改造，以及面向 TikTok 商家真实商用的后续路线建议。

---

## 一、本次改造内容

### 1. 数据库持久层（替代每租户一组 JSON 文件）

```
┌─────────────┐   JWT (sub = tenant_id)   ┌──────────────────────────┐
│  商家浏览器  │ ────────────────────────→ │ FastAPI (多 worker)       │
└─────────────┘                           │  require_tenant 解析租户   │
                                          │  backend/repo.py          │
                                          │  所有查询按 tenant_id 隔离 │
                                          └────────────┬─────────────┘
                                                       │ SQLAlchemy 连接池
                                          ┌────────────▼─────────────┐
                                          │ SQLite（默认，零配置）      │
                                          │ PostgreSQL（生产，可横向扩） │
                                          └──────────────────────────┘
```

| 表 | 内容 | 隔离/索引 |
|---|---|---|
| `tenants` | 租户 + 初始种子库快照（供 Reset 恢复） | 主键 = tenant_id |
| `users` | 商家账号（email 唯一索引，pbkdf2 哈希） | email、tenant_id 索引 |
| `kols` | 达人库 | `(tenant_id, kol_id)` 唯一约束；`(tenant_id, country, category)` 复合索引，/optimize 的过滤直接走索引 |
| `kol_snapshots` | 达人指标历史（趋势图） | `(tenant_id, kol_id)` 索引 |
| `campaigns` | 营销活动（预测 vs 实际） | `(tenant_id, campaign_id)` 唯一约束 |
| `calibrations` | 每租户预测校准表 | 主键 = tenant_id |

- 分层：`backend/db.py`（引擎/模型）→ `backend/repo.py`（按租户隔离的数据访问）→ 各 API 模块。端点层与存储解耦，API 契约完全不变。
- **平滑迁移**：老部署 `data/tenants/{id}/*.json` 与 `data/users.json` 在首次访问时自动导入数据库，无需手工迁移脚本。

### 2. 多商家并发

- 写操作从「整库重写 JSON 文件」改为**行级 SQL**（新增/更新/删除单条达人、单条活动），不同商家的写入互不阻塞。
- PostgreSQL：每 worker 连接池（`DB_POOL_SIZE`/`DB_MAX_OVERFLOW`），`pool_pre_ping` + `pool_recycle` 防断连。
- SQLite（单机模式）：WAL 模式 + busy_timeout，读永不阻塞；进程内写锁将写请求排队而非报错。
- 优化算法为 CPU 密集型纯 Python → 用**多 worker 进程**横向扩并发（`WEB_CONCURRENCY≈CPU 核数`）；`CANDIDATE_CAP=80` 的预筛选保证单次 /optimize 的时延与库大小无关。
- 租户上下文用 `contextvars` 传递，请求间零共享状态；配合无状态 JWT，应用层可以直接开多副本 + 负载均衡。

### 3. 配置项

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `DATABASE_URL` | SQLAlchemy 连接串 | `sqlite:///data/kol_saas.db` |
| `JWT_SECRET` | 令牌签名密钥（**生产必填**） | 开发用不安全默认值 |
| `CORS_ORIGINS` | 允许的前端来源（逗号分隔） | `*`（仅开发） |
| `WEB_CONCURRENCY` | uvicorn worker 数（Docker） | 4 |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | 每 worker 连接池 | 10 / 20 |

### 4. 部署

```bash
# 单机试用（SQLite，零配置）
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 4

# 生产（PostgreSQL + 多 worker）
cp .env.example .env   # 设置 JWT_SECRET、POSTGRES_PASSWORD
docker compose up --build
```

横向扩展路径：多台 app 容器共享同一 PostgreSQL → 前置 Nginx/云 LB → 数据库读副本（本系统读远多于写）。

---

## 二、商业化路线建议

### 阶段 1：可收费的 MVP（当前架构已支撑）

1. **定价分层**（建议按「达人库容量 + 每月优化次数 + 席位」三个维度）：
   - Free：≤50 达人、10 次优化/月 —— 获客与自传播；
   - Pro（~$49/月）：≤1,000 达人、不限优化、CSV/Excel 导入导出、校准闭环；
   - Team（~$199/月）：多席位、多店铺、API 访问、优先支持。
2. **计费接入**：Stripe（东南亚可加 Xendit/2C2P 收当地货币）。在 `tenants` 表上加 `plan`、`period_end` 字段 + 一个用量表即可做配额（架构已预留：所有用量都天然按 tenant_id 统计）。
3. **安全加固**（上线前必做）：
   - `JWT_SECRET` 强随机值；`CORS_ORIGINS` 收紧到正式域名；全站 HTTPS；
   - 登录/注册接口加限流（如 slowapi 或网关层），防撞库；
   - 数据库每日备份（`pg_dump` + 对象存储）。

### 阶段 2：数据护城河 —— TikTok 官方 API 接入

代码中已有接入占位（`/api/connections`），建议顺序：

1. **TikTok Shop Partner API**（最高价值）：拉取真实订单/联盟 GMV → 活动实际 GMV **自动回填**，校准闭环不再依赖商家手填，预测准确率成为核心卖点；
2. **TikTok Creator Marketplace API**：自动同步达人画像/粉丝数据，替代 FastMoss 手工导出；
3. 第三方分析平台（AsiaKOL、Cube Asia 等）作补充。

> 申请 TikTok 开发者资质需要企业主体与合规审查，周期较长，建议尽早启动。

### 阶段 3：规模化运营

- **异步任务**：/optimize 目前同步执行（秒级）。接入真实 API 后的批量同步、超大库优化应改为任务队列（Redis + RQ/Celery/ARQ），前端轮询任务状态；
- **缓存**：达人列表/Top 榜单等读多写少的端点加 Redis 缓存（按 tenant_id + 参数做 key，写操作失效）;
- **可观测性**：结构化日志 + Sentry（错误）+ Prometheus/Grafana（QPS、P95 时延、连接池水位）；
- **合规**：处理东南亚商家数据需关注新加坡 PDPA、印尼 PDP 等；隐私政策、数据删除（删租户 = 按 tenant_id 级联删除，架构已支持）；
- **迁移管理**：表结构后续演进建议引入 Alembic（当前由 `create_all` 自动建表，首版够用）。

### 差异化卖点（对商家的一句话）

> 「六种算法竞争出最优达人组合，且每次活动的真实 GMV 会自动校准下一次预测 —— 用得越久越准」。校准数据是每个商家自己的私有资产（per-tenant calibration），这是竞品爬虫型工具没有的留存钩子。
