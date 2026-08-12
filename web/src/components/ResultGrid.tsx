import { useMemo } from 'react'

import type { Match } from '../api'

/**
 * 结果网格。按相册分组 —— 用户的心智模型是「哪次活动」，而不是一个连续的相似度队列。
 * 组内仍按相似度降序，最可能是本人的照片排在最前。
 */
export function ResultGrid({
  matches,
  onOpen,
}: {
  matches: Match[]
  onOpen: (index: number) => void
}) {
  const groups = useMemo(() => groupByAlbum(matches), [matches])

  return (
    <div className="space-y-8">
      {groups.map((group) => (
        <section key={group.album} className="space-y-3">
          <header className="flex items-baseline justify-between gap-3">
            <h2 className="truncate text-base font-medium">{group.album}</h2>
            <span className="text-ink-600 shrink-0 text-xs">{group.items.length} 张</span>
          </header>

          <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-4 md:grid-cols-6">
            {group.items.map(({ match, index }) => (
              <button
                key={match.photo_id}
                type="button"
                onClick={() => onOpen(index)}
                className="group bg-ink-900 relative aspect-square overflow-hidden rounded-lg"
              >
                {match.thumb_url ? (
                  <img
                    src={match.thumb_url}
                    alt={`${group.album} 的照片`}
                    // 懒加载 + 固定宽高比，避免长列表一次性发起几百个请求
                    loading="lazy"
                    decoding="async"
                    className="size-full object-cover transition-transform group-hover:scale-105"
                  />
                ) : (
                  <span className="text-ink-600 grid size-full place-items-center text-xs">
                    无预览
                  </span>
                )}
              </button>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}

interface AlbumGroup {
  album: string
  // 保留在完整结果数组中的下标，供 lightbox 跨相册前后翻页
  items: { match: Match; index: number }[]
}

function groupByAlbum(matches: Match[]): AlbumGroup[] {
  const byAlbum = new Map<string, AlbumGroup>()
  matches.forEach((match, index) => {
    let group = byAlbum.get(match.album)
    if (!group) {
      group = { album: match.album, items: [] }
      byAlbum.set(match.album, group)
    }
    group.items.push({ match, index })
  })
  // 相册顺序沿用第一次出现的顺序，即该相册最高分照片的排名
  return [...byAlbum.values()]
}
