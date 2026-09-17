# Outlook 云端取件服务

本机 Clash 全局模式会掐断 `outlook.office365.com:993`。取件改在腾讯云 `beike-server` 上做 IMAP，Windows 桌面程序通过 SSH 本地转发访问，**不改 Clash，不把 18793 暴露到公网**。

```
Windows GUI  --ssh -L 18793-->  VPS 127.0.0.1:18793  --IMAP 993-->  Outlook
```

Graph 令牌仍走本机 HTTPS。Thunderbird / IMAP-only 令牌走云端 IMAP。

## 部署

在仓库根目录（已配置 `ssh beike-server`）：

```
python pickup_service/deploy.py
```

会：

1. 生成或复用 `%LOCALAPPDATA%\TemporaryEmailPickup\settings.json` 里的 DPAPI `outlook_pickup_key`
2. 把 `outlook_imap.py` + `server.py` 拷到 `/home/ubuntu/outlook-pickup/`
3. 写入仅本机可读的 `.env`（`PICKUP_BIND=127.0.0.1`）
4. 安装并启动 systemd 服务 `outlook-pickup`

## 本机

启动 `python app.py` / `start.vbs` 时会：

- 建立 `ssh -N -L 18793:127.0.0.1:18793 beike-server`
- `GET http://127.0.0.1:18793/health`
- 把 Outlook IMAP 请求发到该地址（`Authorization: Bearer <key>`）

环境变量可覆盖：`OUTLOOK_PICKUP_URL`、`OUTLOOK_PICKUP_KEY`、`OUTLOOK_PICKUP_SSH`。

## 服务运维

```
ssh beike-server
sudo systemctl status outlook-pickup
curl -sf http://127.0.0.1:18793/health
```

服务只监听 `127.0.0.1:18793`，刷新令牌不写盘。
