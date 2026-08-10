# CLAUDE.md

给在本仓库工作的 AI 助手的指引。人类协作者也建议读一遍「不可违反的约束」。

## 项目一句话

以自拍检索 photos.zrc.sg 历史活动相册中的本人照片。旁挂部署，不改原站。

## 架构速览

五个容器，`docker compose` 编排：

- `db` — Postgres 16 + pgvector，唯一状态所在地
- `embedding` — FastAPI + InsightFace buffalo_l（ONNXRuntime，CPU 或 CUDA），**唯一**做人脸
  检测/embedding 的地方。`/extract` 单张（在线检索）、`/extract/batch` 批量（离线建库）
- `api` — FastAPI，鉴权 + 检索 + 缩略图分发，不含模型
- `web` — nginx 托管 Vite 构建产物
- `jobs` — 一次性容器 / cron 触发，离线建库

## 不可违反的约束

违反下面任何一条都会造成隐私事故或让检索静默失效。改动涉及这些区域时请格外小心。

1. **用户上传的自拍不得持久化。** 不写磁盘、不写数据库、不写日志、不进错误上报。
   `api` 收到的图片字节只应存在于请求生命周期内的内存中。
2. **不得把 embedding 或人脸图片写入日志。** 日志里只允许出现 id、耗时、计数、错误类型。
3. **人脸检测与 embedding 的预处理逻辑只能存在于 `embedding/` 服务中。**
   `api` 和 `jobs` 一律通过 HTTP 调用它。若在别处再实现一份 resize/对齐/归一化，
   离线库和在线查询的向量就会落在不同的空间里 —— 检索会「能跑但结果全错」，且极难定位。
4. **embedding 必须 L2 归一化后再存。** 索引用 `vector_cosine_ops`。
   归一化在 `embedding` 服务的出口统一完成，下游不再动。
5. **每条 face 记录必须带 `model_name` / `model_version` / `dim`。**
   换模型时靠这几个字段识别存量数据并重算，而不是整库作废。
6. **DDL 只以追加方式演进。** 新增 `docs/schema/NNN_*.sql`，绝不原地修改已发布的迁移文件。
7. **所有针对 `face` 表的查询/DML 都必须带上 `album` 条件**（除了故意的全库检索）。
   `face` 按 `album` 做 LIST 分区，少了这个条件就会扫描全部分区 —— 不会报错，只是慢，
   所以极容易在 review 时被放过。见 `docs/schema/README.md`。
8. **先建分区、再插数据。** 某个 album 的行一旦落进 `face_default`，之后就无法再为它
   建专属分区（Postgres 会拒绝）。`pipeline` 的顺序不能颠倒。
9. **self-hosted runner 上不得因 fork PR 而执行不受信任的代码。** 见 `docs/cicd.md`。

## 常用命令

```bash
make help          # 全部命令
make up / down     # 起停 compose
make migrate       # 顺序执行 docs/schema/*.sql
make probe ALBUM=x  # 探查源站页面结构，不写库
make ingest ALBUM=x # 批量离线建库
make cluster       # 重跑 person 聚类
make test          # api + jobs 的 pytest，web 的 vitest
make lint          # ruff + mypy + eslint + prettier
make eval          # 跑阈值评估集，输出 precision/recall
```

## 代码约定

- Python 3.11，`ruff` + `mypy --strict`（`libs`/`api`/`jobs`/`embedding` 同一套配置）。
- SQLAlchemy 2.0 风格（`Mapped[...]` 注解），异步引擎。
- 共享代码放 `libs/gallery_core`，通过 `pip install -e ./libs` 进入 `api` 与 `jobs` 镜像。
- 前端：React 19 + TS strict + Tailwind。组件按 `src/components/` 平铺，业务逻辑进 `src/hooks/`。
- 前端设计相关工作请使用 `web/.claude/skills/` 下的 `design-system` 与 `ui-ux-pro_max` 两个 skill。
- 提交信息用中文或英文均可，但要说清「为什么」而不只是「做了什么」。

## 迭代流程

1. 在 `docs/plans/NNNN-<slug>.md` 写计划（目标 / 范围 / 非范围 / 验收标准 / 风险）。
2. 需要改库就加 `docs/schema/NNN_*.sql`，并同步更新 `docs/schema/README.md` 的表清单。
3. 实现 + 测试。
4. 更新 README「已知局限」与相关文档。

## 数据模型速览

两张主表。一张合影有多个人 → 一条 photo 对应多条 face，这是分表的原因。

- `photo` — `id`(uuidv7) / `album` / `photo_url`(唯一，幂等键) / `thumbnail`(BYTEA)
- `face` — `id`(uuidv7) / `photo_id` / `album` / `embedding(512)`，**按 album LIST 分区**，
  PK 是 `(album, id)`

`album` 就是源站 URL 里那段 slug（`/album/2026-08-10`），不是外键，没有 album 表。

## 当前未决问题

- **photos.zrc.sg 的相册页标记结构未确认。** 站点本身已确认是公开无鉴权、
  地址形如 `/album/<slug>`。`static_gallery.py` 里现在是按优先级依次尝试的通用解析
  （JSON 索引 → `<a href>` → `<img src>`）。
  **下一步跑 `make probe ALBUM=2026-08-10`**，拿到真实输出后收敛成精确选择器。
  详见 [`docs/data-source.md`](docs/data-source.md)。
- 相似度阈值需要用真实数据标定，当前默认值只是文献经验值。见 `docs/evaluation.md`。
- 分区方案的取舍已记录在 `docs/schema/README.md`「分区的代价」—— 相册数到千级时要重新评估。
