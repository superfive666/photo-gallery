import { useEffect, useState } from 'react'

import { ApiError, type FaceItem, type PhotoItem, listFaces } from '../api'

/**
 * 点开一张照片：大图 + 检测到的人脸列表。点一张脸 → 明确确认 → 按此人检索。
 *
 * 确认这步不能省：点脸即搜会把误触变成一次被扣次数的检索（设备维度一小时只有
 * 4 次），而且「用这张脸找 TA 的所有照片」是个应该被有意识执行的动作。
 */
export function FacePicker({
  photo,
  searching,
  onClose,
  onConfirm,
}: {
  photo: PhotoItem
  searching: boolean
  onClose: () => void
  onConfirm: (faceId: string) => void
}) {
  const [faces, setFaces] = useState<FaceItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listFaces(photo.photo_id)
      .then((items) => {
        if (!cancelled) setFaces(items)
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : '人脸列表加载失败，请稍后再试')
        }
      })
    return () => {
      cancelled = true
    }
  }, [photo.photo_id])

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = previousOverflow
    }
  }, [onClose])

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="选择要检索的人脸"
      className="bg-ink-950/95 fixed inset-0 z-50 flex flex-col"
    >
      <header className="flex items-center justify-between gap-3 px-4 py-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium">{photo.album}</p>
          <p className="text-ink-600 truncate text-xs">点选一张脸，用它检索这个人的照片</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭"
          className="bg-ink-900 hover:bg-ink-800 grid size-9 shrink-0 place-items-center rounded-full transition-colors"
        >
          ×
        </button>
      </header>

      <div className="flex min-h-0 flex-1 items-center justify-center px-4">
        {photo.thumb_url ? (
          <img
            src={photo.thumb_url}
            alt={`${photo.album} 的照片`}
            className="max-h-full max-w-full rounded-xl object-contain"
          />
        ) : (
          <span className="text-ink-600 text-sm">这张照片没有预览图</span>
        )}
      </div>

      <footer className="space-y-3 px-4 py-4">
        {error && (
          <p role="alert" className="text-danger-500 text-sm">
            {error}
          </p>
        )}

        {faces === null && !error && <p className="text-ink-600 text-xs">正在加载人脸…</p>}

        {faces !== null && faces.length === 0 && (
          <p className="text-ink-400 text-sm">这张照片上没有检测到可检索的人脸。</p>
        )}

        {faces !== null && faces.length > 0 && (
          <ul className="flex flex-wrap gap-2" aria-label="照片中的人脸">
            {faces.map((face, i) => (
              <li key={face.face_id}>
                <button
                  type="button"
                  onClick={() => setSelected(selected === face.face_id ? null : face.face_id)}
                  aria-pressed={selected === face.face_id}
                  aria-label={`第 ${i + 1} 张脸`}
                  className={`block overflow-hidden rounded-lg border-2 transition-colors ${
                    selected === face.face_id
                      ? 'border-accent-500'
                      : 'border-ink-800 hover:border-ink-600'
                  }`}
                >
                  {face.thumb_url ? (
                    <img
                      src={face.thumb_url}
                      alt=""
                      loading="lazy"
                      decoding="async"
                      className="size-16 object-cover"
                    />
                  ) : (
                    <span className="text-ink-600 bg-ink-900 grid size-16 place-items-center text-center text-[10px] leading-tight">
                      小图
                      <br />
                      待生成
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}

        {selected && (
          <div className="bg-ink-900/60 border-ink-800 space-y-3 rounded-xl border p-4">
            <p className="text-ink-200 text-sm leading-relaxed">
              用选中的这张脸，检索这个人出现过的所有照片？
              <span className="text-ink-600 block text-xs">
                检索基于已入库的照片数据，同一设备一小时最多 4 次。
              </span>
            </p>
            <button
              type="button"
              disabled={searching}
              onClick={() => onConfirm(selected)}
              className="bg-accent-500 hover:bg-accent-600 disabled:bg-ink-800 disabled:text-ink-600 w-full rounded-xl px-4 py-3 font-medium transition-colors disabled:cursor-not-allowed"
            >
              {searching ? '正在检索…' : '确认检索'}
            </button>
          </div>
        )}
      </footer>
    </div>
  )
}
