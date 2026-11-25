# 用 Render 一键部署 Lumio

适合没有自己服务器、想最快上线的情况。全部放在新加坡机房，离东南亚商家最近。

**每月费用约 13 美元**：网站服务 Starter 7 美元 + PostgreSQL 数据库 Basic 256 MB 6 美元。
数据库独立存放，网站重新部署不会丢数据。

> 想用自己的云服务器（Docker + Caddy）部署，请看 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 一、第一次部署（约 10 分钟）

1. 用 GitHub 账号登录 <https://render.com>，在 Billing 里绑定一张银行卡。
2. 点右上角 **New → Blueprint**，选择 `tiktok-kol-optimizer` 仓库，分支选 `master`。
   Render 会读取仓库里的 `render.yaml`，自动创建：
   - 网站服务 `kol-optimizer`（Docker）
   - 数据库 `lumio-db`（PostgreSQL）
   - 随机生成的 `JWT_SECRET`（登录签名）和 `ADMIN_KEY`（运营后台密钥）
3. 页面会列出需要你填的变量，第一次可以**全部留空**，直接点 **Apply**。
4. 等 5 分钟左右，构建完成后打开 Render 给的网址（通常是 `https://kol-optimizer.onrender.com`），注册一个账号试用。
5. 之后每次合并代码到 `master`，Render 会自动重新部署。

## 二、打开运营后台

1. 在 Render 打开 `kol-optimizer` 服务 → **Environment**，找到 `ADMIN_KEY`，点眼睛图标复制。
2. 打开 `https://你的网址/admin.html`，粘贴这个密钥登录。
3. 在后台可以看用户、审核付款、开通套餐、清理演示账号。

`ADMIN_KEY` 等于后台密码，不要发给别人。怀疑泄露时在 Environment 里改成新的随机值（至少 16 位）即可。

## 三、收款设置（扫码付款）

- 收款码图片在仓库的 `frontend/pay/duitnow-qr.png`，换成你自己的 DuitNow / TNG 收款码后合并到 `master` 即可。
- 在 Environment 里填 `PAY_PAYEE_NAME`（显示在收款码旁边的收款人名字）。
- 套餐以美元定价，付款时按 `PAY_FX_RATE`（默认 4.4）换算成马币。汇率变化时改这个值。
- 想在商家点“我已付款”时收到邮件，先完成第四部分，再填 `OPERATOR_EMAIL`。

## 四、开通邮件（推荐，用于验证邮箱、找回密码、续费提醒）

推荐 [Resend](https://resend.com)，每月免费 3000 封。

1. 注册 Resend，添加并验证你的域名（没有域名时可以先跳过邮件）。
2. 在 Render 的 Environment 里填：
   | 变量 | 填什么 |
   |---|---|
   | `SMTP_HOST` | `smtp.resend.com` |
   | `SMTP_USER` | `resend` |
   | `SMTP_PASSWORD` | Resend 的 API Key |
   | `SMTP_FROM` | `Lumio <no-reply@你的域名>` |
   | `APP_BASE_URL` | 你的网站网址，例如 `https://kol-optimizer.onrender.com`，末尾不要斜杠 |
3. 保存后服务会自动重启。注意：填了 `SMTP_HOST` 就必须填 `APP_BASE_URL`，否则网站会拒绝启动。

## 五、AI 投放助手（可选）

在 Environment 里填 `DEEPSEEK_API_KEY` 即可开启。不填时其他功能照常使用。

## 六、绑定自己的域名

在 `kol-optimizer` 服务 → **Settings → Custom Domains** 添加域名，按提示在域名商那里加一条 CNAME 记录，HTTPS 证书自动签发。
绑定后把 `APP_BASE_URL` 改成新域名。

## 七、备份与注意事项

- 付费的 PostgreSQL 自带每日备份，可在 `lumio-db` → **Backups** 恢复。
- “AI 视频”生成的视频文件存在服务器临时目录，重新部署后会清空，需要的视频请及时下载。
- Starter 方案是 0.5 核 CPU、512 MB 内存，适合起步阶段。用户多了以后，可在 Settings 里把网站升级到 Standard（25 美元/月）。
