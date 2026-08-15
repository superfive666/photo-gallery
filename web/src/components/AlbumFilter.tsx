import type { Album } from '../api'

/**
 * 相册筛选。
 *
 * 默认「所有活动」—— 多数人不记得自己参加的是哪一场，逼他们先选一个只会增加放弃率。
 * 但选定相册的那条路径在后端会走分区裁剪，又快又精确，所以值得在 UI 上给出来，
 * 尤其适合「我知道就是那次跑步」的场景。
 *
 * lockedAlbum：绑定单相册的邀请码没有选择可言 —— 展示成静态文本而不是
 * disabled 的下拉。disabled 下拉暗示「以后能选」，静态文本才是诚实的表达。
 */
export function AlbumFilter({
  albums,
  value,
  onChange,
  disabled,
  lockedAlbum = null,
}: {
  albums: Album[]
  value: string
  onChange: (album: string) => void
  disabled: boolean
  lockedAlbum?: string | null
}) {
  if (lockedAlbum) {
    return (
      <div className="space-y-2">
        <span className="text-ink-400 block text-[13px]">检索范围</span>
        <p className="bg-ink-900 border-ink-800 w-full rounded-xl border px-4 py-3 text-base">
          {lockedAlbum}
          <span className="text-ink-600 ml-2 text-xs">本邀请码仅限这次活动</span>
        </p>
      </div>
    )
  }

  // 一个相册都没有（还没建库）时不占屏幕。
  // 只有一个相册也照样显示：筛选对结果虽无影响，但「哪些活动已经能搜」
  // 这个信息本身有价值 —— 建库初期用户会想确认自己参加的那场在不在里面。
  if (albums.length === 0) return null

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
