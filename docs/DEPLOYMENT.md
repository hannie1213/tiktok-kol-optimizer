# 上线部署手册

面向运营者：把 tiko 部署到一台公网服务器并安全地对外提供服务。架构背景见
[saas-architecture.md](saas-architecture.md)。

## 1. 准备

- 一台 Linux 服务器（建议 ≥ 2 核 4 GB），已安装 Docker 与 Docker Compose v2
- 一个域名，A 记录已指向服务器 IP；放行 80 / 443 端口
- （可选）DeepSeek API Key —— 不配置时 AI 助手不可用，其余功能正常

## 2. 配置

```bash
cp .env.example .env
```

生成密钥（每个密钥单独生成一次）：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`.env` 中必须填写：

| 变量 | 说明 |
|---|---|
| `JWT_SECRET` | 登录令牌签名密钥，至少 32 位。更换后所有用户需重新登录 |
| `POSTGRES_PASSWORD` | 内置 PostgreSQL 的密码 |
| `ADMIN_KEY` | 运营后台（`/admin.html`）与开通套餐的密钥，至少 16 位；留空则后台完全关闭 |
| `DOMAIN` | 对外域名，例如 `app.example.com`（使用内置 HTTPS 代理时必填） |
| `DEEPSEEK_API_KEY` | 可选，AI 助手 |
| `SMTP_*`、`APP_BASE_URL` | 可选但强烈建议，邮件能力，见下方「邮件」 |

`docker-compose.yml` 已固定 `APP_ENV=production`。生产模式下，以下任一情况应用会**拒绝启动**并在日志里说明原因：

- `JWT_SECRET` 未设置或过短
- 设置了 `DEFAULT_TENANT`（会绕过登录）
- `CORS_ORIGINS` 含 `*`
- 开启了 `BILLING_SELF_SERVE`（任何人可免费切到付费套餐）
- `ADMIN_KEY` 短于 16 位
- 配置了 `SMTP_HOST` 却没有 `APP_BASE_URL`

### 邮件

配置任意 SMTP 中继即可（Resend、Brevo、Mailgun、Amazon SES、腾讯 / 阿里企业邮箱等）：

```
SMTP_HOST=smtp.resend.com
SMTP_PORT=587
SMTP_USER=resend
SMTP_PASSWORD=你的密钥
SMTP_FROM=tiko <no-reply@你的域名>
APP_BASE_URL=https://你的域名
```

开启后自动获得：注册时发送邮箱验证链接、登录框「忘记密码」自助重置（链接 30 分钟有效、
一次性）、修改密码后的安全提醒、套餐开通 / 变更通知。邮件语言跟随用户界面语言（中 / 英）。

要点：

- 发件域名需在服务商处完成 **SPF / DKIM** 验证，否则邮件大概率进垃圾箱
- `APP_BASE_URL` 是邮件内链接的域名，必须是用户实际访问的地址
- 不配置 SMTP 时产品仍可用：注册不发验证邮件，找回密码退回到运营后台人工重置
- 邮箱验证目前是「软验证」：未验证也能使用，只在账号页提示

### 扫码收款

用户在「套餐与账单」点升级 → 页面显示你的收款码、应付金额和订单号 → 用户扫码付款后填写
交易参考号提交 → 你在运营后台「待确认付款」里点「确认到账」→ 套餐自动开通并邮件通知用户。

- **收款码图片**：`frontend/pay/duitnow-qr.png`。换码时直接替换这个文件；删除它则关闭扫码
  收款，升级按钮退回到 WhatsApp / 微信联系方式
- **`PAY_PAYEE_NAME`**：显示在二维码旁的收款人姓名，方便付款人核对
- **`PAY_FX_RATE`**：套餐标价是美元，订单按此汇率折算成 `PAY_CURRENCY`（默认 MYR，汇率默认 4.4，
  **请按你想收的价格自行设定**）
- **`OPERATOR_EMAIL`**：用户提交付款后通知你的邮箱（需已配置 SMTP）

为什么金额带零头：静态收款码无法通知服务器到账，所以每笔订单会在标价基础上减去 1–49 分，
保证同一时间内所有待付订单金额互不相同。你在钱包收款记录里**按金额**就能对上是哪一单，
再核对一下用户填的参考号即可。

付费周期：1 / 3 / 12 个月（每月按 30 天计）。到期后自动降回免费版；到期前续费会在原到期日
基础上顺延。后台手动设置的套餐没有到期时间。未付款的订单 24 小时后自动失效。

## 3. 启动

```bash
docker compose --profile https up -d --build
```

Caddy 会自动为 `DOMAIN` 申请并续期 HTTPS 证书。应用容器只监听 `127.0.0.1:8000`，
不直接暴露公网。如果你已有 nginx / 云负载均衡负责 TLS，去掉 `--profile https`，
把流量转发到 `127.0.0.1:8000` 即可（需透传 `X-Forwarded-For`，限流依赖真实客户端 IP）。

检查：

```bash
docker compose ps
```

```bash
curl -s https://你的域名/health
```

返回 `{"status":"ok","database":"ok"}` 即正常。数据库表在首次启动时自动创建。

## 4. 上线前检查清单

- [ ] 用浏览器注册一个账号，跑一次优化，确认结果正常
- [ ] 打开 `https://你的域名/admin.html`，用 `ADMIN_KEY` 登录
- [ ] 把 `frontend/index.html` 中 `PAY_CONTACT` 的 WhatsApp / 微信号确认为你的真实收款联系方式
- [ ] 通读并按你的主体信息修改 `frontend/legal.html`（服务条款与隐私政策为通用模板，
      上线前请结合当地法律审阅，补充运营主体名称与联系方式）
- [ ] 配置数据库定时备份（见第 6 节）
- [ ] `.env` 仅保存在服务器上，不要提交到 Git

## 5. 日常运营

**确认扫码付款**：运营后台顶部「待确认付款」→ 在钱包 App 的收款记录里找到同金额的入账 →
「确认到账」。找不到就「驳回」并写明原因，用户可改正后重新提交。同一笔订单不会被重复开通。

**手动开通套餐**（其他渠道收款时）：运营后台 → 找到用户 → 选择套餐 → 应用（无到期时间）。
也可用命令行：

```bash
curl -X POST https://你的域名/billing/grant -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" -d '{"email":"merchant@shop.com","plan":"pro"}'
```

用户无法自助切到付费套餐（接口返回 402），只能自助降级到免费版。

**用户忘记密码**：已配置邮件时用户可在登录框自助重置，无需你介入。未配置邮件、或用户收不到邮件时：先通过对方注册时的联系方式核实身份 → 运营后台 → 该用户 → 「重置密码」
→ 把一次性临时密码发给对方，并提醒其登录后在「套餐与账单 → 账号」中立即修改。
重置会让该用户所有已登录会话失效。

**演示账号**：`@demo.tiko` 账号的令牌 24 小时过期，账号创建 48 小时后自动清理；
后台的「清理演示账号」可立即全部清除。

**限流**（按 IP 或账号，超限返回 429）：

| 接口 | 上限 |
|---|---|
| 登录 | 每 IP 20 次 / 5 分钟；每邮箱 10 次 / 15 分钟 |
| 注册、一键演示 | 每 IP 10 次 / 小时 |
| 优化、方案、规模模拟 | 每账号 30 次 / 分钟 |
| AI 助手 | 每账号 20 条 / 分钟 |
| 管理密钥错误 | 每 IP 10 次 / 15 分钟 |
| 找回密码邮件 | 每 IP 10 次 / 15 分钟；每邮箱 3 封 / 小时 |
| 重发验证邮件 | 每账号 3 封 / 小时 |

限流计数保存在各 worker 进程内存中：多 worker 时实际上限最多为表中数值 × worker 数。
套餐的月度配额不受此影响，由数据库精确计数。

## 6. 备份与恢复

备份（建议用 cron 每日执行，并把文件同步到服务器之外）：

```bash
docker compose exec -T db pg_dump -U kol kol_saas | gzip > backup_$(date +%F).sql.gz
```

恢复到空库：

```bash
gunzip -c backup_2026-11-12.sql.gz | docker compose exec -T db psql -U kol kol_saas
```

## 7. 升级

```bash
git pull
```

```bash
docker compose --profile https up -d --build
```

新增的表与字段在启动时自动补齐，无需手动迁移。升级前先做一次备份。

## 8. 排障

```bash
docker compose logs -f app
```

- 启动即退出、日志含 `Refusing to start with APP_ENV=production` → 按提示修正 `.env`
- 每个响应都带 `X-Request-ID`；用户反馈 500 错误时提供该 ID，可在日志中按 `rid=` 定位完整堆栈
- `/health` 返回 503 → 数据库不可达，检查 `db` 容器

## 9. 已知限制（上线后可按需补齐）

- **支付**：扫码付款后仍需你在后台点一次确认（静态收款码无法自动回调）。要做到全自动，
  需要接入有回调的支付网关，接入点见 `backend/payments.py`
- **到期提醒**：套餐到期前不会自动发邮件提醒续费
- **团队席位**：套餐里的「席位数」目前只是展示，一个工作区仍对应一个登录账号
- **限流存储**：进程内存，不跨 worker / 不跨机器共享；多机部署时应换成 Redis
- **单机 SQLite 模式**（不设 `DATABASE_URL`）：仅适合单 worker 小规模试用，
  需挂载 `/app/state` 卷，否则重建容器会丢数据
