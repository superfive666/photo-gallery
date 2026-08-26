/** 时间线事件 → 展示文案。纯函数，可单测。文案不出现任何技术术语。 */

import type { ProjectStatus, TimelineEvent } from '../editApi'

export function describeEvent(event: TimelineEvent): string {
  const p = event.payload
  switch (event.kind) {
    case 'script_submitted':
      return `提交了剧本「${str(p.title)}」`
    case 'ingest_queued':
      return `相册「${str(p.album)}」首次使用，正在准备素材（下载与分析可能需要几分钟到几十分钟，可以先离开，回来接着弄）`
    case 'ingest_done':
      return `素材准备完成：${num(p.scenes)} 个可选片段`
    case 'polish_done':
      return `剧本已整理成 ${shotCount(p)} 个镜头${p.default_filter ? `，推荐滤镜「${str(p.default_filter)}」` : ''}`
    case 'refine_done':
      return `第 ${num(p.round)} 轮：重新构思了 ${num(p.refined)} 个镜头`
    case 'candidates_ready':
      return '候选片段已找好，请逐镜头评审'
    case 'review_ready':
      return `第 ${num(p.round)} 轮评审开始`
    case 'shot_locked':
      return p.backup_candidate_id
        ? `锁定了镜头 ${num(p.idx)}（含备选）`
        : `锁定了镜头 ${num(p.idx)}`
    case 'shot_feedback':
      return `对镜头 ${num(p.idx)} 提出：${str(p.text)}`
    case 'regenerate_requested':
      return `请求重新生成（第 ${num(p.round)} 轮，${num(p.unlocked)} 个镜头）`
    case 'render_requested':
      return `确认渲染 ${num(p.shots)} 个镜头`
    case 'render_done':
      return `渲染完成，共 ${num(p.shots)} 个片段，可以下载了`
    case 'project_failed':
      return '处理失败了，请稍后重试或换一份剧本'
    default:
      return event.kind
  }
}

export function statusLabel(status: ProjectStatus): string {
  switch (status) {
    case 'ingesting':
      return '准备素材中'
    case 'parsing':
    case 'matching':
      return '整理剧本与选片中'
    case 'refining':
      return '重新生成中'
    case 'reviewing':
      return '待评审'
    case 'rendering':
      return '渲染中'
    case 'done':
      return '已完成'
    case 'failed':
      return '失败'
  }
}

/** 这些状态下后台还在干活，前端要保持轮询。 */
export function isBusy(status: ProjectStatus): boolean {
  return ['ingesting', 'parsing', 'matching', 'refining', 'rendering'].includes(status)
}

export function formatDuration(startMs: number, endMs: number): string {
  const seconds = Math.max(0, Math.round((endMs - startMs) / 1000))
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m${seconds % 60}s`
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function num(v: unknown): number {
  return typeof v === 'number' ? v : 0
}

function shotCount(p: Record<string, unknown>): number {
  return Array.isArray(p.shots) ? p.shots.length : 0
}
