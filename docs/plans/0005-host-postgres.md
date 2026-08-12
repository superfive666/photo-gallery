# 0005 — 数据库改用生产机宿主机上已有的 Postgres

## 背景

生产机（192.168.0.12）上本来就跑着一个 Postgres。再起一个容器化 pg 意味着同一台机器
上两个数据库实例：白占内存、备份两套、升级两次。用户明确要求直连宿主机的库。

## 改动

- `docker-compose.yml` 删掉 `db` 服务与 `pgdata` 卷；`api` / `jobs` 加
  `extra_hosts: host.docker.internal:host-gateway`，去掉对 `db` 的 `depends_on`。
- 新增 `docker-compose.localdb.yml`：本地开发用的容器化 pg 叠加层
  （服务定义 + 把 `depends_on` 加回来）。`.env.example` 的 `COMPOSE_FILE` 默认带它，
  本地开箱即用；生产的 `COMPOSE_FILE` 是 base + gpu，`db` 服务完全不存在。
- `deploy.yml` / `ingest.yml` 去掉 `docker compose up -d db`。
- 备份从「容器里 pg_dump」改为宿主机 `crontab -u postgres` 直接 `pg_dump`。
- `docs/deployment.md` 新增 2.3「宿主机 Postgres」：装 pgvector、建用户建库、
  **以超级用户预建扩展**、listen_addresses / pg_hba / ufw 放行 docker 网段、
  用临时容器验证连通。

## 关键点（都是真实会踩的坑）

1. **扩展必须预建。** `vector` 不是 trusted 扩展，`gallery` 普通用户执行
   `CREATE EXTENSION` 会报 permission denied。迁移里的 `IF NOT EXISTS` 只负责跳过，
   不负责创建 —— 所以 2.3② 里以 postgres 超级用户预建 `vector` 和 `pgcrypto`。
   （之前本机验证迁移时没暴露：initdb 出来的用户就是超级用户。）
2. **三层都要放行，缺一层都是 connection refused / no pg_hba entry：**
   `listen_addresses`（默认只听 localhost）、`pg_hba.conf`（172.16.0.0/12）、
   `ufw`（容器→宿主机走 INPUT 链，默认 deny 会拦）。
3. `host.docker.internal:host-gateway` 在 Linux 上不是默认行为，必须显式声明
   `extra_hosts` —— 只在 Docker Desktop 上它才开箱即用。
4. 代码零改动：`DATABASE_URL` 本来就是唯一入口（`gallery_core/config.py`）。

## 非范围

- 不迁移已有数据（库是新建的）。
- 不做 pg 版本要求，只要求 pgvector ≥ 0.5（HNSW）。CI 仍用 pg16 service container。
