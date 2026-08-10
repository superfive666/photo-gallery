import { useEffect } from 'react'

import type { Match } from '../api'

/**
 * 大图查看。缩略图只有 256px，所以这里不放大缩略图 ——
 * 而是提供「在相册中打开原图」的入口（走 api 的短效签名跳转）。
 */
export function Lightbox({
  matches,
  index,
  onClose,
  onNavigate,
}: {
  matches: Match[]
  index: number
  onClose: () => void
  onNavigate: (nextIndex: number) => void
}) {
  const match = matches[index]

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
      if (event.key === 'ArrowLeft' && index > 0) onNavigate(index - 1)
      if (event.key === 'ArrowRight' && index < matches.length - 1) onNavigate(index + 1)
    }
    window.addEventListener('keydown', onKey)
    // 打开时锁住背景滚动，关闭时恢复
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = previousOverflow
    }
  }, [index, matches.length, onClose, onNavigate])

  if (!match) return null

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="照片详情"
      className="bg-ink-950/95 fixed inset-0 z-50 flex flex-col"
    >
      <header className="flex items-center justify-between gap-3 px-4 py-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium">{match.album_name}</p>
          <p className="text-ink-600 truncate text-xs">
            {match.filename}
            {match.taken_at ? ` · ${formatDate(match.taken_at)}` : ''}
          </p>
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
        {match.thumb_url && (
          <img
            src={match.thumb_url}
            alt={match.filename}
            className="max-h-full max-w-full rounded-xl object-contain"
          />
        )}
      </div>

      <footer className="flex items-center justify-between gap-3 px-4 py-4">
        <button
          type="button"
          disabled={index === 0}
          onClick={() => onNavigate(index - 1)}
          className="bg-ink-900 disabled:text-ink-600 rounded-lg px-4 py-2 text-sm disabled:cursor-not-allowed"
        >
          上一张
        </button>

        <a
          href={match.original_url}
          target="_blank"
          rel="noreferrer noopener"
          className="bg-accent-500 hover:bg-accent-600 rounded-lg px-4 py-2 text-sm font-medium transition-colors"
        >
          查看原图
        </a>

        <button
          type="button"
          disabled={index >= matches.length - 1}
          onClick={() => onNavigate(index + 1)}
          className="bg-ink-900 disabled:text-ink-600 rounded-lg px-4 py-2 text-sm disabled:cursor-not-allowed"
        >
          下一张
        </button>
      </footer>
    </div>
  )
}

function formatDate(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleDateString('zh-Hans-SG', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  })
}
