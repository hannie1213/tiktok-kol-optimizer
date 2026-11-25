# 部署上线指南（Render）

预计费用：约 **7.25 美元/月**（约 RM32）（Starter 实例 7 美元 + 1 GB 持久化磁盘 0.25 美元）。域名另算，约 10 美元/年。

## 一、第一次部署（约 10 分钟）

1. 用 GitHub 账号登录 <https://render.com>，绑定一张支付卡。
2. 点 **New → Blueprint**，选择 `tiktok-kol-optimizer` 仓库。Render 会读取仓库里的 `render.yaml`，自动创建：
   - 一个 Docker Web 服务 `kol-optimizer`（新加坡机房，离东南亚商家最近）
   - 一块挂载在 `/var/data` 的 1 GB 磁盘，用来保存数据库 `app.db`
   - 一个随机生成的 `JWT_SECRET`
3. 创建时会让你填几个留空的变量，先这样填：
   | 变量 | 现在填什么 |
   |---|---|
   | `PUBLIC_BASE_URL` | 先填 `https://kol-optimizer.onrender.com`（以 Render 给你的实际网址为准） |
   | `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | 暂时留空，见第三部分 |
4. 点 **Apply**，等 3 到 5 分钟构建完成。打开 Render 给的网址，注册一个账号试试。
5. 之后每次合并到 `master`，Render 会自动重新部署，数据库在磁盘上不会丢。

## 二、绑定自己的域名

1. 在 Cloudflare、Namecheap 或阿里云国际版买一个 `.com` 域名。
2. Render 服务页 → **Settings → Custom Domains → Add**，填你的域名，按提示在域名商那里加一条 CNAME 记录。HTTPS 证书 Render 会自动签发。
3. 把 `PUBLIC_BASE_URL` 改成 `https://你的域名`（重置密码邮件里的链接用的就是它）。

## 三、开通找回密码邮件（推荐 Resend，免费每月 3000 封）

没配置邮件时，系统不会报错，但重置链接只会写在 Render 的日志里，用户收不到。

1. 注册 <https://resend.com>，在 **Domains** 里添加你的域名并按提示加 DNS 记录（需要先有域名）。
2. 在 **API Keys** 创建一个密钥。
3. 在 Render 的 **Environment** 里填：
   | 变量 | 值 |
   |---|---|
   | `SMTP_HOST` | `smtp.resend.com` |
   | `SMTP_USER` | `resend` |
   | `SMTP_PASSWORD` | 你的 Resend API 密钥 |
   | `SMTP_FROM` | `KOL Optimizer <no-reply@你的域名>` |

## 四、备份

Render 付费磁盘每天自动快照，保留 7 天，可在 **Disks** 页面一键恢复。想额外留一份时，在服务的 **Shell** 里运行：

```sh
python -c "import sqlite3,datetime; s=sqlite3.connect('/var/data/app.db'); s.backup(sqlite3.connect(f'/var/data/backup-{datetime.date.today()}.db'))"
```

## 五、开通收款（个人收款码 + 后台审核）

价格用马币，用户扫你的个人收款码（DuitNow、Touch 'n Go 或银行 App）付款，然后在网站里提交交易号和截图，你在后台确认到账后点"通过"，套餐自动开通并按购买月数计算到期日。不需要公司，也没有手续费。

1. 在 Render 的 **Environment** 填：
   | 变量 | 值 |
   |---|---|
   | `ADMIN_EMAILS` | 你自己登录网站用的邮箱（多个用逗号分隔） |
   | `PAYMENT_PAYEE` | 收款码下方显示的名字，例如 `YUHAN (DuitNow)` |
   | `PRICE_PRO_MYR` / `PRICE_AGENCY_MYR` | 每月价格，默认 99 和 399 |
2. 用这个邮箱在网站注册并登录，左下角会出现 **Review payments →**，或直接打开 `https://你的网址/admin.html`。
3. 在后台点 **Upload new QR**，上传你的收款码图片（保存在持久化磁盘上，重新部署不会丢）。
4. 用户付款并提交后，你会收到邮件提醒（需要先配好第三部分的邮件）。在后台核对金额、交易号和截图：
   - 到账了：点 **Approve & activate**，用户的套餐立即开通，并收到确认邮件。同一套餐续费会接在原到期日后面。
   - 没到账：填写原因后点 **Reject**，用户会收到邮件，可以重新提交。
5. 到期后账号自动降回 Free，不会自动扣款。用户可以随时在网站里续费。

### 套餐规则（在 `backend/billing.py` 的 `PLANS` 里改）

| | Free | Pro RM99/月 | Agency RM399/月 |
|---|---|---|---|
| 每月优化次数 | 10 | 不限 | 不限 |
| 跟踪的活动数 | 2 | 不限 | 不限 |
| 导入 / 导出达人 | 不可以 | 可以 | 可以 |

可以一次购买 1、3 或 12 个月。

## 环境变量一览

| 变量 | 说明 |
|---|---|
| `APP_ENV` | `production` 时开启安全检查（镜像里已默认） |
| `JWT_SECRET` | 登录令牌密钥，至少 32 位，缺了服务器拒绝启动 |
| `DATABASE_PATH` | 数据库位置，默认 `/var/data/app.db` |
| `PUBLIC_BASE_URL` | 网站对外地址，用于邮件里的链接 |
| `SMTP_*` | 发信配置 |
| `AUTH_RATE_LIMIT` | 登录/注册限流，默认 `10/60`（每 IP 每分钟 10 次） |
| `CORS_ORIGINS` | 允许跨域的来源，同域部署时不需要设置 |
| `ADMIN_EMAILS` / `PAYMENT_PAYEE` / `PRICE_*_MYR` / `PAYMENT_QR_PATH` | 收款与审核配置，见第五部分 |

## 本地用 Docker 运行

```sh
docker build -t kol-optimizer .
docker run -p 8000:8000 -e JWT_SECRET=$(openssl rand -hex 32) -v kol-data:/var/data kol-optimizer
```
