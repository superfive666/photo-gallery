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

## 仍未确认

**相册页的具体标记结构。** 这决定 `_parse_album_page` 的最终形态。

`jobs/sources/static_gallery.py` 现在实现的是按优先级依次尝试的通用解析：

1. `/album/<slug>/index.json`、`/album/<slug>/album.json`、`?format=json`
   —— 若站点有结构化索引，优先用它，比解析 HTML 稳定得多。
2. HTML：抓 `<a href>` 里指向图片/视频后缀的链接作为原图，抓同一 `<a>` 内的
   `<img src>` 作为缩略图；没有可用链接时兜底扫全页 `<img src>`。
   命中 `thumb` / `small` / `preview` 等路径特征的链接不会被当成原图。

### 下一步：跑一次 probe

```bash
make probe ALBUM=2026-08-10
```

它会打印解析到了多少相册、多少照片/视频、多少条带源站缩略图，以及前 5 条样例。
**把这个输出（或相册页的 HTML 片段）贴出来**，就能把通用解析收敛成精确的选择器。

如果输出是 `assets_total: 0`，说明通用解析没匹配上这个站点的结构 —— 这是预期内的，
不是 bug。

### 其余几个仍需确认的点

1. **相册索引页**：有没有一个页面列出全部相册？`list_albums()` 会尝试
   `/album/`、`/albums`、`/`，都失败则返回空列表，此时必须用 `--album` 显式指定。
2. **缩略图**：源站是否为每张照片提供缩略图？提供的话直接落其字节（省一次本地重编码）；
   没有则由 `jobs/thumbnails.py` 从原图生成 256px WebP。
   probe 输出里的 `with_source_thumbnail` 会告诉我们答案。
3. **规模**：总照片数量级？这决定 `SEARCH_CANDIDATES`（默认 500，是召回上限）是否够用
   —— 单个成员在库里的照片数超过它，结果就会被截断。
4. **视频占比**：第一期不处理视频，但需要知道量级来决定第二期优先级。
   probe 输出里的 `videos` 会给出单个相册的情况。
5. **变更检测**：当前策略是「这个 photo_url 成功入库过就跳过」。
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
| `static_gallery.py` | 可用，但解析策略是通用的，待 probe 后收敛 |
| `local_dir.py` | 可用。扫描本地目录（一级子目录名 = album slug），用于开发和评估集 |

## 抓取纪律

无论最终解析怎么写都要遵守：

- 全局并发上限（`SOURCE_CONCURRENCY`）+ 请求间隔（`SOURCE_RATE_LIMIT_PER_SECOND`），
  默认保守。别把自己家的图库打挂。
- 带可识别的 `User-Agent`，方便在源站日志里区分本项目的流量。
- 指数退避重试，5xx/超时重试，4xx 不重试。
- **原图不落盘**：批量推理需要把字节 POST 给 embedding 服务，所以字节留在内存里，
  一批处理完即释放。不做原图缓存 —— 磁盘会被打满。
- 单张失败只记录不中断整批（`photo.processing_error`，下次运行自动重试）。
