# 0010 本地路径素材相册

## 背景与目标

目前素材来源由全局开关 `SOURCE_ADAPTER` 决定：要么整库走 photos.zrc.sg
（`static_gallery`），要么整库走本地目录（`local_dir`，定位是开发与评估）。
实际需求是**按相册混用**：有些相册来自源站，有些相册直接指定宿主机上的本地路径
（例如没有发布到照片墙的活动素材）。邀请码照旧一码一相册，持码人不感知也不需要
感知素材在哪 —— 检索、浏览、剪辑的体验与远端相册完全一致。

一句话：**素材来源从「部署期全局选择」变成「相册粒度的运行时路由」。**

## 现状盘点（哪些已经就绪）

比预想的更接近目标，核心机制都已存在：

- `jobs/sources/local_dir.py` 已实现完整的 `LocalDirAdapter`（一级子目录名 = album slug），
  且 `photo_url` 采用 `local://album/<相对路径>` 形态，与远端 `https://...` 在语义上对齐。
- `photo.photo_url` / `media_asset.source_url` 是幂等键并**持久化了来源信息** ——
  URL scheme 本身就区分了本地与远端，存量数据无需任何迁移。
- 剪辑域已经部分本地化：`media_ingest` 对已在 media 目录里的文件回退为
  `local://<album>/<name>`；`render.py` 取原图时本地路径优先。
- 邀请码绑定的是 album slug（`invite_code.album`），与素材来源正交，**零改动**。

## 缺口（本计划要补的）

1. **来源选择是全局的**：`build_adapter()` 只认 `SOURCE_ADAPTER`，无法一库两源。
   混存时立即出问题 —— 例如 `face-thumbs` 回填拿着 `static_gallery` adapter
   去 HTTP GET 一条 `local://` URL。
2. **api 的原图分发假设远端**：`/api/photos/{id}/original` 无条件 302 到
   `photo_url`（`api/app/routers/photos.py:195`），浏览器打不开 `local://`。
3. **api 容器没有挂载本地素材目录**，即使想代理分发也读不到文件。
4. `local://` 的 URL↔路径换算规则只定义在 `jobs/sources/local_dir.py`
   （`photo_url_for`），api 不应 import jobs 包。
5. 渲染取本地照片时若 media 目录里没有副本直接报错（`render.py:143-145`），
   没有回退到素材源目录再读一次。

## 范围

### 1. 路由 adapter（核心）

新增 `jobs/sources/composite.py`：`CompositeAdapter` 同时持有
`LocalDirAdapter` 与 `StaticGalleryAdapter`，实现同一个 `SourceAdapter` 协议：

- `list_albums()`：本地一级子目录 ∪ 远端相册列表（远端失败时降级为仅本地并告警）。
- `list_assets(album)`：**`SOURCE_LOCAL_DIR/<album>` 目录存在 → 本地，否则远端。**
  这是「指定本地路径素材」的唯一入口动作：把目录放进去，相册就是本地的。
- `open_asset(asset)` / `fetch_thumbnail(asset)`：**按 URL scheme 分发**
  （`local://` → 本地读，其余 → HTTP）。回填、重算、渲染等拿存量 URL 取字节的
  场景因此天然支持混存，与「相册当前算本地还是远端」无关。

`SOURCE_ADAPTER` 增加 `auto` 值并作为**新默认**；`local_dir` / `static_gallery`
保留为强制单源模式（开发、评估、CI 不受影响）。

**同名冲突规则：本地目录优先，命中时打一条 warning 日志。** 选「本地优先」是因为
它是确定性的本机事实（目录在不在），而远端列表依赖网络与解析；规则写进
`docs/data-source.md`。

### 2. api 分发本地原图

`/api/photos/{id}/original` 改为按 scheme 分流：

- `https://...` → 维持 302（远端行为完全不变）；
- `local://album/...` → 从只读挂载的素材目录 `FileResponse` 流式分发，
  复用现有的相册越权 404 语义与 `Cache-Control`/`ETag` 约定。

配套：

- URL↔路径换算收敛到 `libs/gallery_core`（新增 `gallery_core/local_source.py`：
  `photo_url_for()` / `path_from_photo_url()`），`jobs/sources/local_dir.py` 与
  `jobs/eval.py` 改为从这里 import。规则仍只定义一次 —— 这正是原注释强调的教训。
- **路径安全**：photo_url 虽然只来自建库流程，api 端仍做防御纵深 ——
  `resolve()` 后必须仍在素材根目录之下，否则 404；拒绝 `..` 与绝对路径成分。
- `docker-compose.yml` 给 api 增加
  `${SOURCE_LOCAL_DIR:-./sample-albums}:/data/sample-albums:ro`。
  这不违背「api 不挂 media」的原则：source 目录是**输入素材**（与源站地位等同，
  jobs 容器早已同样挂载），不是渲染交付物，且只读。

### 3. 剪辑域打通

- `jobs/worker.py` / `media-ingest` 构建 adapter 的入口换成 `build_adapter()`
  返回的 composite（入口本身不变，行为随新默认值升级）——
  worker 认领本地相册的建库/渲染任务即可工作。
- `render.py` 取照片原图：`local://` 且 media 目录无副本时，改为经 adapter
  `open_asset` 从素材源目录读取，而不是直接报错；文件真不在了再报同样的错。
- 本地相册的视频与远端一致：建库时进 `media/<album>/`（拆条与 ffmpeg
  需要随机访问本地文件），来源是复制而非下载。

### 4. 运维与文档

- `probe`：本地相册输出 `{"source": "local_dir", ...}` 与文件清单，不再打 HTTP。
- `jobs invite create --album X`：X 既不是本地目录、库里也没有该相册的照片时
  打印 warning（不阻断 —— 允许先发码后建库的顺序）。
- `.env.example`、`docs/data-source.md`（新增「本地相册」一节）、README
  「已知局限」、`docs/architecture.md` 同步更新。

## 非范围

- **无 DDL 变更**（迭代流程第 2 步跳过）：album 仍是 slug、`photo_url` scheme
  已承载来源信息，不建 album 元数据表。
- 不做上传界面/管理端：指定本地素材 = 把目录放进 `SOURCE_LOCAL_DIR`，运维动作。
- 不做本地目录的 inotify 自动建库：仍由 `make ingest ALBUM=x` / cron 触发。
- 不做 HEIC→JPEG 的浏览器兼容转码（见风险 3）。
- 邀请码机制不动。

## 验收标准

1. `SOURCE_LOCAL_DIR/<slug>/` 放入照片后 `make ingest ALBUM=<slug>` 成功建库，
   库里 `photo_url` 为 `local://album/<slug>/...` 形态。
2. 用绑定该相册的 search 码登录：检索、浏览翻页、缩略图、**原图**全部可用；
   用绑定该相册的 edit 码走一次剪辑流程（建库 → 选片 → 渲染）成功出片。
3. 混存验证：库里同时有远端与本地相册时,`face-thumbs` 回填对两类照片都能取到
   字节;远端相册的原图仍是 302,行为无回归。
4. 越权语义不变：A 相册的码访问 B（本地）相册的照片/原图 → 404。
5. 路径安全单测：构造带 `..`/绝对路径的 photo_url,api 返回 404 且不读根外文件。
6. `make test`、`make lint` 全绿；eval 链路（依赖 `photo_url_for`）不回归。

## 风险与取舍

1. **slug 冲突（本地目录与远端相册同名）**：本地优先。误建目录会遮蔽远端相册 ——
   靠 warning 日志 + 文档约定兜底；不引入显式注册表是刻意的（少一份要维护的状态，
   与「album 就是 slug、不建 album 表」的既有决策一致）。
2. **`list_albums` 联合列表依赖远端可用性**：远端超时不应让本地相册的全量建库
   失败 —— 降级为仅本地并计入 stats.errors。
3. **HEIC/HEIF 原图浏览器不能直接显示**（本地目录允许该后缀）：缩略图是 WebP
   不受影响；原图点开表现为下载。记入 README 已知局限，转码另立计划。
4. **api 新挂一个宿主机目录**：攻击面增量有限（只读、越权仍 404、路径校验），
   但部署文档必须写清目录属主与权限（建议 root:root 644，api 容器内只读）。
5. **「目录存在与否」决定路由**是隐式约定：换来的是零配置零迁移。若日后需要
   显式声明（例如同名冲突常态化），再加 `album_source` 注册表也只是追加式演进。

## 改动清单（预估）

| 文件 | 动作 |
| --- | --- |
| `libs/gallery_core/local_source.py` | 新增：URL↔路径换算（单一定义点） |
| `jobs/sources/composite.py` | 新增：CompositeAdapter |
| `jobs/sources/__init__.py` | `build_adapter` 支持 `auto` |
| `jobs/sources/local_dir.py`、`jobs/eval.py` | 改从 gallery_core import 换算函数 |
| `libs/gallery_core/config.py` | `source_adapter` 加 `auto` 并设为默认 |
| `api/app/routers/photos.py` | original 按 scheme 分流 + 本地流式分发 |
| `jobs/render.py` | 本地照片缺副本时经 adapter 回源 |
| `jobs/__main__.py` | probe 本地分支;invite create 的相册存在性 warning |
| `docker-compose.yml` | api 只读挂载素材目录 |
| `.env.example`、`docs/data-source.md`、`README.md`、`docs/architecture.md` | 文档 |
| `api/tests`、`jobs/tests` | 路由分发、路径安全、冲突优先级、probe 本地分支 |
