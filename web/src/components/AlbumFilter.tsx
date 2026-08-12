import type { Album } from '../api'

/**
 * 相册筛选。
 *
 * 默认「所有活动」—— 多数人不记得自己参加的是哪一场，逼他们先选一个只会增加放弃率。
 * 但选定相册的那条路径在后端会走分区裁剪，又快又精确，所以值得在 UI 上给出来，
 * 尤其适合「我知道就是那次跑步」的场景。
 */
export function AlbumFilter({
  albums,
  value,
  onChange,
  disabled,
}: {
  albums: Album[]
  value: string
  onChange: (album: string) => void
  disabled: boolean
}) {
  // 库里只有一个相册时筛选器没有意义，不占屏幕
  if (albums.length <= 1) return null

  return (
    <div className="space-y-2">
      <label htmlFor="album" className="text-ink-400 block text-[13px]">
        在哪次活动里找
      </label>
      <select
        id="album"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        className="bg-ink-900 border-ink-800 focus:border-accent-500 disabled:text-ink-600 w-full rounded-xl border px-4 py-3 text-base outline-none transition-colors"
      >
        <option value="">所有活动</option>
        {albums.map((album) => (
          <option key={album.album} value={album.album}>
            {album.album}（{album.photo_count} 张）
          </option>
        ))}
      </select>
    </div>
  )
}
