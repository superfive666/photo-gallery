# 剪辑域：剧本驱动的选片+剪裁（media-clip）

设计与决策记录见 [`plans/0009-script-driven-clip-service.md`](plans/0009-script-driven-clip-service.md)（设计第 9 版，落地时对齐 main 的 invite_code：见文档头部适配说明）。
本文是**运维视角**：怎么部署、怎么发码、怎么排查。

## 一句话流程

剪辑码登录（一码一相册）→ 聊天窗贴剧本 → LLM 润色分镜 → CLIP 融合检索出候选
→ 逐镜头评审（点开候选可播放匹配秒段；满意锁定主选、可再带一条备选 /
不满意反馈重生成，可多轮）→ ffmpeg 渲染（备选随主选一同导出，manifest 标 role）
→ 下载 zip。

全部状态在服务端（事件时间线 + 状态表）：关页面、换设备、cookie 过期都不丢进度。

## 部署清单

1. **专用盘**挂到宿主机，路径写进 `.env` 的 `MEDIA_ROOT_HOST`（容器内固定 `/photo-gallery`）：

   ```
   /photo-gallery/
   ├── media/<album>/     **只放视频原片**（照片不落盘：分析在内存完成、渲染时现下载）
   ├── output/<workspace>/ 渲染产物，按码隔离，render 自动创建
   ├── luts/              自备 .cube 滤镜模版放这里
   └── agent-prompts/     可选：提示词热覆盖（同名文件优先于仓库内置）
   ```

2. **CLIP 模型**（唯一新增的常驻推理模型）：宿主机目录放三个文件，
   挂给 embedding 容器（`CLIP_MODEL_DIR_HOST`）：

   - `image.onnx` — Chinese-CLIP ViT-B/16 图像塔（输入 N×3×224×224，输出 512 维）
   - `text.onnx` — 文本塔（输入 token ids [+ attention mask]，输出 512 维）
   - `vocab.txt` — BERT 词表

   导出（在任意有 GPU/大内存的机器上一次性执行，产物拷过来即可）：

   ```bash
   pip install cn_clip torch onnx
   python -c "
   import torch, cn_clip.clip as clip
   from cn_clip.clip import load_from_name
   model, _ = load_from_name('ViT-B-16', device='cpu', download_root='.')
   model.eval()
   img = torch.randn(1, 3, 224, 224)
   txt = clip.tokenize(['测试'])
   torch.onnx.export(model.visual, img, 'image.onnx', input_names=['image'],
                     dynamic_axes={'image': {0: 'batch'}}, opset_version=17)
   torch.onnx.export(model.bert, (txt,), 'text.onnx', input_names=['text'],
                     dynamic_axes={'text': {0: 'batch'}}, opset_version=17)
   "
   # vocab.txt 在 cn_clip 包内或 HuggingFace OFA-Sys/chinese-clip-vit-base-patch16
   ```

   文件缺失时：embedding 服务照常启动（`/healthz` 报 `clip_loaded: false`），
   人脸检索完全不受影响；剪辑建库会明确报错而不是静默跳过。
   模型换版本时改 `CLIP_MODEL_VERSION` —— scene 表按 model_name/version 溯源，
   与 face 表同一套换模型纪律。

3. **LLM**（可选但强烈建议）：`.env` 里配 OpenAI-compatible 三元组
   `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`。不配走回退路径
   （按编号/空行切分剧本，原文即检索词），流程完整但润色效果打折。

4. `make migrate && make up`（up 现在包含 `worker` 常驻服务）。

5. `make filters-import` —— 内置 5 个预设（原色/暖调/冷调/黑白/胶片）+
   `luts/` 下的自备模版一起入库。之后随时可重跑（幂等）。

## 发码 / 吊销

```bash
# 发一张剪辑码（prefix.secret 形态，完整码只显示一次；输出里带 workspace_id）
docker compose run --rm jobs python -m jobs invite create --role edit \
    --album 2026-08-10 --label "发给谁"
# 吊销（与查找码同一套机制）
docker compose run --rm jobs python -m jobs invite disable --prefix <8位hex>
# 下架滤镜
docker compose run --rm jobs python -m jobs filters-import --disable <slug>
```

一码一相册：拿到码 = 拿到用这个相册剪辑的权限，用户没有选相册的入口。
多个码可绑同一相册（素材共享，项目/成片互相不可见）。查找码照旧
`invite create --album ...`（不带 --role）。

## 照片不落盘（006 迁移起）

建库只把**视频**下载到本地（拆条与 ffmpeg 剪裁必须随机访问文件）；照片的关键帧、
画质指标、CLIP 向量都在下载字节的生命周期内算完即弃，照片候选的评审预览用库里的
关键帧（视频候选的秒段预览按 HTTP Range 直接读本地原片，不产生副本），
渲染导出那一刻才按 source_url 从源站现下载单张原图 —— 本地盘的占用 ≈ 视频总量。

006 之前已经下载到 media/<album>/ 里的照片文件可以直接删掉腾空间
（渲染会自动回退到远程下载），例如：

```bash
find /photo-gallery/media/<album> -maxdepth 1 -type f \
  \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) -delete
```

手动拷入的本地照片（local:// 来源）仍然支持，但别删它们的文件 —— 它们没有远程回退。

## 预热大相册

视频多的相册第一次建库要下载 + 拆条，可能几十分钟。重要活动提前跑：

```bash
make media-ingest ALBUM=2026-08-10
```

不预热也能用 —— 用户首次引用时 worker 自动建库，时间线上有进度提示，
用户可关页面稍后回来（断点恢复）。

## 排查

- **渲染报「照片原图缺失」**：local:// 来源的本地照片文件被移动/删除了 —— 放回原位或重新建库。
- **任务卡住/失败**：`job_run` 表是第一现场（queued/running/failed + error + stats）。
  worker 被杀掉时任务停在 running —— 重启 worker 后手动把该行改回 queued 即可重跑。
- **候选全不对**：先确认 `scene` 表里该相册有数据、`edit_sim_floor` 没设太高；
  再看 `edit_round.llm_model`（fallback = 没走 LLM）。
- **换 LLM/改提示词后效果变化**：`edit_round.prompt_fingerprint` + `llm_model`
  可以按轮次归因。
- **渲染产物对不上**：`render_output` 记录了精确/含余量两组时码、滤镜 slug+checksum
  与 ffmpeg 参数。

## 与隐私约束的关系

- 剪辑域的素材是**公开相册的原片**，与自拍无关；约束 1（自拍不持久化）不变。
- `project_event` / 日志照旧只有 id、计数、文案 —— 不出现向量与图片字节（约束 2）。
- 「含某人的镜头」过滤（scene_face）**第一版未实现**，实现时参考照依旧只活在请求内存里。

## 已知局限（第一版）

- 评审页暂无 in/out 点微调 UI（后端字段已支持，渲染用 scene 边界 ±1s 余量）。
  秒段预览与每镜头备选已支持（plans/0011）。
- 交付格式为 文件夹 + manifest.csv + zip；FCPXML/EDL 待后期软件确认后加。
- 阈值/融合权重是经验值，未用真实素材标定（见 plans/0009 的 P0 清单）。
- 无 scene_face（人物过滤）、无 CLIP-aesthetic 画质头。
