# CLAUDE.md

给在本仓库工作的 AI 助手的指引。人类协作者也建议读一遍「不可违反的约束」。

## 项目一句话

以自拍检索 photos.zrc.sg 历史活动相册中的本人照片。旁挂部署，不改原站。

## 架构速览

五个容器（`docker compose` 编排）+ 宿主机数据库：

- **数据库不在 compose 里**：生产连宿主机上已有的 Postgres 16 + pgvector
  （`DATABASE_URL` 指向 `host.docker.internal`），唯一状态所在地。
  本地开发可叠加 `docker-compose.localdb.yml` 起一个容器化 pg。
- `embedding` — FastAPI + InsightFace buffalo_l + Chinese-CLIP（ONNXRuntime，CPU 或 CUDA），
  **唯一**做人脸与图文向量化的地方。`/extract`(/batch) 人脸；`/clip/text`、
  `/clip/image/batch` 图文（剪辑域）
- `api` — FastAPI，鉴权 + 检索 + 缩略图分发 + 剪辑域接口与 agent 框架，不含模型
- `web` — nginx 托管 Vite 构建产物（查找 UI + 剪辑聊天窗）
- `jobs` — 一次性容器 / cron 触发，离线建库
- `worker` — jobs 镜像的常驻模式，认领剪辑域任务（下载建库/解析检索/渲染），
  见 docs/media-edit.md

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
7. **向量检索必须保持「内层 ORDER BY + LIMIT 取候选、外层过滤」的两层结构。**
   pgvector 的 HNSW 只在这个形式下会被用到；把阈值直接写进 WHERE 会退化成全表扫描 ——
   不报错，只是慢。见 `docs/schema/README.md`「向量检索的写法」。
8. **不存任何长期的人物身份数据。** 没有 person 表、不做聚类、不给人脸命名。
   查询是实时的，用完即弃。
9. **仓库是公开的：内网地址、真实密钥、生产主机名一律不进仓库。**
   代码、文档、提交信息都算。`.env` 永不提交，`.env.example` 只放占位值。

## 常用命令

```bash
make help          # 全部命令
make install       # uv sync --all-packages（本地开发装齐依赖）
make up / down     # 起停 compose
make migrate       # 顺序执行 docs/schema/*.sql
make probe ALBUM=x  # 探查源站页面结构，不写库
make ingest ALBUM=x # 批量离线建库
make test          # api + jobs 的 pytest，web 的 vitest
make lint          # ruff + mypy + eslint + prettier
make check         # 提交前门禁：lint + test + 前端构建（本仓库没有 CI）
make lock          # uv lock --upgrade（升级依赖）
make eval          # 跑阈值评估集，输出 precision/recall
```

## 代码约定

- Python 3.11，`ruff` + `mypy --strict`（`libs`/`api`/`jobs`/`embedding` 同一套配置，
  都写在根 `pyproject.toml` 里）。
- SQLAlchemy 2.0 风格（`Mapped[...]` 注解），异步引擎。
- **依赖用 uv workspace 管理。** 根 `pyproject.toml` 是 workspace 根，四个成员
  `libs`/`api`/`jobs`/`embedding` 各有自己的 `pyproject.toml`，共用**一份** `uv.lock`。
  加依赖就改对应成员的 `pyproject.toml` 再 `uv lock`，**改完必须提交 uv.lock** ——
  容器构建用 `--frozen`，锁文件没跟上会直接失败（这是故意的）。
  不要再写 `requirements.txt`，也不要在容器里 `pip install`。
- 共享代码放 `libs/gallery_core`（包名 `gallery-core`），是四个成员里唯一会被真正构建
  安装的包；`api`/`jobs`/`embedding` 都是 `package = false` 的虚拟成员，代码按源码目录导入。
  DB 依赖在 `gallery-core[db]` extra 里 —— embedding 服务不碰数据库，别把它加回去。
- 前端：React 19 + TS strict + Tailwind。组件按 `src/components/` 平铺，业务逻辑进 `src/hooks/`。
- 前端设计相关工作请使用 `web/.claude/skills/` 下的 `design-system` 与 `ui-ux-pro_max` 两个 skill。
- 提交信息用中文或英文均可，但要说清「为什么」而不只是「做了什么」。

## 迭代流程

1. 在 `docs/plans/NNNN-<slug>.md` 写计划（目标 / 范围 / 非范围 / 验收标准 / 风险）。
2. 需要改库就加 `docs/schema/NNN_*.sql`，并同步更新 `docs/schema/README.md` 的表清单。
3. 实现 + 测试。
4. 更新 README「已知局限」与相关文档。

## 数据模型速览

只有两张主表。一张合影有多个人 → 一条 photo 对应多条 face，这是分表的原因。

- `photo` — `id`(uuidv7) / `album` / `photo_url`(唯一，幂等键) / `thumbnail`(BYTEA)
- `face` — `id`(uuidv7) / `photo_id`(FK) / `embedding(512)`，普通表不分区

`album` 只在 `photo` 上 —— 照片属于哪个相册是它自己的属性，`face` 通过外键间接得到。
`album` 就是源站 URL 里那段 slug（`/album/2026-08-10`），不是外键，没有 album 表。

没有 `person` 表、不做聚类。查询完全实时：自拍 → 最明显的一张脸 → KNN。

## 当前未决问题

- ~~photos.zrc.sg 的相册页标记结构未确认~~ **已确认并收敛**（2026-08-14）：
  每个媒体项是带 `data-lightbox` 的 div，字段全在 data-* 属性里
  （`data-original-src` / `data-thumb` / `data-is-video`）。解析用 bs4，
  见 `jobs/sources/static_gallery.py` 的模块 docstring。
  仍未确认的只剩**相册索引页**结构（影响不带 --album 的全量 ingest）。
- 相似度阈值需要用真实数据标定，当前默认值只是文献经验值。见 `docs/evaluation.md`。
- `SEARCH_CANDIDATES`（默认 500）是召回上限，尚未用真实数据验证是否够用。
