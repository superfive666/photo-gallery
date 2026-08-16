# Photo Gallery — Face Search

以自拍为查询条件，在 [photos.zrc.sg](https://photos.zrc.sg) 的历史活动相册中检索出「有我的照片」。

本项目是 [photos.zrc.sg](https://photos.zrc.sg) 的**旁挂部署**，不修改原站。
原站是一个公开、无需鉴权的跑团照片墙，照片和视频按 album 分组
（`photos.zrc.sg/album/2026-08-10`）。

本项目离线抓取指定 album 的照片、批量提取人脸 embedding 落库，
再提供一个上传自拍即可实时检索的 Web 界面。

---

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `docs/` | 全部设计文档（架构、隐私、CI/CD、评估方法） |
| `docs/schema/` | Postgres DDL，按 `NNN_name.sql` 顺序编号，随迭代追加 |
| `docs/plans/` | 每次迭代的计划书，一次迭代一个文件 |
| `web/` | 前端（React + Vite + TypeScript + Tailwind） |
| `api/` | 后端（FastAPI），负责鉴权、检索、缩略图分发 |
| `embedding/` | 人脸检测 + embedding 推理服务（InsightFace buffalo_l），独立容器 |
| `jobs/` | 可定时或手动执行的离线任务：拉取 album → 提取人脸 → 落库 → 聚类 |
| `libs/gallery_core/` | `api` 与 `jobs` 共享的 Python 包（DB 模型、embedding 客户端、配置） |
| `docker/` | 全部 Dockerfile |
| `.github/workflows/` | CI/CD（self-hosted runner） |
| `pyproject.toml` + `uv.lock` | **uv workspace**：四个 Python 成员共用一份锁文件 |

> `embedding/` 与 `libs/` 是在原定结构上追加的两个目录。前者来自「embedding 走独立容器」的选型；
> 后者用于避免 `api` 与 `jobs` 的 DB 模型和预处理逻辑各自漂移 —— 这类漂移会直接导致检索静默失效。
> 理由见 [`docs/architecture.md`](docs/architecture.md#为什么要有-libsgallery_core)。

---

## 三条主链路

```
① 离线建库   jobs → photos.zrc.sg → 批量人脸检测/embedding → Postgres(pgvector)
② 在线检索   web → api → embedding(自拍取最明显一张脸) → KNN → 照片 URL + 缩略图
③ 前端界面   上传/拍摄自拍（可选限定相册）→ 结果网格 → lightbox / 跳回原站原图
④ 浏览检索   分页翻相册 → 点开照片上的人脸小图 → 确认后用已存 embedding 检索该人
```

数据模型只有两张主表：

| 表 | 一行是什么 | 关键字段 |
| --- | --- | --- |
| `photo` | 一张照片/视频 | `id`(uuidv7) · `album` · `photo_url` · `thumbnail` |
| `face` | **一张脸** | `id`(uuidv7) · `photo_id`(FK) · `embedding(512)` |

一张合影有多个人 → 一条 photo 对应多条 face，这是分表的原因。
`album` 只在 `photo` 上，`face` 通过外键间接得到。

**不存 person、不做聚类、不给人脸命名。** 查询完全是实时的：上传自拍 → 取最明显的
一张脸 → 对 face 做 KNN → 返回照片。自拍处理完即销毁。

详见 [`docs/architecture.md`](docs/architecture.md)。

---

## 快速开始

Python 依赖用 [uv](https://docs.astral.sh/uv/) 管理（workspace + 单一 `uv.lock`），
前端仍用 npm。

```bash
uv sync --all-packages             # 或 make install：按 uv.lock 装齐依赖
cp .env.example .env               # 填入 INVITE_CODE_HASH / JWT_SECRET / AUDIT_HASH_SALT
make up                            # 起 embedding + api + web（本地默认还带容器化 pg）
make migrate                       # 执行 docs/schema 下的 DDL
make probe  ALBUM=2026-08-10       # 先探查源站页面结构，确认解析正确
make ingest ALBUM=2026-08-10       # 批量拉取 + embedding + 落库
open http://localhost:8080
```

`INVITE_CODE_HASH` 用 `uv run python -m api.app.tools.hash_invite` 生成
（明文邀请码不进 .env）。

常用命令见 `make help`。

### 依赖怎么加

```bash
# 改对应成员的 pyproject.toml（api / jobs / embedding / libs），然后
uv lock            # 刷新 uv.lock —— 必须一起提交
uv sync --all-packages
```

CI 用 `uv sync --frozen`：改了依赖但忘记提交 `uv.lock` 会直接失败，这是故意的。
`libs/pyproject.toml` 里的 DB 依赖在 `[db]` extra 中 —— embedding 服务不碰数据库，
所以它的镜像里没有 sqlalchemy / asyncpg / pgvector。

---

## 部署

两台内网机器都注册成 GitHub self-hosted runner，用机器名做 label 精确定位。
部署全程在带 GPU 的那台上：就地 `build → 迁移 → 滚动重启 → 健康检查`，
**不引入镜像仓库** —— 构建产物直接留在要运行它的 docker 里。
版本靠 `sha-<short>` tag 区分，回滚就是换个 tag 重新 `up`。

**数据库用生产机宿主机上已有的 Postgres**，不跑容器化 pg；容器经
`host.docker.internal` 访问它。本地开发叠加 `docker-compose.localdb.yml`
即可获得一个容器化 pg（`.env.example` 默认就是这个配置）。

从裸 Ubuntu 到上线的逐步 runbook（含 runner 安装）见
[`docs/deployment.md`](docs/deployment.md)；分支模型与 workflow 布局见
[`docs/cicd.md`](docs/cicd.md)。

```bash
# 生产机上叠加 GPU 层（写在 .env 里，这样手敲命令也一致）
COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
EMBEDDING_USE_GPU=true
```

---

## 已知局限

这些是人脸识别的固有局限，不是 bug，请提前对使用者做预期管理：

- **小脸召回不了。** 短边小于 `MIN_FACE_PX`（默认 40px）的人脸 embedding 不可靠，会被丢弃。
  大合影里站在后排的人很可能搜不到。
- **侧脸、遮挡、口罩、墨镜会显著降低召回率。** 因为不做 person 聚类，侧脸没有「经由
  同一个人的正脸被间接命中」这条路径，这类照片就是搜不到。这是「不存人物身份数据」
  换来的代价，不是 bug。缓解办法只有一个：多上传一两张不同角度的自拍。
- **某人照片特别多时结果会被截断。** 检索取 `SEARCH_CANDIDATES`（默认 500）个最近邻
  候选再过滤，超出的部分不会返回。遇到这种情况调这个值，不要调阈值。
- **儿童照片、跨越多年的照片**准确率下降明显 —— 人脸随年龄变化，ArcFace 对此不鲁棒。
- **误报无法归零。** 阈值是 precision/recall 的权衡，调高会漏、调低会串人。阈值标定方法见
  [`docs/evaluation.md`](docs/evaluation.md)。
- **视频当前不处理**（只登记不提取），见 `docs/plans/`。
- **设备维度限流**（自拍检索默认 3 次/小时，按脸检索 4 次/小时，独立计数）的设备 id
  绑在 JWT 里 —— 清 cookie / 脚本不带 cookie 都换不来新身份，想换必须重新登录过
  captcha。彻底清空 cookie 后重新登录仍会得到新设备身份，所以硬边界依旧是
  session 与 IP 两层限流。见 `docs/plans/0007`。
- **登录验证码挡的是脚本批量试码，不是专业打码平台。** 自研 SVG captcha 的威胁模型
  就到这里 —— 更强的对抗手段（第三方 captcha）与本项目的 CSP 和隐私立场冲突，不做。

---

## 隐私

- 用户上传的自拍**只在内存中处理**，提取 embedding 后立即丢弃；不落盘、不落库、不写日志。
  这条纪律由 `api/tests/test_no_persistence.py` 做源码级守卫，不只是文档承诺。
- 人脸 embedding 属于生物识别数据（新加坡 PDPA）。数据的用途、留存与删除方式见
  [`docs/privacy.md`](docs/privacy.md)。
- **源站公开不等于本站可以公开。** 原站上找某人的照片得一张张翻，而这里输入一张自拍就能
  把他在所有活动里的照片一次性聚齐 —— 可及性的量变就是隐私上的质变。
  所以本站需要邀请码。
- **邀请码可以绑定单个相册**（`python -m jobs invite create --album ...`）：持码人只能在
  自己参加的那次活动里检索，检索、相册列表、图片分发三处都在服务端强制这个边界。
  发码即授权，按活动隔离是默认姿势；全相册的管理码只留给站主。
- 本项目只存 256px 缩略图，原图始终只是一个指向源站的 URL，不复制、不缓存。
- 检索结束后界面会**明确告知自拍已删除** —— 不只是上传前承诺一句，而是完成后确认一次。
- 提供 opt-out：`make block SELFIE=...` 用当事人的自拍把他的全部人脸从检索中永久屏蔽。

---

## 模型许可

使用 [InsightFace](https://github.com/deepinsight/insightface) 的 `buffalo_l` 预训练模型包
（RetinaFace `det_10g` + ArcFace `w600k_r50`，512 维）。

> ⚠️ **InsightFace 的预训练模型仅授权用于非商业研究用途。** 本项目作为社团内部非商业使用符合该
> 授权；若日后转为商业用途，必须更换为商业可用的模型权重。此约束请勿在迭代中被静默忽略。
