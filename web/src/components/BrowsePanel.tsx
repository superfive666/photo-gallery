import { useCallback, useEffect, useState } from 'react'

import { ApiError, type PhotoItem, type PhotoPage, listPhotos } from '../api'
import { formatMs } from '../lib/time'
import { FacePicker } from './FacePicker'

/**
 * 浏览模式：分页翻看照片 → 点开一张 → 点上面的脸 → 按这个人检索。
 *
 * 每页 10 张由后端定死。分页用「上一页/下一页」而不是页码条 ——
 * 手机上页码条点不准，而浏览照片本来就是顺序翻的行为。
 */
export function BrowsePanel({
  album,
  onSearchByFace,
  searching,
}: {
  // 当前生效的相册（scoped session 固定为绑定相册；全相册码则跟随下拉选择）
  album: string
  onSearchByFace: (faceId: string) => void
  searching: boolean
}) {
  const [page, setPage] = useState(1)
  const [data, setData] = useState<PhotoPage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [openedPhoto, setOpenedPhoto] = useState<PhotoItem | null>(null)

  // 换相册回到第一页 —— 停在旧页码会看到「空白的第 7 页」
  useEffect(() => setPage(1), [album])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await listPhotos(page, album || undefined))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '照片列表加载失败，请稍后再试')
    } finally {
      setLoading(false)
    }
  }, [page, album])

  useEffect(() => {
    void load()
  }, [load])

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.per_page)) : 1

  return (
    <section className="space-y-4" aria-label="浏览相册">
      {error && (
        <p role="alert" className="text-danger-500 text-sm">
          {error}
        </p>
      )}

      {data && data.items.length === 0 && !loading && (
        <p className="text-ink-400 py-10 text-center text-sm">
          这个相册还没有已入库的照片。照片会在活动后由管理员批量处理。
        </p>
      )}

      <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-5" aria-busy={loading}>
        {(data?.items ?? []).map((photo) => (
          <button
            key={photo.photo_id}
            type="button"
            onClick={() => setOpenedPhoto(photo)}
            className="group bg-ink-900 relative aspect-square overflow-hidden rounded-lg"
          >
            {photo.thumb_url ? (
              <img
                src={photo.thumb_url}
                alt={`${photo.album} 的照片`}
                loading="lazy"
                decoding="async"
                className="size-full object-cover transition-transform group-hover:scale-105"
              />
            ) : (
              <span className="text-ink-600 grid size-full place-items-center text-xs">
                无预览
              </span>
            )}
            {photo.kind === 'video' && (
              <span className="bg-ink-950/80 text-ink-200 absolute top-1.5 left-1.5 flex items-center gap-1 rounded-full px-2 py-0.5 text-xs">
                <span aria-hidden="true">▶</span>
                {photo.duration_ms != null ? formatMs(photo.duration_ms) : '视频'}
              </span>
            )}
            {photo.face_count > 0 && (
              <span className="bg-ink-950/80 text-ink-200 absolute right-1.5 bottom-1.5 rounded-full px-2 py-0.5 text-xs">
                {photo.face_count} 张脸
              </span>
            )}
          </button>
        ))}
      </div>

      {data && data.total > 0 && (
        <nav className="flex items-center justify-between gap-3" aria-label="翻页">
          <button
            type="button"
            disabled={page <= 1 || loading}
            onClick={() => setPage(page - 1)}
            className="bg-ink-900 hover:bg-ink-800 disabled:bg-ink-800 disabled:text-ink-600 rounded-xl px-4 py-3 text-sm font-medium transition-colors disabled:cursor-not-allowed"
          >
            上一页
          </button>
          <span className="text-ink-600 text-xs">
            第 {page} / {totalPages} 页 · 共 {data.total} 张
          </span>
          <button
            type="button"
            disabled={page >= totalPages || loading}
            onClick={() => setPage(page + 1)}
            className="bg-ink-900 hover:bg-ink-800 disabled:bg-ink-800 disabled:text-ink-600 rounded-xl px-4 py-3 text-sm font-medium transition-colors disabled:cursor-not-allowed"
          >
            下一页
          </button>
        </nav>
      )}

      {openedPhoto && (
        <FacePicker
          photo={openedPhoto}
          searching={searching}
          onClose={() => setOpenedPhoto(null)}
          onConfirm={(faceId) => {
            setOpenedPhoto(null)
            onSearchByFace(faceId)
          }}
        />
      )}
    </section>
  )
}
