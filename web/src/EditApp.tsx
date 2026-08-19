import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from './api'
import type { FilterPreset, ProjectDetail, ProjectSummary, TimelineEvent } from './editApi'
import {
  approveShot,
  createProject,
  downloadUrl,
  feedbackShot,
  getEvents,
  getProject,
  listFilters,
  listProjects,
  regenerateProject,
  renderProject,
} from './editApi'
import { ReviewCard } from './components/edit/ReviewCard'
import { Timeline } from './components/edit/Timeline'
import { isBusy, statusLabel } from './lib/editEvents'

const POLL_MS = 3000

/**
 * 剪辑聊天窗。
 *
 * 所有状态都在服务端（事件时间线 + 状态表），本组件只是它们的视图 ——
 * 关掉页面、换设备再登录，从 /edit/projects 与 events 完整恢复，这就是断点恢复。
 */
export function EditApp({ album, onLogout }: { album: string; onLogout: () => void }) {
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [filters, setFilters] = useState<FilterPreset[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [detail, setDetail] = useState<ProjectDetail | null>(null)
  const [events, setEvents] = useState<TimelineEvent[]>([])
  const [script, setScript] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const lastSeq = useRef(0)

  const handleError = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && err.status === 401) {
        onLogout()
        return
      }
      setError(err instanceof ApiError ? err.message : '操作失败，请稍后再试')
    },
    [onLogout],
  )

  const refreshDetail = useCallback(
    async (id: string) => {
      try {
        setDetail(await getProject(id))
      } catch (err) {
        handleError(err)
      }
    },
    [handleError],
  )

  useEffect(() => {
    void listProjects().then(setProjects).catch(handleError)
    void listFilters()
      .then(setFilters)
      .catch(() => setFilters([]))
  }, [handleError])

  // 打开会话：全量拉时间线 + 详情
  useEffect(() => {
    if (!activeId) return
    lastSeq.current = 0
    setEvents([])
    setDetail(null)
    void getEvents(activeId, 0)
      .then((res) => {
        setEvents(res.events)
        lastSeq.current = res.last_seq
      })
      .catch(handleError)
    void refreshDetail(activeId)
  }, [activeId, refreshDetail, handleError])

  // 轮询增量事件。后台在干活（建库/选片/渲染）时新事件会陆续出现。
  useEffect(() => {
    if (!activeId || !detail) return
    if (!isBusy(detail.status)) return
    const timer = setInterval(() => {
      void getEvents(activeId, lastSeq.current)
        .then((res) => {
          if (res.events.length > 0) {
            setEvents((prev) => [...prev, ...res.events])
            lastSeq.current = res.last_seq
          }
          if (res.project_status !== detail.status) void refreshDetail(activeId)
        })
        .catch(() => undefined) // 轮询失败静默，下一轮再试
    }, POLL_MS)
    return () => clearInterval(timer)
  }, [activeId, detail, refreshDetail])

  async function submitScript() {
    setPending(true)
    setError(null)
    try {
      const created = await createProject(script.trim())
      setScript('')
      setProjects((prev) => [created, ...prev])
      setActiveId(created.id)
    } catch (err) {
      handleError(err)
    } finally {
      setPending(false)
    }
  }

  const act = useCallback(
    async (fn: () => Promise<unknown>) => {
      if (!activeId) return
      setPending(true)
      setError(null)
      try {
        await fn()
        lastSeq.current = 0
        const res = await getEvents(activeId, 0)
        setEvents(res.events)
        lastSeq.current = res.last_seq
        await refreshDetail(activeId)
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          // 另一台设备改过了：拉最新状态，让用户基于新状态重试
          await refreshDetail(activeId)
        }
        handleError(err)
      } finally {
        setPending(false)
      }
    },
    [activeId, refreshDetail, handleError],
  )

  // ---------------------------------------------------------------- 会话列表页
  if (!activeId) {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 pb-16">
        <header className="flex items-baseline justify-between gap-3 py-5">
          <div>
            <h1 className="text-lg font-semibold tracking-tight">剪辑助手</h1>
            <p className="text-ink-600 text-xs">相册 {album}</p>
          </div>
          <button
            type="button"
            onClick={onLogout}
            className="text-ink-600 hover:text-ink-200 text-xs transition-colors"
          >
            退出
          </button>
        </header>

        <section className="bg-ink-900/60 border-ink-800 space-y-3 rounded-xl border p-4">
          <label htmlFor="script" className="block text-sm font-medium">
            新建剪辑：把剧本贴进来
          </label>
          <p className="text-ink-400 text-[13px] leading-relaxed">
            按镜头写清每个画面（编号或空行分隔）。素材就是这个相册里的照片和视频，
            系统会为每个镜头找出候选，由你逐一确认后出片。
          </p>
          <textarea
            id="script"
            value={script}
            onChange={(e) => setScript(e.target.value)}
            rows={6}
            placeholder={
              '例：\n1. 开场：活动场地全景\n2. 大家陆续到场、打招呼\n3. 切蛋糕的瞬间\n…'
            }
            className="bg-ink-900 border-ink-800 focus:border-accent-500 w-full rounded-xl border px-4 py-3 text-base leading-relaxed outline-none transition-colors"
          />
          <button
            type="button"
            disabled={pending || !script.trim()}
            onClick={() => void submitScript()}
            className="bg-accent-500 hover:bg-accent-600 disabled:bg-ink-800 disabled:text-ink-600 w-full rounded-xl px-4 py-3 font-medium transition-colors disabled:cursor-not-allowed"
          >
            {pending ? '正在创建…' : '开始剪辑'}
          </button>
        </section>

        {error && (
          <p role="alert" className="text-danger-500 mt-4 text-sm">
            {error}
          </p>
        )}

        {projects.length > 0 && (
          <section className="mt-8 space-y-2">
            <h2 className="text-ink-400 text-sm">历史剪辑</h2>
            <ul className="space-y-2">
              {projects.map((p) => (
                <li key={p.id}>
                  <button
                    type="button"
                    onClick={() => setActiveId(p.id)}
                    className="bg-ink-900 hover:bg-ink-800 flex w-full items-center justify-between gap-3 rounded-xl px-4 py-3 text-left transition-colors"
                  >
                    <span className="truncate text-sm">{p.title || '未命名剪辑'}</span>
                    <span className="text-ink-600 shrink-0 text-xs">
                      {statusLabel(p.status)}
                      {p.current_round > 1 ? ` · 第 ${p.current_round} 轮` : ''}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    )
  }

  // ---------------------------------------------------------------- 会话（聊天窗）页
  const reviewing = detail?.status === 'reviewing'
  const shots = detail?.shots ?? []
  const allLocked = shots.length > 0 && shots.every((s) => s.locked)
  const anyUnlocked = shots.some((s) => !s.locked)

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-16">
      <header className="border-ink-800 flex items-center justify-between gap-3 border-b py-4">
        <button
          type="button"
          onClick={() => setActiveId(null)}
          className="text-ink-600 hover:text-ink-200 text-xs transition-colors"
        >
          ← 全部剪辑
        </button>
        <div className="min-w-0 text-center">
          <h1 className="truncate text-sm font-semibold tracking-tight">
            {detail?.title ?? '加载中…'}
          </h1>
          {detail && (
            <p className="text-ink-600 text-xs">
              {statusLabel(detail.status)}
              {detail.current_round > 1 ? ` · 第 ${detail.current_round} 轮` : ''}
            </p>
          )}
        </div>
        <span className="w-14" aria-hidden="true" />
      </header>

      <main className="space-y-6 py-5">
        <Timeline events={events} />

        {detail && isBusy(detail.status) && (
          <p className="text-ink-400 text-center text-sm">
            {statusLabel(detail.status)}…可以先离开，回来接着弄，进度不会丢
          </p>
        )}

        {detail?.status === 'failed' && (
          <p role="alert" className="text-danger-500 text-center text-sm">
            处理失败了。可以回到列表重新提交剧本，问题持续请联系管理员。
          </p>
        )}

        {reviewing && detail && (
          <section className="space-y-3">
            <h2 className="text-ink-400 text-sm">
              逐镜头评审：满意就从候选里选一条锁定；不满意就写下想法
            </h2>
            {shots.map((shot) => (
              <ReviewCard
                key={shot.id}
                shot={shot}
                filters={filters}
                defaultFilter={detail.default_filter_slug}
                pending={pending}
                onApprove={(shotId, candId, filterSlug) =>
                  void act(() =>
                    approveShot(detail.id, shotId, candId, filterSlug, detail.state_version),
                  )
                }
                onFeedback={(shotId, text) =>
                  void act(() => feedbackShot(detail.id, shotId, text, detail.state_version))
                }
              />
            ))}

            {anyUnlocked && (
              <div className="bg-ink-900/60 border-ink-800 space-y-2 rounded-xl border p-4">
                <label htmlFor="note" className="text-ink-400 block text-xs">
                  整体想法（可选，会和每个镜头的反馈一起用于重新生成）
                </label>
                <textarea
                  id="note"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  rows={2}
                  className="bg-ink-900 border-ink-800 focus:border-accent-500 w-full rounded-xl border px-4 py-3 text-base leading-relaxed outline-none transition-colors"
                />
                <button
                  type="button"
                  disabled={pending}
                  onClick={() =>
                    void act(() =>
                      regenerateProject(detail.id, note.trim(), detail.state_version),
                    )
                  }
                  className="bg-ink-900 hover:bg-ink-800 disabled:text-ink-600 w-full rounded-xl px-4 py-3 text-sm font-medium transition-colors disabled:cursor-not-allowed"
                >
                  对未锁定的镜头重新生成
                </button>
              </div>
            )}

            <button
              type="button"
              disabled={pending || !allLocked}
              onClick={() => void act(() => renderProject(detail.id, detail.state_version))}
              className="bg-accent-500 hover:bg-accent-600 disabled:bg-ink-800 disabled:text-ink-600 w-full rounded-xl px-4 py-3 font-medium transition-colors disabled:cursor-not-allowed"
            >
              {allLocked ? '确认渲染' : '全部镜头锁定后即可渲染'}
            </button>
          </section>
        )}

        {detail?.status === 'done' && (
          <a
            href={downloadUrl(detail.id)}
            className="bg-accent-500 hover:bg-accent-600 block w-full rounded-xl px-4 py-3 text-center font-medium transition-colors"
          >
            下载片段包（zip，含 manifest）
          </a>
        )}

        {error && (
          <p role="alert" className="text-danger-500 text-sm">
            {error}
          </p>
        )}
      </main>
    </div>
  )
}
