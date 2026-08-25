# 数据源接入：photos.zrc.sg

## 已确认

- **公开、无需鉴权**的静态跑团照片墙。
- 照片和视频按预先归纳好的 album 分组。
- 相册地址：`https://photos.zrc.sg/album/<slug>`，例如
  [`/album/2026-08-10`](https://photos.zrc.sg/album/2026-08-10)。
- **slug 直接就是数据库里的 `album` 字段**（`VARCHAR(200)`，只在 `photo` 表上）。
  不需要额外的 album 元数据表 —— slug 本身即业务标识。
- 幂等键是 `photo_url`（原图完整地址）。URL 在静态站点上稳定且天然唯一。

因为源站公开，两件事被简化了：

1. **不需要签名链接。** 签名保护的是源站访问控制，而源站没有访问控制可绕过。
   `/api/photos/{id}/original` 直接 302 到 `photo_url`。保留这一跳只是为了不把源站
   URL 结构写进前端。
2. **没有 private 相册要过滤。** 原先的 `album.visibility` 概念整个去掉了。

但**本站仍然需要邀请码**：人脸检索创造了一个源站本身没有的能力 —— 拿一张某人的照片
就能把他在所有活动里的照片一次性聚齐。见 [`privacy.md`](privacy.md)。

## 相册页结构（2026-08-14 已确认，解析已收敛）

相册页是服务端渲染的 HTML，每个媒体项是一个带 `data-lightbox` 属性的 `<div>`
（站点自己的 ZephyrLightbox 组件），全部信息都在 data-* 属性里。真实样例（视频）：

```html
<div class="group relative aspect-[4/5] ..."
     onclick="ZephyrLightbox.show('\/album\/2026-08-12\/20260812215627863.mp4', true, 'Stone', ...)"
     data-thumb="/album/thumb/2026-08-12/20260812215627863.mp4"
     data-id="20260812215627863" data-lightbox
     data-src="/album/2026-08-12/20260812215627863.mp4"
     data-is-video="true" data-uploader="Stone"
     data-original-src="/album/2026-08-12/20260812215627863.mp4">
```

映射（实现在 `jobs/sources/static_gallery.py`，用 bs4 选 `[data-lightbox]`）：

| SourceAsset 字段 | 来源 |
| --- | --- |
| `photo_url` | `data-original-src`（缺失时退回 `data-src`），urljoin 成绝对地址 |
| `thumbnail_url` | `data-thumb` —— 源站为每个条目都提供缩略图（`/album/thumb/<slug>/<id>.<ext>`） |
| `kind` | `data-is-video="true"` → video；属性缺失时按扩展名兜底 |
| `filename` | URL path 的 basename（`<data-id>.<ext>`） |

视频照常产出（`kind="video"`）：pipeline 对视频**只登记不提取**（`reason="video"`），
这就是「只用非视频文件建库」的实现点。

页面上找不到任何 `data-lightbox` 节点时返回空并打 `album_page_no_lightbox_items`
的 error 日志 —— 视为源站结构变更，宁可 0 计数报警也不猜。回归测试
`jobs/tests/test_static_gallery.py` 用的就是上面那段原样 HTML。

### 仍未确认的点

1. **相册索引页**：有没有一个页面列出全部相册？`list_albums()` 会尝试
   `/album/`、`/albums`、`/`，都失败则返回空列表，此时必须用 `--album` 显式指定。
   （影响不带 `--album` 的全量 ingest 和每日定时增量。）
2. **规模**：总照片数量级？这决定 `SEARCH_CANDIDATES`（默认 500，是召回上限）是否够用
   —— 单个成员在库里的照片数超过它，结果就会被截断。
3. **变更检测**：当前策略是「这个 photo_url 成功入库过就跳过」。
   对追加式的照片墙够用；同一 URL 内容被替换的情况检测不到，需要时用 `--full`。
   如果源站响应带 `ETag`/`Last-Modified`，可以后续升级成 checksum 比对。

## 抽象：`SourceAdapter`

`pipeline.py` 只依赖这个协议，不知道源站页面长什么样。

```python
class SourceAdapter(Protocol):
    async def list_albums(self) -> list[str]: ...
    def list_assets(self, album: str) -> AsyncIterator[SourceAsset]: ...
    def open_asset(self, asset: SourceAsset) -> AsyncIterator[bytes]: ...
    async def fetch_thumbnail(self, asset: SourceAsset) -> bytes | None: ...
```

```python
@dataclass(frozen=True)
class SourceAsset:
    album: str                      # slug，即 DB 的 album 字段
    filename: str
    photo_url: str                  # 原图完整地址，幂等键
    kind: Literal["image", "video"] = "image"
    thumbnail_url: str | None = None  # 源站提供的缩略图，没有则本地生成
    size_bytes: int | None = None      # 用于批次的字节预算
```

| 实现 | 状态 |
| --- | --- |
| `static_gallery.py` | **可用，解析已按真实页面结构收敛**（`[data-lightbox]` 契约） |
| `local_dir.py` | 可用。扫描本地目录（一级子目录名 = album slug）。两个用途：开发/评估集（`SOURCE_ADAPTER=local_dir`），以及本地相册（auto 模式，根目录 = `{MEDIA_ROOT}/media`） |
| `composite.py` | **可用，`SOURCE_ADAPTER=auto` 的默认实现**。相册粒度路由本地/远端，取字节按 URL scheme 分发 |

## 本地相册（plans/0010）

不是所有素材都发布在 photos.zrc.sg —— 相册可以直接来自宿主机的本地目录。

**运维动作只有一个**：把照片/视频放进 `${MEDIA_ROOT}/media/<slug>/`（与剪辑域
原片目录合一），然后 `make ingest ALBUM=<slug>`。邀请码照旧
`jobs invite create --album <slug>`，持码人不感知素材在哪 —— 检索、浏览、
原图、剪辑与远端相册体验一致（本地原图由 api 从只读挂载分发，而不是 302）。

**来源判定规则**（`jobs/sources/resolve_album_source`，按优先级）：

1. 库里已有该相册的记录 → **记录的 scheme 说了算**（`photo.photo_url` 与
   `media_asset.source_url` 都算证据）。远端相册跑过剪辑建库后
   `media/<album>/` 里会有下载的视频副本 —— 只看目录会把它误判成本地相册，
   这条规则挡住这个陷阱。
2. 首次入库：`media/<album>/` 目录存在 → 本地，否则远端。
3. `photo` 表同时出现两种 scheme（数据已分叉）→ 按远端处理并打 warning，
   绝不静默改道。

**边界与注意**：

- 目录里的文件会被人脸建库（含子目录，`rglob`）；剪辑建库只扫一级文件，
  本地相册的剪辑素材建议平铺。
- 往**远端相册**的 media 目录手动拷文件：会被剪辑建库收编（既有行为，
  合成键 `local://<album>/<file>`），但**不会**进人脸检索库（规则 1 判它远端）。
- 本地相册的 photo_url 形态是 `local://album/<slug>/<相对路径>`，
  URL↔路径换算的单一定义点在 `gallery_core/local_source.py`。
- HEIC/HEIF 能入库检索，但浏览器打开原图表现为下载（已知局限）。

## 抓取纪律

无论最终解析怎么写都要遵守：

- 全局并发上限（`SOURCE_CONCURRENCY`）+ 请求间隔（`SOURCE_RATE_LIMIT_PER_SECOND`），
  默认保守。别把自己家的图库打挂。
- 带可识别的 `User-Agent`，方便在源站日志里区分本项目的流量。
- 指数退避重试，5xx/超时重试，4xx 不重试。
- **原图不落盘**：批量推理需要把字节 POST 给 embedding 服务，所以字节留在内存里，
  一批处理完即释放。不做原图缓存 —— 磁盘会被打满。
- 单张失败只记录不中断整批（`photo.processing_error`，下次运行自动重试）。
