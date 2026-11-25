# 部署上线指南（Render）

预计费用：约 **7.25 美元/月**（Starter 实例 7 美元 + 1 GB 持久化磁盘 0.25 美元）。域名另算，约 10 美元/年。

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

## 五、开通收款（Lemon Squeezy）

Lemon Squeezy 是"代收方"（Merchant of Record）：用户付款、全球增值税/销售税、发票都由它处理，然后把钱结算给你。**个人身份即可注册，不需要公司**。费用约每笔 5% + 0.5 美元。

1. 注册 <https://lemonsqueezy.com>，创建一个 Store，按提示完成身份验证和收款账户（支持 PayPal 或银行卡）。审核通过前可以先用 **Test mode** 调试。
2. **Products → New product**，建两个"订阅"类产品：
   - `Pro`：29 美元/月
   - `Agency`：99 美元/月
3. 打开每个产品的 variant，记下 **Variant ID**（数字）。在 **Settings → Stores** 记下 **Store ID**。
4. **Settings → API** 创建 API key。
5. **Settings → Webhooks → +**：
   - URL：`https://你的网址/billing/webhook`
   - Signing secret：自己随便设一串长随机字符串
   - 勾选所有 `subscription_*` 事件
6. 在 Render 的 **Environment** 填：
   | 变量 | 值 |
   |---|---|
   | `LEMONSQUEEZY_API_KEY` | 第 4 步的 key |
   | `LEMONSQUEEZY_STORE_ID` | Store ID |
   | `LEMONSQUEEZY_VARIANT_PRO` | Pro 的 Variant ID |
   | `LEMONSQUEEZY_VARIANT_AGENCY` | Agency 的 Variant ID |
   | `LEMONSQUEEZY_WEBHOOK_SECRET` | 第 5 步的 signing secret |
7. 在网站上注册一个新账号，点 **Upgrade to Pro**，用测试卡 `4242 4242 4242 4242` 付款。回到网站后左下角应显示 **Pro plan**。确认无误后在 Lemon Squeezy 关闭 Test mode，并把上面的 key 和 ID 换成正式环境的。

没配置这些变量时，网站照常运行，点升级会提示"Payments are not set up yet"。

### 套餐规则（在 `backend/billing.py` 的 `PLANS` 里改）

| | Free | Pro 29 美元 | Agency 99 美元 |
|---|---|---|---|
| 每月优化次数 | 10 | 不限 | 不限 |
| 跟踪的活动数 | 2 | 不限 | 不限 |
| 导入 / 导出达人 | 不可以 | 可以 | 可以 |

用户取消订阅后，可以用到当期结束，之后自动降回 Free。

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
| `LEMONSQUEEZY_*` | 收款配置，见第五部分 |

## 本地用 Docker 运行

```sh
docker build -t kol-optimizer .
docker run -p 8000:8000 -e JWT_SECRET=$(openssl rand -hex 32) -v kol-data:/var/data kol-optimizer
```
