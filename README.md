# Smart：智能简报与 Relay 统一管理

一个仓库、一份 Docker Compose、一个登录入口，部署两个服务：

| 服务 | 代码 | 职责 | 访问方式 |
| --- | --- | --- | --- |
| Smart / OpusBrief | 根目录、`briefing/`、`web/` | AI 简报、定时任务、模板、历史、飞书登录、统一管理界面 | 宿主机 `127.0.0.1:8812` |
| Opus Relay | `relay/` | Miniflux 增量抓取、标题翻译、分组路由、Discord 批量推送 | Compose 网络内 `opus-relay:8090`，不发布端口 |

Smart 的 `/relay` 页面通过后台代理访问 Relay，API 和实时日志沿用 Smart 的登录会话。两个容器由同一 Compose 网络连接，不再依赖旧的 `pushbyopus` 仓库或外部 `opus_default` 网络。

**配置范围：** 系统设置中的 LLM 用于生成简报；Relay 中的翻译配置只用于推送前翻译非中文标题，二者独立。获取模型列表支持输入 Base URL / API Key，保留手动输入；编辑时可复用已保存 Key，但更换 Base URL 必须重填。连接测试会实际调用所选模型，可能产生少量 API 用量。

## 从零部署

需要 Docker Engine 和 Docker Compose v2。

```bash
git clone https://github.com/alphacaicai2/Opussummary.git
cd Opussummary
cp .env.example .env
cp relay/config.example.json relay/config.json
mkdir -p data relay/data
```

启动前配置：

1. 在 `.env` 中填写飞书应用的 `FEISHU_CLIENT_ID`、`FEISHU_APP_SECRET` 和实际的 `FEISHU_REDIRECT_URI`。在飞书后台登记同一回调地址，例如 `https://smart.example.com/auth/feishu/callback`。可通过用户或邮箱域名白名单限定管理员。
2. 在 `relay/config.json` 中填写真实 Miniflux URL 和 Token。默认路由为空、翻译关闭；启动后可在统一页面填写频道路由、标题翻译等设置。
3. 保留 `FEISHU_LOGIN_ENABLED=true`；缺失凭证会拒绝启动。会话密钥不填写时自动生成到 `data/.web-session-secret`，备份数据时一并保留。

```bash
docker compose config --quiet
docker compose up -d --build --wait
docker compose ps
```

将 HTTPS 反向代理指向 `127.0.0.1:8812`，并支持 WebSocket Upgrade。同一宿主机的 Caddy 可使用：

```caddyfile
smart.example.com {
    reverse_proxy 127.0.0.1:8812
}
```

打开 Smart 域名，用飞书登录后进入「系统设置」配置简报、进入「Relay 管理」配置订阅推送。Relay API 没有独立鉴权，必须保留内部网络访问方式。

仅供本机开发时可显式设置 `FEISHU_LOGIN_ENABLED=false`，通过 `http://127.0.0.1:8812` 访问；不要将该配置用于公网部署。

## 更新、检查与测试

```bash
git pull --ff-only
docker compose up -d --build --wait
docker compose ps
docker compose logs --tail=100 opusbrief
docker compose logs --tail=100 opus-relay
```

健康检查分别覆盖 Smart 的登录状态接口和 Relay 的配置接口，证明 HTTP 服务可响应；不代表 Miniflux、模型、Discord 凭证有效，需在管理页面分别验证。查看或分享日志时注意隐藏 Webhook URL 等凭证。

完整回归测试使用临时数据库，不挂载生产配置或数据；Relay 集成测试已使用本仓库源码，不再要求额外克隆另一个项目：

```bash
docker build -t smart-tests .
docker run --rm --tmpfs /app/data \
  -v "$PWD/relay:/app/relay:ro" \
  smart-tests python -m unittest discover -s tests -v
```

## 数据与备份

这些文件都不应提交 Git，也不会进入镜像构建上下文：

| 路径 | 内容 |
| --- | --- |
| `.env` | 飞书等运行凭证 |
| `data/` | 简报数据库、计划任务、模型配置、会话密钥 |
| `relay/config.json` | Miniflux、翻译和 Discord 路由凭证 |
| `relay/data/` | 推送去重数据库、翻译缓存、轮询进度 |

一致性备份应在两个服务停止后复制上述文件和目录；不要在写入期间只复制 SQLite 主文件，忽略 WAL。恢复时先放回配置与数据，再启动服务。备份含密钥，应限制读取权限并保存在仓库之外。

## 从原来两个 Compose 项目迁移

已有部署不能直接启动第二套 Relay，否则可能重复推送：

1. 记录两个容器的挂载路径、镜像版本和旧 Compose 文件，构建新镜像但先不启动。
2. 等旧 Relay 当前轮询与批量发送完成，再停止旧 Relay 和 Smart；确认容器已退出。
3. 备份 Smart 的 `.env`、`data/` 及旧 Relay 的 `config.json`、完整 `data/`。将 Relay 配置与数据复制到统一仓库的 `relay/config.json`、`relay/data/`，核对数据库完整性与记录数。必须保留 `opus.db` 中的去重记录及轮询进度。
4. 若旧 Relay 容器名占用 `opus-relay`，将停止的旧容器重命名留作回退，并关闭其自动重启。不要删除旧数据。
5. 在统一仓库运行 `docker compose up -d --build --wait`。确认仅一个 Relay 轮询进程运行；检查飞书登录、简报配置、Relay 配置和实时日志。
6. 回退时先停止新服务。若新服务已处理文章，将最新 Relay 数据和配置备份后同步回旧目录，再恢复旧镜像、Compose 与容器；避免使用过期去重记录造成重复推送。

旧域名的跳转属于反向代理配置，不由本仓库自动修改。当前生产环境统一入口为 `smart.vibexcap.com`，旧 `relay.vibexcap.com` 仅跳转至统一入口，旧 API 已停用。

## 目录与来源

```text
.
├── docker-compose.yml     # 同时编排 Smart 和 Relay
├── .env.example           # Smart 登录配置示例
├── briefing/              # 简报生成与调度
├── web/                   # 统一页面、鉴权、Relay 代理
├── tests/                 # Smart 与 Relay 集成回归
└── relay/                 # 独立运行的 Relay 后端、镜像与配置示例
```

Relay 源码来自 [pushbyopus/Opus](https://github.com/alphacaicai2/pushbyopus/tree/ce148a4f09d5f417c36faec6eb215e9e473d3b0b/Opus)，保留原始提交标识。后续统一系统的开发、修复和部署以本仓库为准；旧仓库保留历史实现与参考文档。
