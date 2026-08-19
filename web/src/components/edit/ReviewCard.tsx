import { useState } from 'react'

import type { FilterPreset, Shot } from '../../editApi'
import { filterPreviewUrl, sceneThumbUrl } from '../../editApi'
import { formatDuration } from '../../lib/editEvents'

/**
 * 单个镜头的评审卡片：先表态（满意/不满意），再操作。
 * 满意 → 从候选里勾一条 + 选滤镜 → 锁定；不满意 → 写想法，等「重新生成」。
 */
export function ReviewCard({
  shot,
  filters,
  defaultFilter,
  pending,
  onApprove,
  onFeedback,
}: {
  shot: Shot
  filters: FilterPreset[]
  defaultFilter: string | null
  pending: boolean
  onApprove: (shotId: string, candidateId: string, filterSlug: string | null) => void
  onFeedback: (shotId: string, text: string) => void
}) {
  const [mode, setMode] = useState<'idle' | 'pick' | 'complain'>('idle')
  const [candidateId, setCandidateId] = useState<string | null>(null)
  const [filterSlug, setFilterSlug] = useState<string | null>(shot.filter_slug ?? defaultFilter)
  const [complaint, setComplaint] = useState(shot.feedback ?? '')

  const selectable = shot.candidates.filter((c) => c.status !== 'rejected')

  if (shot.locked) {
    const chosen = shot.candidates.find((c) => c.id === shot.locked_candidate_id)
    return (
      <section className="bg-ink-900/60 border-ink-800 rounded-xl border p-4">
        <header className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-medium">
            镜头 {shot.idx} · {shot.description}
          </h3>
          <span className="text-ink-600 shrink-0 text-xs">已锁定 ✓</span>
        </header>
        {chosen && (
          <div className="mt-3 flex items-center gap-3">
            <img
              src={sceneThumbUrl(chosen.scene_id)}
              alt={`镜头 ${shot.idx} 已选画面`}
              className="bg-ink-900 aspect-video w-28 rounded-lg object-cover"
            />
            <p className="text-ink-600 text-xs">
              {chosen.kind === 'video'
                ? formatDuration(chosen.start_ms, chosen.end_ms)
                : '照片'}
              {shot.filter_slug ? ` · 滤镜 ${shot.filter_slug}` : ''}
            </p>
          </div>
        )}
      </section>
    )
  }

  return (
    <section className="bg-ink-900/60 border-ink-800 rounded-xl border p-4">
      <header className="space-y-1">
        <h3 className="text-sm font-medium">
          镜头 {shot.idx} · {shot.description}
        </h3>
        {shot.feedback && mode !== 'complain' && (
          <p className="text-warn-500 text-xs">已记录你的想法，点「重新生成」后会换一批</p>
        )}
      </header>

      {/* 候选缩略图，横向滚动，移动端友好 */}
      {selectable.length > 0 ? (
        <div className="-mx-1 mt-3 flex gap-2 overflow-x-auto px-1 pb-1">
          {selectable.map((c) => (
            <button
              key={c.id}
              type="button"
              onClick={() => {
                setCandidateId(c.id)
                setMode('pick')
              }}
              aria-label={`选择候选 ${c.rank}`}
              className={`relative shrink-0 overflow-hidden rounded-lg border-2 transition-colors ${
                candidateId === c.id ? 'border-accent-500' : 'border-transparent'
              }`}
            >
              <img
                src={sceneThumbUrl(c.scene_id)}
                alt={`镜头 ${shot.idx} 候选 ${c.rank}`}
                className="bg-ink-900 aspect-video w-36 object-cover"
                loading="lazy"
              />
              <span className="bg-ink-950/80 absolute right-1 bottom-1 rounded px-1.5 py-0.5 text-xs">
                {c.kind === 'video' ? formatDuration(c.start_ms, c.end_ms) : '照片'}
              </span>
            </button>
          ))}
        </div>
      ) : (
        <p className="text-ink-600 mt-3 text-xs">
          这一轮没找到合适的候选 —— 素材库里可能没有这个画面。写下更具体的想法再试一次，
          或者调整剧本里这个镜头。
        </p>
      )}

      {mode === 'pick' && candidateId && (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <label className="text-ink-400 text-xs" htmlFor={`filter-${shot.id}`}>
            滤镜
          </label>
          <select
            id={`filter-${shot.id}`}
            value={filterSlug ?? ''}
            onChange={(e) => setFilterSlug(e.target.value || null)}
            className="bg-ink-900 border-ink-800 focus:border-accent-500 rounded-xl border px-3 py-2 text-sm outline-none"
          >
            <option value="">不加滤镜</option>
            {filters.map((f) => (
              <option key={f.slug} value={f.slug}>
                {f.display_name}
              </option>
            ))}
          </select>
          {filterSlug && (
            <img
              src={filterPreviewUrl(filterSlug)}
              alt={`滤镜 ${filterSlug} 效果预览`}
              className="bg-ink-900 h-9 rounded-lg"
            />
          )}
          <button
            type="button"
            disabled={pending}
            onClick={() => onApprove(shot.id, candidateId, filterSlug)}
            className="bg-accent-500 hover:bg-accent-600 disabled:bg-ink-800 disabled:text-ink-600 rounded-xl px-4 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed"
          >
            就用这条，锁定
          </button>
        </div>
      )}

      <div className="mt-3">
        {mode !== 'complain' ? (
          <button
            type="button"
            onClick={() => setMode('complain')}
            className="text-ink-600 hover:text-ink-200 text-xs transition-colors"
          >
            都不满意？写下你的想法
          </button>
        ) : (
          <div className="space-y-2">
            <textarea
              value={complaint}
              onChange={(e) => setComplaint(e.target.value)}
              rows={2}
              placeholder="想要什么、不要什么，越具体越好（例：要室外的全景，不要背对镜头）"
              className="bg-ink-900 border-ink-800 focus:border-accent-500 w-full rounded-xl border px-4 py-3 text-base leading-relaxed outline-none transition-colors"
            />
            <button
              type="button"
              disabled={pending || !complaint.trim()}
              onClick={() => onFeedback(shot.id, complaint.trim())}
              className="bg-ink-900 hover:bg-ink-800 disabled:bg-ink-800 disabled:text-ink-600 rounded-xl px-4 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed"
            >
              记下这个想法
            </button>
          </div>
        )}
      </div>
    </section>
  )
}
