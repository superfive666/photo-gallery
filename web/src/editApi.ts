/** 剪辑域接口客户端。所有请求带 cookie；401 由调用方静默回到邀请码页。 */

import { ApiError, csrfHeaders } from './api'

const BASE = import.meta.env.VITE_API_BASE_URL ?? '/api'

export interface FilterPreset {
  slug: string
  display_name: string
  builtin: boolean
}

export interface ProjectSummary {
  id: string
  title: string
  album: string
  status: ProjectStatus
  current_round: number
  state_version: number
  created_at: string
  updated_at: string
}

export type ProjectStatus =
  | 'ingesting'
  | 'parsing'
  | 'matching'
  | 'reviewing'
  | 'refining'
  | 'rendering'
  | 'done'
  | 'failed'

export interface Candidate {
  id: string
  scene_id: string
  rank: number
  similarity: number
  quality: number
  final_score: number
  status: 'pending' | 'approved' | 'rejected'
  kind: 'image' | 'video'
  start_ms: number
  end_ms: number
  in_ms: number | null
  out_ms: number | null
}

export interface Shot {
  id: string
  idx: number
  source_text: string
  description: string
  queries: string[]
  media_kind: string
  filter_slug: string | null
  locked: boolean
  locked_candidate_id: string | null
  feedback: string | null
  round_no: number
  candidates: Candidate[]
}

export interface ProjectDetail extends ProjectSummary {
  script: string
  default_filter_slug: string | null
  error: string | null
  shots: Shot[]
}

export interface TimelineEvent {
  seq: number
  actor: 'user' | 'assistant' | 'system'
  kind: string
  payload: Record<string, unknown>
  created_at: string
}

export interface EventsResponse {
  events: TimelineEvent[]
  last_seq: number
  project_status: ProjectStatus
  state_version: number
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    credentials: 'same-origin',
    ...init,
  })
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`
    try {
      const body = (await response.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      // 响应体不是 JSON，保留默认文案
    }
    throw new ApiError(response.status, detail)
  }
  return (await response.json()) as T
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    // CSRF 双提交：与查找侧同一套（见 api.ts csrfHeaders 的说明）
    headers: { 'Content-Type': 'application/json', ...csrfHeaders() },
    body: JSON.stringify(body),
  })
}

export const listFilters = () => request<FilterPreset[]>('/edit/filters')
export const listProjects = () => request<ProjectSummary[]>('/edit/projects')
export const createProject = (script: string) =>
  post<ProjectSummary>('/edit/projects', { script })
export const getProject = (id: string) => request<ProjectDetail>(`/edit/projects/${id}`)
export const getEvents = (id: string, afterSeq: number) =>
  request<EventsResponse>(`/edit/projects/${id}/events?after_seq=${afterSeq}`)

export interface ActionResponse {
  ok: boolean
  state_version: number
  project_status: ProjectStatus
}

export const approveShot = (
  projectId: string,
  shotId: string,
  candidateId: string,
  filterSlug: string | null,
  stateVersion: number,
) =>
  post<ActionResponse>(`/edit/projects/${projectId}/shots/${shotId}/approve`, {
    candidate_id: candidateId,
    filter_slug: filterSlug,
    state_version: stateVersion,
  })

export const feedbackShot = (
  projectId: string,
  shotId: string,
  text: string,
  stateVersion: number,
) =>
  post<ActionResponse>(`/edit/projects/${projectId}/shots/${shotId}/feedback`, {
    text,
    state_version: stateVersion,
  })

export const regenerateProject = (projectId: string, note: string, stateVersion: number) =>
  post<ActionResponse>(`/edit/projects/${projectId}/regenerate`, {
    note: note || null,
    state_version: stateVersion,
  })

export const renderProject = (projectId: string, stateVersion: number) =>
  post<ActionResponse>(`/edit/projects/${projectId}/render`, { state_version: stateVersion })

export const sceneThumbUrl = (sceneId: string) => `${BASE}/edit/scenes/${sceneId}/thumb`
export const filterPreviewUrl = (slug: string) => `${BASE}/edit/filters/${slug}/preview`
export const downloadUrl = (projectId: string) => `${BASE}/edit/projects/${projectId}/download`
