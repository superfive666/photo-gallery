# 数据源接入：photos.zrc.sg

> **状态：待确认。** 源站是自建/静态相册，抓取方式尚未定稿。本文件记录已知信息、
> 抽象设计与待回答的问题。所有源站细节都隔离在 `jobs/sources/` 之后。

## 抽象：`SourceAdapter`

`jobs/pipeline.py` 只依赖这个协议，不知道源站长什么样。换源站或源站改版只需替换 adapter。

```python
class SourceAdapter(Protocol):
    async def list_albums(self) -> list[SourceAlbum]: ...
    async def list_assets(self, album_id: str, since: datetime | None) -> AsyncIterator[SourceAsset]: ...
    async def open_asset(self, asset: SourceAsset) -> AsyncIterator[bytes]: ...   # 流式，不整块读进内存
    def build_original_url(self, asset: SourceAsset, ttl_seconds: int) -> str: ...
```

```python
@dataclass(frozen=True)
class SourceAsset:
    id: str                 # 源站稳定唯一标识 → photo.source_asset_id
    album_id: str
    kind: Literal["image", "video"]
    filename: str
    checksum: str | None    # etag / md5；没有就退化为 f"{size}:{mtime}"
    size_bytes: int | None
    taken_at: datetime | None
    width: int | None
    height: int | None
```

已提供的实现：

| 文件 | 状态 |
| --- | --- |
| `jobs/sources/static_gallery.py` | **占位**，待填充真实抓取逻辑 |
| `jobs/sources/local_dir.py` | 可用。扫描本地目录，用于开发和评估集 |

先用 `local_dir` 把 ② ③ 两条链路和评估流程全部跑通，等源站信息到位后只补 `static_gallery`。
这样源站的未知不会阻塞任何其他工作。

## 待回答的问题

抓取实现需要下面这些答案，请补充：

1. **列表接口**：album 页面是服务端渲染的 HTML，还是有 JSON 索引（如 `index.json`、
   `manifest.json`）？有没有目录列举（autoindex）？
   → 有结构化索引的话工程量小一个数量级，务必优先确认。
2. **URL 规律**：原图、缩略图、album 页的 URL 模板分别是什么？
   给一个真实的 album 链接 + 一张原图链接即可推断。
3. **稳定标识**：什么能作为 `source_asset_id`？文件路径？如果相册会被整理/改名，
   路径就不稳定，需要退化为文件内容 hash。
4. **鉴权**：需要登录吗？Cookie / Basic Auth / 签名 URL / 完全公开？
   有 private album 吗？如果有，必须能在 API 层判断可见性，否则会把私密相册泄露到检索结果里。
5. **元数据**：拍摄时间从哪来 —— 目录名、文件名、EXIF，还是索引里有？
   （用于结果按时间排序和按活动分组）
6. **变更检测**：有 `ETag` / `Last-Modified` 响应头吗？没有的话增量同步只能靠
   「文件大小 + mtime」弱校验，或每次全量比对文件列表。
7. **规模与限速**：总照片数量级？源站能承受多大并发和带宽？
   → 决定 `SOURCE_CONCURRENCY` 与 `SOURCE_RATE_LIMIT` 默认值。别把自己家的图库打挂。
8. **视频**：album 里视频占比多少？第一期不处理，但需要知道量级来决定第二期的优先级。

## 抓取纪律（无论最终实现如何都要遵守）

- 全局并发上限 + 请求间隔，可配置，默认保守。
- 尊重 `robots.txt`（虽然是自己的站，但保持习惯）。
- 带可识别的 `User-Agent`，方便在源站日志里区分本项目的流量。
- 指数退避重试，5xx/超时重试，4xx 不重试。
- 流式下载到临时文件，**处理完立即删除**；不做原图缓存。
- 单张照片失败只记录不中断整批。
