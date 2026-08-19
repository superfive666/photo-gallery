import { describe, expect, it } from 'vitest'

import type { TimelineEvent } from '../editApi'
import { describeEvent, formatDuration, isBusy, statusLabel } from './editEvents'

function event(kind: string, payload: Record<string, unknown> = {}): TimelineEvent {
  return { seq: 1, actor: 'assistant', kind, payload, created_at: '2026-08-19T00:00:00Z' }
}

describe('describeEvent', () => {
  it('renders known kinds without technical jargon', () => {
    expect(describeEvent(event('script_submitted', { title: '毕业视频' }))).toContain(
      '毕业视频',
    )
    expect(describeEvent(event('render_done', { shots: 5 }))).toContain('5')
    // 文案不出现实现细节
    const all = [
      'script_submitted',
      'ingest_queued',
      'ingest_done',
      'polish_done',
      'candidates_ready',
      'shot_locked',
      'render_done',
      'project_failed',
    ].map((k) => describeEvent(event(k)))
    for (const text of all) {
      expect(text).not.toMatch(/embedding|向量|CLIP|LLM/i)
    }
  })

  it('falls back to raw kind for unknown events', () => {
    expect(describeEvent(event('mystery_kind'))).toBe('mystery_kind')
  })
})

describe('status helpers', () => {
  it('labels every status', () => {
    for (const s of [
      'ingesting',
      'parsing',
      'matching',
      'reviewing',
      'refining',
      'rendering',
      'done',
      'failed',
    ] as const) {
      expect(statusLabel(s)).toBeTruthy()
    }
  })

  it('busy states keep polling, terminal states do not', () => {
    expect(isBusy('rendering')).toBe(true)
    expect(isBusy('reviewing')).toBe(false)
    expect(isBusy('done')).toBe(false)
  })
})

describe('formatDuration', () => {
  it('formats seconds and minutes', () => {
    expect(formatDuration(0, 8000)).toBe('8s')
    expect(formatDuration(0, 95_000)).toBe('1m35s')
  })
})
