import { useEffect } from 'react'

import type { Match } from '../api'
import { formatMs, seekUrl } from '../lib/time'

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
          <p className="truncate text-sm font-medium">{match.album}</p>
          <p className="text-ink-600 truncate text-xs">
            第 {index + 1} / {matches.length} 张
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
            alt={`${match.album} 的${match.kind === 'video' ? '视频' : '照片'}`}
            className="max-h-full max-w-full rounded-xl object-contain"
          />
        )}
      </div>

      {/* 视频命中：列出这个人出现的时间段，点哪段就从哪段开始播（#t= 原生跳转） */}
      {match.kind === 'video' && match.segments.length > 0 && (
        <section aria-label="出现时段" className="space-y-2 px-4 pb-1">
          <p className="text-ink-600 text-xs">
            在视频中出现 {match.segments.length} 段，点击从该时间点开始播放
          </p>
          <ul className="flex flex-wrap gap-2">
            {match.segments.map((seg) => (
              <li key={seg.start_ms}>
                <a
                  href={seekUrl(match.original_url, seg.start_ms)}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="bg-ink-900 hover:bg-ink-800 text-ink-200 inline-block rounded-full px-4 py-3 font-mono text-xs transition-colors"
                >
                  {formatMs(seg.start_ms)}–{formatMs(seg.end_ms)}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}

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
          href={
            match.kind === 'video' && match.segments[0]
              ? seekUrl(match.original_url, match.segments[0].start_ms)
              : match.original_url
          }
          target="_blank"
          rel="noreferrer noopener"
          className="bg-accent-500 hover:bg-accent-600 rounded-lg px-4 py-2 text-sm font-medium transition-colors"
        >
          {match.kind === 'video' ? '播放视频' : '查看原图'}
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
