# 0010 本地路径素材相册

## 背景与目标

目前素材来源由全局开关 `SOURCE_ADAPTER` 决定：要么整库走 photos.zrc.sg
（`static_gallery`），要么整库走本地目录（`local_dir`，定位是开发与评估）。
实际需求是**按相册混用**：有些相册来自源站，有些相册直接指定宿主机上的本地路径
（例如没有发布到照片墙的活动素材）。邀请码照旧一码一相册，持码人不感知也不需要
感知素材在哪 —— 检索、浏览、剪辑的体验与远端相册完全一致。

一句话：**素材来源从「部署期全局选择」变成「相册粒度的运行时路由」。**

**本地相册的存放位置定为 `/photo-gallery/media/<album>/`（容器内路径，即
`{media_root}/media`，与剪辑域原片目录合一）。** 不另设独立的本地素材盘：
这块专用盘已经存在、jobs/worker 已经整盘挂载，且本地相册的视频天然落在
拆条/ffmpeg 需要的位置上，剪辑建库不用再搬运一次。代价是这个目录从此有
两种住户 —— 远端相册的视频副本（media-ingest 下载的）和本地相册的原始素材 ——
来源判定因此不能只看目录存在与否，见「路由规则」。

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
3. **api 容器没有挂载 media 目录**（现状是刻意不挂），本地原图想代理分发也
   读不到文件。
4. `local://` 的 URL↔路径换算规则只定义在 `jobs/sources/local_dir.py`
   （`photo_url_for`），api 不应 import jobs 包。
5. **来源判定的陷阱**：`media/<album>/` 目录同时住着远端相册的视频副本与
   本地相册的原始素材，「目录存在」不能作为本地相册的判据（详见路由规则）。

## 范围

### 1. 路由 adapter（核心）

新增 `jobs/sources/composite.py`：`CompositeAdapter` 同时持有
`LocalDirAdapter` 与 `StaticGalleryAdapter`，实现同一个 `SourceAdapter` 协议：

- `list_albums()`：`{media_root}/media` 下的一级子目录 ∪ 远端相册列表
  （远端失败时降级为仅本地并告警）。
- `list_assets(album)`：按下面的**路由规则**选本地或远端实现。
- `open_asset(asset)` / `fetch_thumbnail(asset)`：**按 URL scheme 分发**
  （`local://` → 本地读，其余 → HTTP）。回填、重算、渲染等拿存量 URL 取字节的
  场景因此天然支持混存，与「相册当前算本地还是远端」无关。

`SOURCE_ADAPTER` 增加 `auto` 值并作为**新默认**；`local_dir` / `static_gallery`
保留为强制单源模式。`auto` 的本地根**固定为 `{media_root}/media`**，不复用
`SOURCE_LOCAL_DIR` —— 后者继续专属于 `local_dir` 单源模式（开发与评估集），
两条路径互不干扰，eval 链路零改动。

**路由规则（相册粒度，按优先级）：**

1. **库里已有该相册的照片记录 → 沿用记录的 scheme。** `photo.photo_url` 是
   持久化的来源事实：已是 `https://` 的相册永远走远端，已是 `local://` 的永远走
   本地。这一条挡住了关键陷阱 —— 远端相册跑过剪辑建库后 `media/<album>/` 里会有
   下载的视频副本，若只看「目录存在」，下一次 face ingest 会把它误判成本地相册，
   同一相册在库里裂成两套 URL（典型的「能跑但结果全错」）。
2. **库里没有记录（首次入库）→ `media/<album>/` 目录存在则本地，否则远端。**
   这是「指定本地路径素材」的唯一运维动作：把目录放进去再跑 ingest。
3. 冲突（库里是远端记录但目录也存在）→ 沿用远端 + warning 日志，绝不静默改道。

规则 1 需要查库，而 `SourceAdapter` 协议是无 DB 依赖的 —— 判定逻辑放在 jobs 侧的
小 helper（`resolve_album_source(session, album) -> "local" | "remote"`）里，
pipeline / media_ingest 在进入相册前调用，把结果告诉 composite；
composite 自身只做无状态分发。规则写进 `docs/data-source.md`。

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
  `${MEDIA_ROOT_HOST:-./photo-gallery-data}/media:/photo-gallery/media:ro`。
  这推翻了原注释「api 不挂 media —— 原片只有 jobs/worker 需要碰」：本地相册的
  原图分发让 api 也需要读原片了。挂载仍是**只读**，且 output（用户交付物）的
  隔离不变；compose 里的注释要一并改掉，说明新理由。

### 3. 剪辑域打通

- `jobs/worker.py` / `media-ingest` 构建 adapter 的入口换成 `build_adapter()`
  返回的 composite（入口本身不变，行为随新默认值升级）——
  worker 认领本地相册的建库/渲染任务即可工作。
- **本地相册的剪辑建库几乎是免费的**：素材本来就在 `media/<album>/`，
  正是 `media_ingest` 现有 local-only 扫描路径的输入 —— 视频无需下载或复制，
  照片原地分析，`media_asset.path` 直接指向源文件。主要工作是把
  `resolve_album_source` 接进 worker 的建库任务，本地相册跳过远端列举。
- `render.py` 现有的「本地文件优先」逻辑对本地相册天然成立（path 列有值、
  文件就在盘上）；`local://` 且文件缺失仍报错 —— 此时文件是真被移动/删除了，
  报错文案保持现状即可，不再需要「回源目录重读」的兜底。

### 4. 运维与文档

- `probe`：本地相册输出 `{"source": "local_dir", ...}` 与文件清单，不再打 HTTP。
- `jobs invite create --album X`：X 既不是本地目录、库里也没有该相册的照片时
  打印 warning（不阻断 —— 允许先发码后建库的顺序）。
- `.env.example`、`docs/data-source.md`（新增「本地相册」一节）、README
  「已知局限」、`docs/architecture.md` 同步更新。

## 非范围

- **无 DDL 变更**（迭代流程第 2 步跳过）：album 仍是 slug、`photo_url` scheme
  已承载来源信息，不建 album 元数据表。
- 不做上传界面/管理端：指定本地素材 = 把目录放进 `/photo-gallery/media/`，运维动作。
- 不改 `SOURCE_LOCAL_DIR` 的含义：它仍只服务 `local_dir` 单源模式（开发/评估）。
- 不做本地目录的 inotify 自动建库：仍由 `make ingest ALBUM=x` / cron 触发。
- 不做 HEIC→JPEG 的浏览器兼容转码（见风险 3）。
- 邀请码机制不动。

## 验收标准

1. `/photo-gallery/media/<slug>/` 放入照片后 `make ingest ALBUM=<slug>` 成功建库，
   库里 `photo_url` 为 `local://album/<slug>/...` 形态。
2. 用绑定该相册的 search 码登录：检索、浏览翻页、缩略图、**原图**全部可用；
   用绑定该相册的 edit 码走一次剪辑流程（建库 → 选片 → 渲染）成功出片。
3. 混存验证：库里同时有远端与本地相册时,`face-thumbs` 回填对两类照片都能取到
   字节;远端相册的原图仍是 302,行为无回归。
   **回归重点**：对一个已入库的远端相册先跑 `media-ingest`（使 `media/<album>/`
   出现视频副本）再跑一次 face ingest —— 相册必须仍按远端处理（路由规则 1），
   库里不得出现该相册的 `local://` 行。
4. 越权语义不变：A 相册的码访问 B（本地）相册的照片/原图 → 404。
5. 路径安全单测：构造带 `..`/绝对路径的 photo_url,api 返回 404 且不读根外文件。
6. `make test`、`make lint` 全绿；eval 链路（依赖 `photo_url_for`）不回归。

## 风险与取舍

1. **slug 冲突（本地目录与远端相册同名）**：库里已有记录 → 记录的 scheme 说了算
   （见路由规则 1，绝不静默改道）；全新相册撞名 → 本地优先 + warning。
   不引入显式注册表是刻意的（少一份要维护的状态，与「album 就是 slug、
   不建 album 表」的既有决策一致）；若日后冲突常态化，加 `album_source`
   注册表也只是追加式演进。
2. **`list_albums` 联合列表依赖远端可用性**：远端超时不应让本地相册的全量建库
   失败 —— 降级为仅本地并计入 stats.errors。
3. **HEIC/HEIF 原图浏览器不能直接显示**（本地目录允许该后缀）：缩略图是 WebP
   不受影响；原图点开表现为下载。记入 README 已知局限，转码另立计划。
4. **api 新挂 media 目录（只读）**：攻击面增量有限（只读、越权仍 404、路径校验），
   但从此 api 能读到**全部相册**的原片 —— 相册边界完全靠 `_load_photo` 的
   scope 校验兜住，`local://` 分发必须走同一个入口，不允许绕过它的新代码路径。
   部署文档写清目录属主与权限（建议 root:root 644）。
5. **本地相册与剪辑原片共用目录**：本地相册的文件被 media-ingest 视为素材是
   预期行为（这正是合一的收益）；反向的误判由路由规则 1 挡住。剩余风险是
   人为把文件手动拷进远端相册的目录 —— 这些文件会被剪辑建库收编
   （`local://<album>/<name>` 回退，现状已如此），但不会进 face 检索库。
   在 `docs/data-source.md` 里把这条边界写明。

## 改动清单（预估）

| 文件 | 动作 |
| --- | --- |
| `libs/gallery_core/local_source.py` | 新增：URL↔路径换算（单一定义点） |
| `jobs/sources/composite.py` | 新增：CompositeAdapter（本地根 = `{media_root}/media`） |
| `jobs/sources/__init__.py` | `build_adapter` 支持 `auto`;新增 `resolve_album_source`（查库判来源） |
| `jobs/sources/local_dir.py`、`jobs/eval.py` | 改从 gallery_core import 换算函数 |
| `libs/gallery_core/config.py` | `source_adapter` 加 `auto` 并设为默认 |
| `api/app/routers/photos.py` | original 按 scheme 分流 + 本地流式分发 |
| `jobs/pipeline.py`、`jobs/media_ingest.py`、`jobs/worker.py` | 进入相册前调用 `resolve_album_source` |
| `jobs/__main__.py` | probe 本地分支;invite create 的相册存在性 warning |
| `docker-compose.yml` | api 只读挂载 `media/`（并更新原注释的理由） |
| `.env.example`、`docs/data-source.md`、`README.md`、`docs/architecture.md` | 文档 |
| `api/tests`、`jobs/tests` | 路由分发、路径安全、冲突优先级、probe 本地分支 |
