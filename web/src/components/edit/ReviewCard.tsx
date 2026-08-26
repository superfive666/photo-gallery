import { useState } from 'react'

import type { Candidate, FilterPreset, Shot } from '../../editApi'
import { filterPreviewUrl, sceneThumbUrl } from '../../editApi'
import { formatDuration } from '../../lib/editEvents'
import { EMPTY_SELECTION, togglePick } from '../../lib/shotSelection'
import { CandidatePreview } from './CandidatePreview'

/**
 * 单个镜头的评审卡片。流程：点候选看匹配片段 → 设主选（可再设一条备选）→ 锁定；
 * 都不满意 → 写想法，等「重新生成」。
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
  onApprove: (
    shotId: string,
    candidateId: string,
    backupCandidateId: string | null,
    filterSlug: string | null,
  ) => void
  onFeedback: (shotId: string, text: string) => void
}) {
  const [sel, setSel] = useState(EMPTY_SELECTION)
  const [previewId, setPreviewId] = useState<string | null>(null)
  const [filterSlug, setFilterSlug] = useState<string | null>(shot.filter_slug ?? defaultFilter)
  const [complaining, setComplaining] = useState(false)
  const [complaint, setComplaint] = useState(shot.feedback ?? '')

  const selectable = shot.candidates.filter((c) => c.status !== 'rejected')
  const previewing = selectable.find((c) => c.id === previewId) ?? null

  if (shot.locked) {
    const picks: Array<{ label: string; candidate: Candidate | undefined }> = [
      {
        label: '主选',
        candidate: shot.candidates.find((c) => c.id === shot.locked_candidate_id),
      },
    ]
    if (shot.backup_candidate_id) {
      picks.push({
        label: '备选',
        candidate: shot.candidates.find((c) => c.id === shot.backup_candidate_id),
      })
    }
    return (
      <section className="bg-ink-900/60 border-ink-800 rounded-xl border p-4">
        <header className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-medium">
            镜头 {shot.idx} · {shot.description}
          </h3>
          <span className="text-ink-600 shrink-0 text-xs">已锁定 ✓</span>
        </header>
        <div className="mt-3 space-y-2">
          {picks.map(
            ({ label, candidate }) =>
              candidate && (
                <div key={candidate.id} className="flex items-center gap-3">
                  <img
                    src={sceneThumbUrl(candidate.scene_id)}
                    alt={`镜头 ${shot.idx} ${label}画面`}
                    className="bg-ink-900 aspect-video w-28 rounded-lg object-cover"
                  />
                  <p className="text-ink-600 text-xs">
                    {label} ·{' '}
                    {candidate.kind === 'video'
                      ? formatDuration(candidate.start_ms, candidate.end_ms)
                      : '照片'}
                    {shot.filter_slug ? ` · 滤镜 ${shot.filter_slug}` : ''}
                  </p>
                </div>
              ),
          )}
        </div>
      </section>
    )
  }

  const badge = (c: Candidate) =>
    sel.primaryId === c.id ? '主选' : sel.backupId === c.id ? '备选' : null

  return (
    <section className="bg-ink-900/60 border-ink-800 rounded-xl border p-4">
      <header className="space-y-1">
        <h3 className="text-sm font-medium">
          镜头 {shot.idx} · {shot.description}
        </h3>
        {shot.feedback && !complaining && (
          <p className="text-warn-500 text-xs">已记录你的想法，点「重新生成」后会换一批</p>
        )}
      </header>

      {/* 候选缩略图，横向滚动，移动端友好。点开看匹配片段再决定 */}
      {selectable.length > 0 ? (
        <>
          <div className="-mx-1 mt-3 flex gap-2 overflow-x-auto px-1 pb-1">
            {selectable.map((c) => (
              <button
                key={c.id}
                type="button"
                onClick={() => setPreviewId(previewId === c.id ? null : c.id)}
                aria-label={`查看候选 ${c.rank}${badge(c) ? `（当前${badge(c)}）` : ''}`}
                className={`relative shrink-0 overflow-hidden rounded-lg border-2 transition-colors ${
                  previewId === c.id ? 'border-accent-500' : 'border-transparent'
                }`}
              >
                <img
                  src={sceneThumbUrl(c.scene_id)}
                  alt={`镜头 ${shot.idx} 候选 ${c.rank}`}
                  className="bg-ink-900 aspect-video w-36 object-cover"
                  loading="lazy"
                />
                {badge(c) && (
                  <span
                    className={`absolute top-1 left-1 rounded px-1.5 py-0.5 text-xs font-medium ${
                      badge(c) === '主选' ? 'bg-accent-500' : 'bg-ink-950/80'
                    }`}
                  >
                    {badge(c)}
                  </span>
                )}
                <span className="bg-ink-950/80 absolute right-1 bottom-1 rounded px-1.5 py-0.5 text-xs">
                  {c.kind === 'video' ? formatDuration(c.start_ms, c.end_ms) : '照片'}
                </span>
              </button>
            ))}
          </div>
          {!previewing && (
            <p className="text-ink-600 mt-2 text-xs">
              点候选可以播放它匹配到的片段，看满意再选
            </p>
          )}
        </>
      ) : (
        <p className="text-ink-600 mt-3 text-xs">
          这一轮没找到合适的候选 —— 素材库里可能没有这个画面。写下更具体的想法再试一次，
          或者调整剧本里这个镜头。
        </p>
      )}

      {/* 详看区：匹配片段循环播放 + 设为主选/备选 */}
      {previewing && (
        <div className="mt-3 space-y-2">
          <CandidatePreview candidate={previewing} shotIdx={shot.idx} />
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => setSel(togglePick(sel, previewing.id, 'primary'))}
              className="bg-ink-900 hover:bg-ink-800 rounded-xl px-4 py-3 text-sm font-medium transition-colors"
            >
              {sel.primaryId === previewing.id ? '取消主选' : '设为主选'}
            </button>
            <button
              type="button"
              onClick={() => setSel(togglePick(sel, previewing.id, 'backup'))}
              className="bg-ink-900 hover:bg-ink-800 rounded-xl px-4 py-3 text-sm font-medium transition-colors"
            >
              {sel.backupId === previewing.id ? '取消备选' : '设为备选'}
            </button>
          </div>
          {sel.backupId && !sel.primaryId && (
            <p className="text-ink-600 text-xs">备选已记下 —— 还需要一条主选才能锁定</p>
          )}
        </div>
      )}

      {/* 锁定行：有主选即可锁，备选可有可无 */}
      {sel.primaryId && (
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
            onClick={() =>
              sel.primaryId && onApprove(shot.id, sel.primaryId, sel.backupId, filterSlug)
            }
            className="bg-accent-500 hover:bg-accent-600 disabled:bg-ink-800 disabled:text-ink-600 rounded-xl px-4 py-3 text-sm font-medium transition-colors disabled:cursor-not-allowed"
          >
            {sel.backupId ? '锁定（主选 + 备选）' : '锁定这条'}
          </button>
        </div>
      )}

      <div className="mt-3">
        {!complaining ? (
          <button
            type="button"
            onClick={() => setComplaining(true)}
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
