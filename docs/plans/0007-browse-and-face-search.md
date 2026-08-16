# 0007 — 相册浏览 + 按已入库人脸搜索

## 目标

在「上传自拍检索」之外增加第二条检索路径：

1. 登录后可以**分页浏览**（10 张/页）权限范围内的照片缩略图。
2. 点开一张照片，看到这张照片上**已检测到的人脸**的小图列表。
3. 点某张脸 → 确认 → 用这张脸**已存的 embedding** 在权限范围内检索所有相关照片。
4. 按脸检索有**独立**的设备维度限流（默认 4 次/小时），与自拍检索的 3 次/小时互不共享。

## 非范围

- 不做人脸聚类、不合并「同一个人」的多张脸（约束 #8 不变）。
- 不给人脸命名、不存任何选择记录 —— 点脸检索与上传自拍检索一样用完即弃。
- 浏览不提供下载打包；原图仍然只是 302 到公开源站。

## 设计决策

### 人脸小图：入库时裁、存 face.thumb（003 迁移）

embedding 服务返回的 bbox 是 **exif_transpose 之后的原图像素坐标**（见
`embedding/app/model.py`）。入库批次里原图字节还在内存中（`loaded.clear()` 之前），
在那里按 bbox 外扩 35% 裁出 160px WebP 存进 `face.thumb`：

- 从原图裁，清晰；从 256px 缩略图裁小脸会糊成马赛克。
- 裁剪必须同样先 exif_transpose，否则竖拍照片的框会错位。
- 这是**展示用途**的裁剪，不是识别预处理 —— 不喂给任何模型，不违反约束 #3。
- 存量数据用 `python -m jobs face-thumbs` 回填：bbox 已在库里，
  只需重新下载原图裁剪，**不经过 embedding 服务、不动 GPU**。

容量：15 万张脸 × ~3KB ≈ 450MB（TOAST），单机 Postgres 无压力。

### 按脸检索：只传 face_id，embedding 不出库

`POST /search/by-face {face_id}`。服务端取出该脸的 embedding 直接进
`search_by_embedding` —— 向量从不进响应体，客户端也没有任何途径提交裸向量
（否则等于把检索接口变成任意向量探针）。

scope 校验：face → photo → album 必须落在 session 的 `alb` 范围内，越权 404
（与图片分发一致：不向持码人确认「这个 id 存在」）。

### 设备限流搬进 JWT（顺带修一个真实的洞）

原设计里设备 id 只在 cookie：脚本**不带 cookie** 每次请求都会拿到新设备 id，
设备维度限流对脚本完全无效。本次把设备 id 在登录时写进 JWT 的 `dev` claim，
检索时限流键取自 JWT 而不是 cookie：

- 清 cookie / 不带 cookie → JWT 里的设备身份不变，限流照常。
- 想换设备身份必须重新登录 → 每次都要过 captcha，成本从 0 抬到人工。
- 旧 token 没有 `dev` claim → 401 重新登录（与 0006 的 `alb` 先例一致）。

两个设备限流器分开计数：自拍检索 `RATE_LIMIT_SEARCHES_PER_DEVICE_PER_HOUR`
（默认 3）、按脸检索 `RATE_LIMIT_FACE_SEARCHES_PER_DEVICE_PER_HOUR`（默认 4）。
session / IP 两层全局限流两种检索共用（它们是滥用总量的硬边界）。

### 浏览接口

- `GET /photos?page=N` — 每页 10 张（`per_page` 上限 50），scope 强制：
  绑定相册的 session 传别的 album → 403，不传 → 强制绑定相册。
  只列 `kind='image'`、未软删除的照片，按 id（uuidv7 时间序）排序。
- `GET /photos/{id}/faces` — 该照片的人脸列表（face_id + 小图 URL），
  按 bbox 面积降序（最大的脸在前）。
- `GET /faces/{face_id}/thumb` — 人脸小图字节，ETag 缓存，scope 越权 404。

三个都要求登录 session（GET 无 CSRF —— CSRF 防的是跨站**写**；读接口靠
session cookie + SameSite=Lax + 无 CORS 头兜底）。

## 验收标准

- [ ] 浏览分页可用；scoped session 只能看绑定相册的照片，传别的 album → 403
- [ ] 点开照片能看到人脸小图；无小图的存量脸在回填后出现
- [ ] 点脸确认后返回该人的相关照片，结果与用这张脸的原图自拍检索一致
- [ ] scoped session 按脸检索永远限制在绑定相册；跨相册 face_id → 404
- [ ] 同设备一小时第 4 次自拍检索 429；第 5 次按脸检索 429；两者互不影响
- [ ] 清 cookie / 不带 cookie 的脚本无法绕过设备限流（限流键在 JWT 里）
- [ ] 旧 session（无 dev claim）一律 401 重新登录
- [ ] embedding 向量不出现在任何响应体里

## 风险

- **face.thumb 回填期间**（迁移已跑、回填未跑）人脸列表里小图 404 —— 前端做占位
  处理；回填是一次性的。
- 源站缩略图路径的照片没有本地重编码，浏览网格直接用现有 /thumb 接口，无新风险。
- uuidv7 的时间戳前缀可被观察者估算，但仍有 74 位随机 —— 不可枚举；
  且所有 by-id 接口都做 scope 校验，猜中 id 也拿不到范围外的数据。
