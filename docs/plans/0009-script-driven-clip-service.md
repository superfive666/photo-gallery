# 0009 — 剧本驱动的选片+剪裁服务（media-clip）

> **合入 main 时的适配说明**：main 已先行合并 plans/0006（邀请码入库，prefix.secret
> 一码一相册）与 0008（视频人脸检索），因此本计划落地时做了两处对齐：
> ① 不建 edit_invite 表，改为给既有 invite_code 表加 role 列（edit 码必绑相册，
> 行 id 即 workspace_id）；② 迁移编号为 005、本文档编号为 0009。

状态：**第一版已实施**（P1~P4 的主体 + 反馈闭环 + 断点恢复 + agent 框架，
一次合入；未含 scene_face 人物过滤、in/out 微调 UI、FCPXML/EDL，见
docs/media-edit.md「已知局限」。阈值标定等 P0 事项仍开放）。设计基线为第 9 版。
第 2 版合入：去 whisper；画质指标融合排序；单次人工评审必经；剧本润色；
邀请码分查找/剪辑双角色双 UI。
第 3 版合入：转码质量说明与输出档位（含无损直切）；滤镜库升级为
**数据库表 + docker job 导入**；评审页选滤镜 → 渲染时烧入的完整生效路径。
第 4 版合入：**评审反馈闭环** —— 评审页逐镜头标记满意/不满意；满意的锁定
选片，不满意的收集用户补充 idea/提示词后重新生成重检，新一轮必须携带
上一轮完整上下文；循环直到全部满意才进渲染。
第 5 版合入：**断点恢复与跨设备会话** —— 剪辑码即工作区身份，任意设备登录
同码即同会话；全流程（提示词、剧本、候选、选择、反馈、渲染）记入事件
时间线；UI 改为 agent 聊天窗形态，关掉页面/换设备登录后完整看到历史
并从断点继续。
第 6 版合入：**LLM 接入与服务端 Agent 框架** —— 大模型走 OpenAI-compatible
协议，base_url/api_key/model 全部环境变量可配（支持私有自托管模型）；
系统提示词与技能（skill）全部配置在服务端 api 服务内：提示词是版本化
模板文件、技能是代码注册表，agent 流程由固定状态机驱动。
第 7 版合入：按邀请码哈希分区的磁盘布局（已被第 8 版取代）。
第 8 版合入：**素材库全局共享、按相册组织、按需下载** —— media 不再带
workspace 哈希前缀，统一放 `/photo-gallery/media/<album>/`；首次引用触发
下载原片 + 建库，同相册后续任务直接复用。workspace 隔离只保留在
项目/轮次/事件/输出上。
第 9 版合入：**一码一相册** —— 剪辑码在发码时绑定唯一相册（与查询侧同
逻辑），用户没有选相册的权限；剪辑码入 `edit_invite` 表（存 argon2 hash +
绑定相册），workspace_id 即该表行 id，取代第 5 版的 HMAC 派生方案。

## 需求一句话

给定一个素材库（照片 + 视频）和一份写好的剪辑剧本/分镜描述，服务自动完成
**选片 → 剪裁 → 简单滤镜**，输出按镜头编号组织的视频片段和照片，
最终的编排/排版/转场/配乐交给人工后期在 NLE 里完成。

## 目标

1. 素材库离线建索引：视频拆镜头、抽关键帧，照片直接入库，全部做语义 embedding，
   **同时计算画质指标**（清晰度、曝光、拍摄稳定性、分辨率档位）。
2. 剧本解析：先由大模型**润色补全**（用户的剧本已较详细，润色目标是补足检索所需的
   具象视觉信息，而不是重写），再拆成结构化镜头清单。模型经 OpenAI-compatible
   协议接入，URL/key/model 环境变量可配，私有模型即插即用。
3. 每个镜头检索 Top-5 候选：**语义相似度 + 画质分融合排序**，不是纯 KNN。
4. **人工评审（必经步骤，可多轮）**：逐镜头标记满意/不满意。满意 → 从候选中
   选定并锁定；不满意 → 填补充 idea/提示词，触发新一轮「润色→重检」，
   新一轮携带上一轮全部上下文，锁定镜头不受影响。循环直到全部满意。
5. **滤镜库**：数据库管理，支持管理员用 docker job 批量导入自备的 LUT 模版，
   评审页选中的滤镜在渲染时烧入片段。
6. 一键渲染：ffmpeg 帧精确剪裁（前后各留 1s 余量），保持源片原分辨率、
   视觉无损档位输出，+ manifest.csv（可选 FCPXML/EDL），打包下载。
7. **断点恢复**：剪辑流程的每一步都持久化在服务端并记入事件时间线，
   关页面、换设备、cookie 过期都不丢进度 —— 同一个剪辑码从任何设备登录，
   看到完整历史，从上次停下的地方继续（补提示词/选片/触发渲染）。

## 非目标（明确不做）

- 自动成片：不做时间线编排、转场、卡点、配乐、字幕合成 —— 那是后期的活。
- 语音检索：**不部署 whisper**，不做台词级索引（已评审确认不需要）。
- 自由式调色：滤镜只从滤镜库里选（LLM 可推荐默认值），不做逐镜头自由调色 ——
  但滤镜库本身可扩充（导入 job）。
- 人物身份库：照旧**不建 person 表、不做聚类**（约束 8 不变）。
  「筛出含某人的镜头」用参考照实时 KNN，和现有自拍检索同一模式。

## 现状评估：能复用什么、缺什么

| 现有组件 | 复用方式 |
| --- | --- |
| `db`（pgvector + HNSW） | 直接复用。新增几张表，向量检索沿用「内层 ORDER BY+LIMIT、外层过滤」两层结构（约束 7） |
| `embedding` 服务 | 扩展。人脸能力原样复用（关键帧上跑人脸 → 支持"含某人"过滤）；**新增 CLIP 图文双塔端点**，继续满足约束 3「预处理只此一处」 |
| `jobs` 离线管线 | 复用骨架（job_run 记录、幂等、失败重试），新增 `media-ingest` / `filters-import` / `render` 子命令与常驻 `worker` 模式（按需建库） |
| `api` 鉴权 | 扩展。单邀请码 → **双邀请码分角色**（见下），JWT/session/限流机制原样复用 |
| `web` | 扩展。按角色分流：查找类走现有页面不动；剪辑类走新的对话框 + 评审流程 |
| 迁移/uv workspace/CI 约定 | 全部沿用 |

**缺口（需要新增的能力）：**

1. **视频拆条**：ffmpeg/ffprobe + PySceneDetect（内容感知切分），抽关键帧。
2. **图文检索模型**：剧本是中文 → 选 **Chinese-CLIP**（图、文各出 512 维向量，
   与现有 `vector(512)` 惯例一致）。这是本需求唯一必须新部署的常驻推理模型。
3. **画质指标计算**：纯 OpenCV/ffmpeg 计算，无需模型（见「画质指标」一节）。
4. **剧本润色 + 分镜解析**：调**可配置的 OpenAI-compatible LLM**（私有自托管或
   任意兼容服务，见「LLM 接入与服务端 Agent 框架」，不占本地推理资源）。
5. **滤镜库**：`filter_preset` 表 + `jobs filters-import` 导入管线 + 预览端点。
6. **剪裁渲染**：ffmpeg 精确 trim + LUT 滤镜链，jobs 里新增 render 命令即可，
   不需要新容器。
7. **文件存储**：片段/输出文件太大不能进 BYTEA，走专用盘挂载
   `/photo-gallery`：素材全局共享 `media/<album>/`，输出按码隔离
   `output/<workspace_id>/`（见「磁盘布局」一节）。

### 新模型/组件清单（部署视角）

| 组件 | 放哪 | 体量 | 说明 |
| --- | --- | --- | --- |
| Chinese-CLIP ViT-B/16（ONNX） | `embedding` 容器新端点 | ~400 MB，512 维 | 唯一新增常驻模型。GPU 上千帧/分钟；CPU ~10-20 帧/s，离线建库也够 |
| ffmpeg + ffprobe + PySceneDetect + OpenCV | `jobs` 镜像 | apt/pip 依赖 | 拆条、抽帧、画质指标、剪裁、滤镜烧入 |
| LLM（OpenAI-compatible，`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` 环境变量配置） | 外部或自托管 API | — | 剧本润色 + 分镜解析 + 生成检索 query + 推荐滤镜 + 多轮重生成。私有模型即插即用；换 Claude/OpenAI 只是换三个环境变量 |
| 滤镜库 | `filter_preset` 表（LUT 字节入库） | 每个 .cube 几百 KB | 内置默认集随部署导入；自备模版放 `/photo-gallery/luts` 跑 `jobs filters-import` |
| CLIP-aesthetic 线性头（可选，P5） | `embedding` | 几 MB | 启发式画质分不够用时再上 |

不新增容器；`embedding` 内存预算需从 8g 评估上调（buffalo_l + Chinese-CLIP 共存）。

## 画质指标（建库时逐 scene 计算，入库为列）

候选不能只看语义相关度，拍摄质量必须参与排序。指标全部是便宜的
CPU 计算，在 media-ingest 时一次算好：

| 指标 | 算法 | 归一化 |
| --- | --- | --- |
| `resolution_tier` | 取自 ffprobe 的高度：2160(4K)→1.0，1440(2K)→0.85，1080→0.7，720→0.4，更低→0.2 | 0~1 |
| `stability` | 对镜头内等间隔采样帧（降采样后）做相邻帧全局运动估计（OpenCV 相位相关/稀疏光流），取运动向量的高频抖动方差映射到 0~1。手持抖动分数低，稳定/架机位分数高。有意的匀速运镜（pan）是低频运动，不会被误伤 | 0~1 |
| `sharpness` | 关键帧 Laplacian 方差（多关键帧取均值） | 0~1（按库内分布分位归一） |
| `exposure` | 亮度直方图过曝/欠曝裁剪比例 | 0~1 |

综合画质分（列 `quality_score`，权重可配、P0 标定）：

```
quality = 0.30*resolution_tier + 0.30*stability + 0.25*sharpness + 0.15*exposure
```

### 融合排序（约束 7 的两层结构不变）

```sql
-- 内层：纯向量 KNN 取候选（吃 HNSW）
SELECT ... ORDER BY embedding <=> :q LIMIT 200
-- 外层：硬过滤 + 融合重排
WHERE similarity >= :sim_floor          -- 相似度硬门槛，防"高清但不相关"上位
  AND stability >= :min_stability       -- 废片硬门槛（可按项目关）
  AND duration_ms BETWEEN ...           -- 时长适配
ORDER BY 0.65*similarity + 0.35*quality_score DESC
```

要点：**画质只在"已相关"的候选之间起排序作用** —— 先过相似度硬门槛，融合权重
也设上限（画质 ≤ 0.35），保证不会出现"画质极好但和剧本无关"的候选挤掉正确答案。
多条检索 query 的结果用 RRF 融合后再取 Top-5 写入 shot_candidate。
权重与门槛按项目可配，默认值在 P0 用真实素材标定（沿用 `make eval` 模式）。

## 邀请码分类与双 UI

现状：单邀请码，argon2 hash 存 `INVITE_CODE_HASH` 环境变量，登录发 JWT
session cookie。扩展为**查找码照旧走环境变量，剪辑码入表、一码一相册**：

- **查找类不动**：`INVITE_CODE_HASH` 环境变量原样保留，老会话不失效。
- **剪辑码进 `edit_invite` 表**（见数据模型）：一行一个码，存 argon2 hash +
  **绑定的相册 slug** + enabled 开关。一码一相册是产品语义：用户拿到码就等于
  拿到"用这个相册剪辑"的权限，**没有选相册的入口**；多个码可以绑同一个相册
  （素材共享，各自的项目/输出隔离）。
- 发码走 `jobs invite-create --album <slug>`：getpass 输入邀请码（不进 shell
  history），落 argon2 hash 入表并打印 workspace_id。吊销 = `enabled=false`。
- 登录顺序：先试查找码 env hash → 不中再逐行 verify `edit_invite` 中 enabled
  的行（码的数量是"每场活动几个"的量级，argon2 逐行验证毫秒~百毫秒级，且登录
  本就限流防枚举）。命中后 JWT 写 `role: "edit"` + `workspace_id` + `album`。
- `require_session` 拆出 `require_role("search"|"edit")` 依赖：查找接口要 search，
  剪辑接口要 edit，互不越权。`/session/me` 返回 role（edit 时含相册名），
  前端据此分流。无 `role` 的存量 token 按 `search` 处理。
- 限流、审计、consent 流程原样复用。

两套 UI 流程：

```
查找类（现状不动）：邀请码 → 自拍上传 → 相册浏览/结果网格

剪辑类（新流程，agent 聊天窗形态，评审是唯一的人工环节，可循环多轮直到满意）：
  邀请码 → 进入工作区：会话列表（历史剪辑项目）+「新建剪辑」
  → 打开一个会话 = 一条聊天时间线（详见「工作区与断点恢复」）
  → 新建：在输入框粘贴剧本 → 提交（相册无需选、也无从选：码上绑定的那一个）
  → 后台自动：（该码绑定的相册若未入库，先下载+建库，进度卡片实时更新）
    → LLM 润色 → 分镜 → 逐镜头融合检索（范围=码绑定的相册）
    （进度以 assistant 消息卡片形式追加进时间线）
  → 【评审卡片】（内嵌在时间线里）逐镜头先做二选一：😊 满意 / 😞 不满意
      满意的镜头：
      · 从 Top-5 候选（缩略图 + 悬停预览）中勾选一条 → 该镜头锁定 ✅
      · 滤镜下拉（选项来自滤镜库，默认 = LLM 推荐值），关键帧实时预览
      · in/out 点微调
      不满意的镜头：
      · 展开反馈框：填补充 idea / 更具体的提示词（想要什么、不要什么）
      · 页面底部另有一个项目级补充框（整体风格/氛围类的意见）
  → 若存在不满意镜头：点「重新生成」→ 走一遍反馈闭环（见下节）→ 回到本页（第 N+1 轮）
  → 全部镜头锁定后「确认渲染」才可点
  → 渲染任务排队（jobs render，选中的滤镜此时烧入）→ 下载 zip + manifest
```

润色稿的确认合并进评审页：每个镜头直接展示润色后的描述，微调走"就地改文字重检
单镜头"（不用 LLM），大改走"不满意 + 反馈重新生成"（用 LLM）—— 人工环节始终
只有评审这一个界面，只是可以循环。

## 评审反馈闭环（不满意 → 带上下文重新生成）

用户点「重新生成」后，后台对**未锁定的镜头**重跑「润色 → 检索」，关键是新一轮
的 LLM 输入不是从零开始，而是把历史完整带上：

```
第 N+1 轮 LLM 输入 = 原始剧本
                   + 上一轮润色稿全文（含已锁定镜头，供保持整体风格一致）
                   + 每个不满意镜头：上轮描述 + 上轮检索 query
                                    + 上轮 Top-5 候选摘要（讲清"当时找到了什么"）
                                    + 用户对该镜头的反馈原文
                   + 项目级补充意见（每一轮的都累积保留，不只是最近一轮）
输出 = 仅未锁定镜头的新描述 + 新检索 query（锁定镜头原样带过，禁止改写）
```

约定：

- **锁定镜头绝不动**：不重新润色、不重新检索、选片/滤镜/in-out 保持原样。
  「整个流程重新来一遍」只作用于不满意的部分 —— 已确认的成果不能被新一轮冲掉。
- **负反馈生效**：上一轮被用户明确看过且不满意的候选（该镜头当轮的 Top-5）
  标记 rejected，新一轮检索在外层过滤中排除这些 scene，保证换血而不是复读。
- **每一轮都留痕**：轮次、用户补充输入、当轮 shot list 快照存 `edit_round` 表，
  LLM 组装上下文时按轮次顺序全量读取 —— 这就是"上一把的背景 context 和这一次
  的额外输入一起匹配"的落地方式。
- 不设硬性轮数上限，但 UI 上显示当前第几轮；若三轮后仍不满意，提示用户
  检索能力可能到顶（素材库里可能真的没有这个镜头），建议改剧本或补素材。

## 工作区与断点恢复（剪辑码即会话）

需求：剪辑码不管从哪台设备登录都是同一个会话；全流程像 agent 聊天窗一样
完整留痕；关掉页面/换设备再登录，看到全部历史并从断点继续。

### 身份：稳定的 workspace_id，而不是每次登录随机的 sid

现状的 session cookie 每次登录生成随机 sid —— 对查找类合适（用完即弃），
对剪辑类不行（换设备就断）。剪辑类改为：

- **workspace_id = `edit_invite` 表的行 id**（uuidv7）。登录验证剪辑码命中哪
  一行，就是哪个 workspace —— 同一个码在任何设备、任何时间登录都落到同一行，
  身份天然稳定；明文码照旧只存 argon2 hash，不可反推。
  （第 5 版曾设计 HMAC 派生 id；第 9 版剪辑码入表后行 id 更直接，HMAC 方案废弃。）
- 剪辑域所有数据（项目、轮次、事件、输出目录）都挂在 workspace_id 下。cookie
  只是钥匙，**状态全部在服务端** —— cookie 过期/清除浏览器数据/换手机，
  重输邀请码即恢复，什么都不丢。
- 码与相册的绑定也在这一行上：检索、建库触发的相册范围直接读
  `edit_invite.album`，用户无从越出。

### 磁盘布局：素材全局共享、按相册组织（专用盘挂载 /photo-gallery）

宿主机专门挂一块盘到 `/photo-gallery`。**素材库是全局一份、按相册分子目录**，
不带 workspace 哈希前缀 —— 同一个相册的原片全站只存一份，所有剪辑任务复用：

```
/photo-gallery/
├── media/                       # 全局素材库，按相册组织
│   ├── 2026-08-10/              #   = 源站相册 slug，首次被某个编辑任务引用时下载
│   │   ├── xxx.jpg / yyy.mp4    #   官方原片，落盘后只读
│   └── 2026-09-01/
├── output/                      # 渲染产物，按工作区隔离（交付物是用户私有的）
│   └── <workspace_id>/<project>/<序号>_<slug>/...
├── luts/                        # 滤镜导入目录（filters-import 扫这里）
└── agent-prompts/               # 可选：提示词模板覆盖
```

- **media 按需填充**：相册来自剪辑码的绑定（一码一相册，用户不选）。
  某相册首次被任何码的任务引用 → 触发「下载原片到 media/<album>/ + 建库
  （拆条/embedding/画质指标）」，时间线上以进度卡片呈现；已下载过的相册
  直接复用，秒级进入润色检索。下载解析复用 jobs 里现成的源站解析逻辑
  （static_gallery）。
- media 里是**未经处理的官方原片**，落盘后全程只读——分析、剪裁、转码的产物
  一律写去 output，原片永不改写，也不生成代理文件（评审页预览用建库时入库的
  关键帧缩略图）。放进来的是什么质量，出片天花板就是什么质量。
- **素材共享、成果隔离**：`media_asset`/`scene` 不带 workspace_id，只带
  `album`（与 photo 表同语义的源站 slug）；检索范围 = 该码绑定的相册
  （外层过滤 `album = :album`，值取自 JWT/`edit_invite`，pgvector ≥ 0.8 开
  `hnsw.iterative_scan=relaxed_order` 保证过滤后不漏召回）。
  项目、轮次、事件、输出仍按 workspace_id 隔离 —— 别的码看不到你的剪辑任务
  和成片；绑同一相册的码共享同一份素材与索引（与查找类共享同一批公开相册，
  信任模型一致）。
- **按需建库需要一个常驻执行者**：api 收到"引用了未入库相册"时写一条
  ingest 请求（复用 job_run），由常驻的 `jobs worker`（同一镜像加 `worker`
  子命令、`restart: unless-stopped`）拾取执行：下载 → 拆条 → embedding →
  落库，并逐步追加时间线事件。管理员手动 `jobs media-ingest --album <slug>`
  预热大相册的路径保留。
- 挂载关系：`jobs`（含 worker）挂整个 `/photo-gallery`（media 写入仅限下载
  新相册，其余只读；output 读写）；`api` 只读挂 output（分发下载 zip）；
  `embedding` 不挂（字节走 HTTP）。
- workspace_id（= `edit_invite` 行 id）继续作为会话/项目/输出的隔离键，
  只是不出现在 media 路径里；output 子目录由 render 首次写入时自动创建，
  管理员无需手动建目录（发码时 `jobs invite-create` 会打印 workspace_id
  供排查数据归属）。

### 留痕：事件时间线（聊天窗的数据来源）

新表 `project_event`：项目内一条追加式（append-only）事件流，前端聊天窗就是
按顺序渲染这张表：

| 字段 | 说明 |
| --- | --- |
| `project_id` / `seq` | 项目内单调递增序号，(project_id, seq) 唯一 |
| `actor` | user / assistant / system |
| `kind` | script_submitted, polish_done, candidates_ready, shot_locked, shot_feedback, regenerate_started, filter_selected, inout_adjusted, render_started, render_done, render_failed … |
| `payload` | JSONB。存展示所需的小快照 + 对状态实体的 id 引用（候选缩略图等大对象走既有接口按 id 取，不内嵌进事件） |
| `created_at` | — |

要点：

- **事件流是展示层，状态表是事实层。** shot/shot_candidate/edit_round 仍是唯一
  可信状态；事件只追加、绝不改写，两者靠 id 关联。断点恢复 = 读状态表接着干活，
  读事件表还原聊天记录 —— 二者天然一致，不会出现"聊天记录说锁了、状态却没锁"。
- **LLM 上下文组装照旧读 edit_round**（第 4 版设计不变），事件流不参与 prompt ——
  避免把 UI 噪音（滚动、点开预览之类不记录，只记录有语义的动作）喂给模型。
- 每一次用户的额外提示词都是一条 user 事件 + 落入当轮 edit_round —— 满足
  「每一次额外提示词都带上前面全部 context」。

### 断点恢复的具体语义

```
重新登录（任意设备）→ GET /api/projects            → 会话列表（按最近活动排序）
打开某个项目        → GET /api/projects/{id}/events?after_seq=N → 增量拉时间线
                    → 项目当前状态（reviewing 第几轮 / rendering 进度 / done）
                      决定输入框和评审卡片的可交互态
继续操作            → 所有写接口本来就是状态机驱动（第 4 版），从哪个设备调用无区别
渲染中断点          → render 是 jobs 里的后台任务，用户掉线不影响；回来时
                      时间线里已有 render_done 事件和下载卡片
```

轮询用 `after_seq` 增量拉取（复用查找类已有的轮询习惯，暂不引入 WebSocket；
渲染进度类事件秒级轮询足够）。

### 多设备并发写

同一个码同时在两台设备操作同一项目是允许的（时间线双方都实时可见），但写操作
带乐观并发控制：写接口携带项目 `state_version`，过期返回 409，前端拉最新时间线
后重试 —— 防止 A 设备点「重新生成」的同时 B 设备还在锁定镜头造成状态错乱。

### 与隐私约束的交界（重要）

- 事件时间线里存的是剧本文本、镜头描述、id 引用与计数 —— **不存人脸图片、
  不存 embedding**（约束 2 延伸适用）。
- 若剪辑流程用到「含某人」过滤：用户上传的参考照依旧**只在请求内存中存活**
  （约束 1 不变），时间线只记录"设置了人物过滤"这个事实 + 当时算出的匹配
  scene id 集合，参考照本身与其向量不落任何表 —— 断点恢复后人物过滤条件
  仍然有效（靠 scene id 集合），但照片已不可还原。

### 聊天窗 UI（参考 Claude Code / Codex 等 agent 移动端设计）

- 左侧（移动端为首页）：会话列表 = 该工作区的项目列表，显示标题（取剧本首行）、
  状态徽章（评审中第 2 轮 / 渲染中 / 已完成）、最近活动时间。
- 会话内：消息流 + 结构化卡片混排 ——
  · user 消息：剧本原文、每轮补充的提示词、反馈
  · assistant 卡片：润色稿（可展开）、进度条、评审卡片（候选网格 + 满意/不满意
    交互直接内嵌）、渲染完成卡片（下载按钮 + manifest 摘要）
  · 底部输入框：语义随状态变化（新建时"粘贴剧本…"，评审有不满意镜头时
    "补充你的想法…"，渲染完成后"开始新一轮剪辑…"）
- 时间线无限上翻即历史，天然就是「断点恢复看到所有记录」的形态。

## LLM 接入与服务端 Agent 框架

### 供应商可配：OpenAI-compatible，环境变量三元组

模型不写死。走 OpenAI-compatible 的 `/chat/completions` 协议，配置进
`gallery_core.config.Settings`（与现有配置同一套 pydantic-settings）：

```
LLM_BASE_URL=https://your-private-llm.example/v1   # 私有模型网关
LLM_API_KEY=...                                    # 供 token 方主导
LLM_MODEL=your-model-name
LLM_TIMEOUT_S=120        # 可选，默认值内置
LLM_MAX_RETRIES=2        # 可选
```

- 客户端用 `openai` SDK 的 `base_url` 覆写（或纯 httpx，实现时定），私有模型、
  Claude（经兼容网关）、OpenAI 互换只是换三个环境变量，代码零改动。
- **不假设私有模型支持 function calling / JSON mode**：结构化输出的兜底策略是
  「prompt 内嵌 JSON schema 约束 → 输出抽取 → pydantic 校验 → 失败把校验错误
  喂回去重试一次」；探测到 `response_format: json_object` 可用时才启用原生模式。
  这样换任何兼容模型都不挑能力。
- **溯源**：每轮 `edit_round` 记录当时的 `model` 名与提示词版本指纹（模板文件
  content hash）—— 和 face 表记 model_name/model_version 是同一个思想：换模型/
  改提示词之后，旧轮次的产出仍能说清是谁生成的。

### Agent 形态：固定状态机 + LLM 节点，不做自由 tool-loop

这个 app 的 agent 流程是**写死的**（用户要么满意、要么继续给提示词），这是
优点不是限制 —— 架构上就做成「状态机驱动、LLM 只在固定节点被调用」：

- 状态机（parsing → matching → reviewing ⇄ refining → rendering）由服务端代码
  驱动，**LLM 不决定"下一步做什么"，只在每个节点完成一件签好契约的事**
  （输入模板渲染好的 prompt，输出过 pydantic 校验的 JSON）。
- 好处：私有模型能力强弱只影响单节点输出质量，永远不会把流程带偏；每个节点
  可单测、可回放（edit_round 快照）；坏输出被 schema 挡住，不会脏到状态表。
- 不采用自由 tool-loop（让 LLM 自己决定调什么工具）：依赖模型的规划能力，
  私有模型未必稳，且本流程根本不需要 —— 检索、锁定、渲染的顺序是确定的。

### 代码布局：全部在 api 服务内（回答"配在 FastAPI 这边吗"——是）

润色/重生成都是在线流程，agent 框架作为 `api` 的子包，随代码部署即完成
"server side 配置好"；前端只发用户文本，系统提示词/技能/上下文组装全在服务端：

```
api/app/agent/
  client.py          # OpenAI-compatible 异步客户端（env 三元组、超时、重试）
  runner.py          # 状态机节点执行器：模板渲染 → 调 LLM → 抽取 → 校验 → 入库+记事件
  schemas.py         # 各节点输出契约（pydantic）：ShotList / RefinedShots / FilterRec
  prompts/           # 提示词即配置，git 版本化，改提示词=改文件+部署，不改代码
    system.md        #   系统提示词：角色设定（照片/视频剪辑助理）、输出纪律
    polish.md.j2     #   首轮：剧本润色+分镜+检索query（jinja2 模板）
    refine.md.j2     #   反馈闭环：组装历史轮次+反馈，重写未锁定镜头
    filter_rec.md.j2 #   从滤镜库现有预设中推荐
  skills/            # 技能注册表（见下）
    __init__.py      #   SKILLS = {name: Skill(...)}
    search_scenes.py #   融合检索（包装 ③ 的 SQL）
    list_filters.py  #   读滤镜库
    match_person.py  #   人物过滤（调 embedding 服务，参考照不落盘）
```

### skill 怎么配（回答"skill 配在哪、怎么配"）

一个 skill = 一个 Python 模块，注册进表里，三要素齐全：

```python
@register_skill
class SearchScenes(Skill):
    name = "search_scenes"
    description = "按镜头描述在素材库做语义+画质融合检索"
    Input  = SearchScenesIn    # pydantic：query、时长范围、排除 scene ids…
    Output = SearchScenesOut   # pydantic：Top-N 候选（id、分数、时码）
    async def run(self, inp): ...
```

- **在固定流程里，skill 由 runner 按状态机顺序调用**（如 refine 节点：先调 LLM
  重写描述 → 再调 search_scenes → 结果写 shot_candidate）。LLM 看不到也不需要
  看到 skill —— 它只产出描述和 query。
- **注册表同时自动导出 OpenAI tools JSON schema**（从 pydantic 生成），留一条
  升级路：若将来某个节点想放开成小范围 tool-loop（比如让模型自己决定"再检索
  一次换个说法"），把该节点的 skills 子集作为 `tools` 传给兼容接口即可，注册表
  与实现完全复用 —— 但默认不开，理由见上。
- 不配在数据库、不配在前端：skill 是有类型契约和测试的代码，进 git 走 CI；
  只有"用哪个模型"（env）和"提示词内容"（模板文件）是部署期可变的配置。
- 提示词模板若需要不重新部署就能调：可选支持从挂载卷
  `/photo-gallery/agent-prompts/` 读同名文件覆盖仓库默认值（读到就用、没有就用内置），
  实现成本一个 loader，P2 里顺手做。

## 滤镜库（支持手动导入模版）

滤镜不是仓库里的静态文件，而是**数据库管理的滤镜库**，像视频编辑软件的预设
面板一样可增删、可扩充：

- 新表 `filter_preset`（见数据模型）：一行一个滤镜。`slug` 唯一作幂等键；
  LUT 文件字节直接入库（.cube 几百 KB，BYTEA 走 TOAST 无压力）；带预览缩略图、
  显示名、启用开关、checksum、导入时间。
- **导入走 docker job**：`jobs filters-import --dir /photo-gallery/luts`。管理员把提前准备
  好的 .cube 模版放进挂载目录，job 依次：校验 LUT 格式 → 对一张标准测试图套用
  生成预览缩略图 → 按 slug upsert 入库。重复执行幂等；改了文件重跑即更新
  （checksum 变化则记新版本），删除用 `--disable <slug>` 软下架不删行。
- 内置默认预设（暖调/冷调/胶片/黑白等，P0 敲定清单）也走同一条导入管线
  （仓库 `assets/luts/` 随部署导入）—— 滤镜只有一张表、一个来源，
  没有「内置 vs 导入」两套逻辑。
- 亮度/对比度/饱和度这类 eq 微调作为预设行上的可选附加参数（JSONB），
  和 LUT 同行存储、同时生效。

### 评审页选滤镜 → 渲染烧入的生效路径

1. `GET /api/filters`（require_role("edit")）返回启用滤镜清单：显示名 + 预览缩略图。
2. 评审页项目级设默认、镜头级可覆盖；**实时预览**：
   `GET /api/scenes/{id}/preview?filter=<slug>` 对该候选的关键帧在线套 LUT 返回
   单帧 JPEG（毫秒级，ETag 缓存）—— 预览单帧即可，不渲染整段。
3. 选择结果落到 `shot.filter_preset_id`。
4. 评审确认后，render job 从库里取 LUT 字节写临时文件，进 ffmpeg
   `lut3d`（+eq）滤镜链烧入片段。
5. `render_output` 记录所用滤镜的 slug + checksum —— 日后滤镜文件被更新，
   也能说清「这段片子当时是用哪个版本调的」，可复现。

## 关于重编码与质量（评审问题 1 的回答）

**像素不打折**：默认不做任何缩放，输出保持源片原分辨率（4K 进 4K 出）。
是否统一分辨率是 P0 的交付规格决策，不是技术限制。

**质量档位**：重编码有理论损失，但可控制到视觉无损，且提供三档：

| 档位 | 编码 | 说明 |
| --- | --- | --- |
| 默认 | H.264 CRF 16 slow（10bit/HDR 源用 HEVC CRF 18） | 一代编码肉眼无法区分（VMAF≈99），体积适中 |
| 中间码（可选） | ProRes 422 / DNxHR HQ | 后期剪辑的行业标准中间格式，时间线拖动最顺滑，视觉无损，体积约 5~10 倍 |
| 无损直切（可选，仅限未选滤镜的镜头） | stream copy | **零重编码零损失**。切点向外吸附到源视频关键帧，误差恰好被 1s 余量吸收 |

- 选了滤镜的镜头必须重编码 —— 烧入 LUT 绕不开解码-处理-再编码。
- 未选滤镜的镜头可走无损直切档，一个比特都不动。
- 整条链路总共两代编码（本服务一代 + 后期成片导出一代），与常规剪辑工作流相同；
  若选 ProRes 中间码档，则与专业流程完全一致。
- 重编码**只发生在导出的片段文件上**，原素材永不改动。
- 每段导出片段在评审确认的 in/out 点基础上**前后各多留 1s 余量**，manifest 里
  同时记录「精确点」与「含余量点」两组时码，后期编排空间充足，也能凭源时码
  回原片精修。

## 数据模型（新增迁移 `005_media_edit.sql`，只追加）

素材库与 photos.zrc.sg 的 `photo`/`face` 是两个业务域，**不复用 photo 表**
（photo 以 `photo_url` 为幂等键、绑死相册语义），新开一组表：

```
media_asset ──1:N──▶ scene ──1:N──▶ scene_face
  (album)             (album)
edit_invite ──1:N──▶ edit_project ──1:N──▶ shot ──1:N──▶ shot_candidate ──▶ scene
（一码一相册）            │                   │                  │
                         │                   ▼                  ▼
                         ├─▶ edit_round   filter_preset    render_output
                         └─▶ project_event（追加式事件时间线，聊天窗数据源）
```

- `media_asset` — 素材一行（全局共享）：`album`（源站 slug，带索引）、
  `source_url`（唯一，幂等键，与 photo.photo_url 同思想）、`path`（本地落盘
  路径）、kind(image/video)、时长/分辨率/fps/codec、checksum、`resolution_tier`、
  processing_status（沿用 photo 的状态机写法）。
- `scene` — 检索的基本单元。视频：一个镜头一行（start_ms/end_ms + 1~3 张关键帧缩略图
  BYTEA）；照片：整张即一个 scene。带 `album`（冗余自 media_asset，检索外层
  过滤直接用，免 join）+ `embedding vector(512)`（Chinese-CLIP，出口 L2
  归一化，约束 4）+ `model_name/model_version/dim`（约束 5）+ 画质列
  （`stability`/`sharpness`/`exposure`/`quality_score`、face_count）。HNSW cosine 索引；
  按相册过滤时开 `hnsw.iterative_scan` 保证过滤后不漏召回。
- `scene_face` — 关键帧上的人脸向量（复用现有 buffalo_l 端点），支撑「含某人」过滤。
  仅存向量与 bbox，不命名不聚类。
- `filter_preset` — **滤镜库**：`slug`（唯一，幂等键）、显示名、LUT 字节（BYTEA）、
  预览缩略图（BYTEA）、eq 附加参数（JSONB）、checksum、`enabled`、导入时间。
- `edit_invite` — **剪辑码表（一码一相册）**：id（uuidv7，即 workspace_id）、
  `code_hash`（argon2，明文不落库）、`album`（绑定的相册 slug）、`enabled`、
  created_at。发码走 `jobs invite-create --album`，吊销置 enabled=false。
- `edit_project` — 一次剪辑任务：`workspace_id`（FK → edit_invite.id，跨设备
  恢复的锚点，带索引）、`album`（冗余自 edit_invite，创建时固化，之后改绑码
  也不影响存量项目）、剧本原文、默认滤镜、`current_round`、`state_version`
  （乐观并发控制）、状态（parsing → matching → reviewing ⇄ refining →
  rendering → done/failed；reviewing 点「重新生成」进 refining，完成回
  reviewing，轮次 +1；绑定相册未入库时先经 ingesting 态）。
  **project 不是用户填的字段**：用户在聊天窗点「新建剪辑」提交剧本的那一刻
  隐式创建（相册来自码的绑定，无需也无法选择）；一个 workspace 下可有任意多个
  project（= 会话列表里的多个对话），标题自动取剧本首行，id 为 uuidv7。
  断点恢复、渲染输出目录都是 project 级：输出路径中的 `<project>` =
  「id 短前缀 + 标题 slug」拼成的目录名。
- `project_event` — **追加式事件时间线**：(project_id, seq) 唯一、actor、kind、
  payload JSONB（小快照 + id 引用，不内嵌大对象）。聊天窗按序渲染；只追加不改写。
- `edit_round` — **反馈闭环的留痕**：project FK、round_no、用户补充输入
  （项目级 + 逐镜头反馈 JSONB）、当轮润色稿/shot list 快照、created_at。
  第 N+1 轮的 LLM 上下文从这张表按轮次全量组装。
- `shot` — 镜头一行：序号、原文片段、润色描述、检索 query（多条）、media 类型偏好、
  目标时长范围、`filter_preset_id`（FK，可空=用项目默认）、人物参考约束、
  `locked`（满意并选定后置真，后续轮次跳过）、`feedback`（本轮不满意的反馈原文）、
  `round_no`（描述最后一次生成于第几轮）。
- `shot_candidate` — 镜头×scene 候选：similarity、quality_score、final_score、rank、
  `round_no`、status(pending/approved/rejected)、用户微调后的 in/out 点。
  rejected 的 scene 在该镜头后续轮次的检索外层过滤中排除（负反馈）。
- `render_output` — 渲染产物：文件路径、实际 in/out 点、输出档位、
  滤镜 slug+checksum、ffmpeg 命令摘要。

## 流水线

```
⓪ 滤镜导入（离线，jobs filters-import，随时可重跑）
   /photo-gallery/luts/*.cube → 校验 → 生成预览缩略图 → 按 slug upsert 进 filter_preset

① 建库（按需触发 + 可手动预热，jobs worker / jobs media-ingest --album <slug>）
   相册首次被某个编辑任务引用 → 下载官方原片到 /photo-gallery/media/<album>/
   （解析复用 static_gallery，source_url 幂等）
   → ffprobe 元数据 → PySceneDetect 拆镜头 → 每镜头抽 1~3 关键帧
   → 批量调 embedding 服务（CLIP + 人脸）→ 画质指标计算 → 带 album 落库
   照片走同一管线（无拆条步骤）；已入库相册直接跳过整个 ①

② 剧本润色 + 解析（api，在线）
   POST /api/projects {剧本文本}（require_role("edit")）
   → LLM 润色：保留结构与意图，补全每镜头的具象视觉要素
     （主体/动作/场景/景别/情绪/时长），输出结构化 shot list
   → 每镜头生成 2~3 条具象检索 query + 从滤镜库现有预设中推荐默认值

③ 融合检索（api，在线）
   每条 query → CLIP 文本向量 → 内层 KNN LIMIT 200 → 外层硬门槛 + 融合重排
   （见「融合排序」）→ RRF 合并多 query → Top-5 写 shot_candidate

④ 人工评审（web，必经步骤，可多轮）
   逐镜头标记满意/不满意；满意 → 选定候选并锁定 + 选滤镜（单帧实时预览）；
   不满意 → 填补充反馈。存在不满意镜头时「重新生成」→ ④a；全部锁定 → rendering。

④a 反馈闭环（api，在线，仅未锁定镜头）
   组装历史上下文（原剧本 + 各轮润色稿 + 各镜头反馈 + 累积的项目级意见，
   读 edit_round）→ LLM 重写未锁定镜头的描述与 query
   → 重跑 ③（外层过滤额外排除该镜头已 rejected 的 scene）→ 回到 ④，轮次 +1

⑤ 渲染导出（jobs render）
   ffmpeg 帧精确剪裁（评审确认的 in/out ± 1s 余量）
   + 烧入选定滤镜（lut3d + eq，从 filter_preset 取字节）
   → 按档位输出（默认 CRF16 / 可选 ProRes / 无滤镜镜头可无损直切）
   → /photo-gallery/<workspace_id>/output/<project>/<序号>_<slug>/ 目录
   + manifest.csv（镜头↔文件↔源素材时码↔滤镜版本）+ 可选 FCPXML/EDL
   → 打包 zip 供下载（api 只读挂 output 分发）
```

②～⑤ 的每个有语义的节点（提交、润色完成、候选就绪、锁定、反馈、重生成、
渲染开始/完成）都同步追加一条 `project_event` —— 聊天窗时间线与断点恢复
（见「工作区与断点恢复」）就建立在这条事件流上。

## 资源与性能估算

- 建库瓶颈在视频解码 + 拆条 + 稳定性估计，CPU 约 0.3~1× 实时
  （即 10 小时素材数小时级，一次性）。生产机有 GPU 时 CLIP 推理可忽略不计。
- 在线部分：剧本润色/解析秒级～十秒级（LLM），检索毫秒级 KNN + 重排，
  滤镜单帧预览毫秒级。
- 磁盘：关键帧缩略图与 LUT 进库（TOAST），输出片段按素材量的 10~30% 预留 volume；
  选 ProRes 档时输出预留需另计（约源片段的 3~6 倍）。

## 风险与对策

| 风险 | 对策 |
| --- | --- |
| 剧本语言抽象，CLIP 检索不中 | LLM 润色成具象描述 + 多 query RRF 融合；评审页可就地改描述重检单个镜头；照 `make eval` 模式建小标注集量化命中率 |
| 画质分喧宾夺主（高清但不相关上位） | 相似度硬门槛在先，画质融合权重设上限（≤0.35），权重 P0 标定 |
| 稳定性指标误伤有意运镜 | 只惩罚高频抖动（手持抖）不惩罚低频运动（pan/推拉）；评审页兜底 |
| 阈值/权重无真实数据标定 | P1 验收含 20 条查询人工目测；P0 用真实素材定权重初值 |
| 导入的 LUT 格式五花八门 | filters-import 严格校验（.cube 3D LUT，尺寸 17/33/65），不合格明确报错跳过并计数，不静默入库 |
| 滤镜更新后旧渲染无法解释 | render_output 记滤镜 slug+checksum，可追溯可复现 |
| embedding 容器内存/显存上涨 | 双模型共存实测后调 compose 限额；必要时 CLIP 拆独立容器（接口不变） |
| 双角色越权（查找码调剪辑接口或反之） | `require_role` 在依赖层强制，接口级测试覆盖两个方向的 403 |
| 多轮重生成后描述漂移、越改越偏 | 每轮 LLM 输入始终以原始剧本为锚 + 全部历史累积；锁定镜头禁止改写保证已确认部分稳定 |
| 反复不满意 = 素材库里根本没有该镜头 | rejected 负反馈避免复读；UI 第三轮起提示"可能无此素材"，建议改剧本或补素材，而不是无限烧 LLM 调用 |
| 双设备并发写同一项目导致状态错乱 | 写接口带 state_version 乐观并发控制，过期 409 前端重拉；事件流只追加，天然无写冲突 |
| 事件表膨胀 | payload 只存小快照+id 引用，大对象（缩略图等）走既有接口按 id 取；无语义的 UI 动作不记录 |
| 换设备恢复被冒用（码即身份） | 与现状同一信任模型（持码即可用）；剪辑码保持高熵、登录照旧限流防枚举；edit_invite 表支持按码即时吊销（enabled=false） |
| 私有模型能力不齐（JSON 输出不稳、上下文窗口小、中文分镜质量差） | 固定状态机限制 LLM 影响面；schema 校验+错误回喂重试；上下文按轮次组装可截断早期轮次只留摘要；P0 用真实剧本对私有模型做一次能力验收，不达标换模型只改 env |
| 提示词改动导致行为漂移无法归因 | edit_round 记录 model 名 + 提示词模板 content hash，逐轮可追溯 |
| 跨工作区数据泄漏（A 码看到 B 码的项目/成片） | 项目/轮次/事件/输出按 workspace_id 隔离，接口从 JWT 取 workspace_id 而非请求参数；接口级测试覆盖跨码访问 404/403。素材池按设计全局共享（与查找类同一批公开相册，信任模型一致） |
| 按相册过滤导致 KNN 召回不足 | pgvector ≥ 0.8 开 hnsw.iterative_scan=relaxed_order；相册数大了再评估局部索引 |
| 按需建库耗时长（视频多的相册下载+拆条可达数十分钟） | 时间线进度卡片如实呈现 + 项目挂 ingesting 态可随时离开（断点恢复兜底）；提供 `jobs media-ingest --album` 手动预热；同一相册并发引用用 job_run 去重防止重复下载 |

## 分期与验收

| 阶段 | 内容 | 验收标准 | 估时 |
| --- | --- | --- | --- |
| P0 需求校准 | 真实剧本+素材子集走查；敲定 shot list JSON schema、内置 LUT 清单、融合权重初值、输出规格与档位、交付格式（问清后期软件：PR/FCP/剪映 → 决定 CSV/EDL/FCPXML） | 双方确认样例输入输出 | 2~3 天 |
| P1 素材建库 | `005` 迁移（含 filter_preset 表、album 列）、embedding 加 CLIP 端点、`jobs media-ingest --album`（下载官方原片到 media/<album>/ + 拆条 + 画质指标）、`jobs worker` 常驻模式 + job_run 去重 | 手动预热一个真实相册全量入库（含视频拆条）；抽 20 条中文查询目测 Top-5 合理；画质列分布抽查；同相册重复触发幂等 | 1.5 周 |
| P2 剧本→候选 + 分角色 + 反馈闭环与会话后端 | agent 框架（client/runner/prompts/skills）、LLM 润色/解析、project/shot 接口、融合检索、`edit_invite` 表登录 + `jobs invite-create` 发码 + `require_role`、`edit_round` 留痕与多轮上下文组装、锁定/负反馈语义、`project_event` 事件流 + 增量拉取接口、state_version 并发控制 | 样本剧本每镜头出 Top-5，人工评命中率达标；两方向越权返回 403；模拟一轮"不满意+反馈"后未锁定镜头候选换血、锁定镜头零变化；删 cookie 重登录后项目列表/时间线/状态完整恢复 | 2 周 |
| P3 剪裁渲染 + 滤镜库 | `jobs filters-import`、`jobs render`（三档输出、1s 余量、LUT 烧入）、manifest、打包下载 | 自备 .cube 一条命令入库；输出可直接导入后期软件；manifest 时码与源片对得上 | 1 周 |
| P4 剪辑类 UI（聊天窗形态） | 会话列表页 + 聊天时间线（消息流与结构化卡片混排：剧本、润色稿、进度、评审卡片内嵌满意/不满意与候选锁定、反馈输入、滤镜下拉+单帧预览、in/out 微调、渲染/下载卡片）+ after_seq 增量轮询 + 移动端适配 | 剪辑码登录后全流程在聊天窗内走通一个项目，含至少一轮"不满意→反馈→重生成→满意"循环；中途关页面换浏览器重登录，历史完整、可继续到出片；查找码 UI 不受影响 | 2.5 周 |
| P5 可选 | CLIP-aesthetic 画质头（启发式不够用时） | 按需 | — |

## 待确认问题（P0 要回答的）

1. 素材规模：常用相册大概几个、每个多少小时视频/多少张照片？（布局已定：
   专用盘挂 `/photo-gallery`，素材全局共享 `media/<album>/`，输出按码隔离
   `output/<workspace_id>/`；盘容量按"会用到的相册原片总量 × 1.3"起步估）
2. 剧本形态样例：给 1 份真实剧本用于校准润色 prompt 与分镜粒度。
3. 后期用什么软件 → 决定交付格式（纯文件夹+CSV 最通用；FCPXML/EDL 可导时间线）。
4. 是否需要「只要含某某的镜头」这类人物过滤（决定 scene_face 是否进 P1）。
5. 内置默认 LUT 清单（自备模版随时可导入，内置集只是兜底）。
6. 输出档位默认值：H.264 CRF16 够不够，还是后期希望直接拿 ProRes 中间码？
   （影响磁盘预算 3~6 倍）
7. 私有 LLM 的规格：base_url 协议确认是 OpenAI `/chat/completions` 吗？
   上下文窗口多大（决定多轮上下文是否需要摘要压缩）？支持
   `response_format: json_object` 吗（不支持就走 prompt 约束+校验重试的兜底）？
