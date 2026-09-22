# Relay 后端

此目录是 Smart 统一仓库中的 RSS → Discord 推送服务，由根目录的 `docker-compose.yml` 管理。

- 管理入口：Smart 的 `/relay` 页面，沿用 Smart 的飞书登录。
- 内部 API：`http://opus-relay:8090`，不发布宿主机端口；没有独立鉴权，不应直接暴露到公网。
- 本地配置：`relay/config.json`（从 `config.example.json` 复制），不提交 Git。
- 持久数据：`relay/data/opus.db`，含去重记录、翻译缓存和轮询进度；迁移必须保留。
- 简报与标题翻译使用各自的模型配置；Smart 的 LLM 设置不会自动修改 Relay 翻译设置。

部署、备份和迁移见[根目录 README](../README.md)。`static/index.html` 保留用于内部服务兼容，统一管理界面位于 `web/static/relay.html`。

源码导入自 [pushbyopus/Opus](https://github.com/alphacaicai2/pushbyopus/tree/ce148a4f09d5f417c36faec6eb215e9e473d3b0b/Opus)，原始提交 `ce148a4f09d5f417c36faec6eb215e9e473d3b0b`。后续修改在本仓库维护；无需克隆旧仓库或预先创建旧 Docker 网络。
