# 0011 — 评审页视频段预览 + 每镜头备选

## 背景 / 问题

真实使用反馈（2026-08-26）暴露两个评审阶段的痛点：

1. **只看关键帧难以下判断。** 好的画面在多个候选的关键帧里都出现，但关键帧
   是静止的一瞬 —— 到底选哪条，需要看到「匹配到的那几秒」动起来才能确认。
2. **只能选一条太紧。** 评审时经常有两条都不错、当场难分高下的情况。
   用户需要每个镜头能选两条：一条主选、一条备选，后期在剪辑软件里再定夺。

## 目标

- 评审时点开任一候选即可**播放该候选匹配到的秒段**（scene 的 start~end），
  循环播放，看完再决定选不选。
- 锁定镜头时除主选外可**额外指定一条备选**；渲染时备选与主选一同导出，
  manifest 标注 role，后期软件里二选一。

## 非范围

- in/out 点微调 UI（后端字段已支持，仍留待后续）。
- 预览转码/低码率代理流 —— 第一版直接分发原片字节（HTTP Range），
  浏览器按需拉取；源片是 H.264 mp4（建库下载的活动视频），浏览器可直接解码。
- 备选数量参数化（固定 1 条备选，够用为止）。

## 设计

### 视频段预览

新端点 `GET /edit/scenes/{scene_id}/preview`：

- 权限与 `/edit/scenes/{id}/thumb` 完全一致 —— edit 会话 + scene.album 必须
  等于码绑相册。
- 仅 `kind=video` 且有本地落盘文件的素材可预览；照片候选前端直接放大关键帧，
  不走此端点。
- 直接 `FileResponse(asset.path)` 分发**整个原片文件**：Starlette 原生支持
  HTTP Range，浏览器 `<video>` 会 seek 到片段位置按需拉取，服务端不做剪裁
  （api 容器无 ffmpeg，也不该有 —— 渲染纪律不破）。
- 路径防御与 photos.original 同一套：解析后必须落在 `{media_root}/media` 内，
  否则 404（防御纵深，正常数据不会越界）。

前端 ReviewCard 重构交互：

- 点候选缩略图 → 下方展开预览区。视频候选用 `<video>` 播 scene 的
  start~end 秒段（media fragment `#t=start,end` 初始定位 + timeupdate 循环）；
  照片候选放大显示关键帧。
- 预览区里两个动作：「设为主选」「设为备选」。缩略图上用角标标注当前身份。
- 选定主选后出现滤镜选择 + 「锁定这个镜头」。

### 备选

- `shot` 表追加 `backup_candidate_id`（007 迁移，FK → shot_candidate，
  ON DELETE SET NULL），语义与 locked_candidate_id 平行。
- approve 接口追加可选 `backup_candidate_id`；备选与主选同样标 `approved`
  （regenerate 的负反馈只否决 pending，备选不会被误伤）。
- 渲染：备选片段与主选一同导出，文件名 `NN_<slug>_alt.mp4`（紧跟主选排序），
  manifest 追加 `role` 列（primary / backup）。
- 撤销锁定（写反馈）时主选、备选一并清空，之前 approved 的候选状态复位为
  pending —— 否则该候选躲过 regenerate 的 rejected 标记，下一轮还会复读。

## 验收标准

- 评审页点视频候选能看到匹配秒段循环播放；点照片候选能看到大图。
- 锁定时可选 0 或 1 条备选；锁定卡片显示两条；渲染 zip 里备选文件与
  manifest role 列齐全。
- 越权（他相册 scene）、越界路径、无本地文件的预览请求一律 404。
- `make test` / `make lint` 全绿。

## 风险

- **原片体积大**：预览按 Range 拉流不落盘，但用户快速点很多候选会产生
  较多读放大。可接受 —— 素材盘是本地盘，评审是单人低频操作。
- **moov atom 在文件尾**的 mp4 首帧慢：建库下载的源片多为 faststart，
  且浏览器会用 Range 拿尾部，最多多一次往返，不做预处理。
- 浏览器不支持源片编码（罕见，如 HEVC in Chrome/Linux）：预览黑屏但选择
  流程不受阻，关键帧仍在。
