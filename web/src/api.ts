const BASE = import.meta.env.VITE_API_BASE_URL ?? '/api'

export interface Match {
  photo_id: string
  album_id: string
  album_name: string
  filename: string
  taken_at: string | null
  score: number
  thumb_url: string | null
  original_url: string
}

export type SearchStatus = 'ok' | 'no_face' | 'no_match'

export interface SearchResponse {
  matches: Match[]
  faces_detected: number
  status: SearchStatus
  message: string | null
  latency_ms: number
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly retryAfterSeconds?: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function parseError(response: Response): Promise<never> {
  let detail = `请求失败（${response.status}）`
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') detail = body.detail
  } catch {
    // 响应体不是 JSON，保留默认文案
  }
  const retryAfter = response.headers.get('Retry-After')
  throw new ApiError(response.status, detail, retryAfter ? Number(retryAfter) : undefined)
}

export async function login(inviteCode: string, consent: boolean): Promise<void> {
  const response = await fetch(`${BASE}/session/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // session 走 HttpOnly cookie，必须带上凭据
    credentials: 'same-origin',
    body: JSON.stringify({ invite_code: inviteCode, consent }),
  })
  if (!response.ok) await parseError(response)
}

export async function checkSession(): Promise<boolean> {
  const response = await fetch(`${BASE}/session/me`, { credentials: 'same-origin' })
  if (!response.ok) return false
  const body = (await response.json()) as { authenticated?: boolean }
  return body.authenticated === true
}

export async function logout(): Promise<void> {
  await fetch(`${BASE}/session/logout`, { method: 'POST', credentials: 'same-origin' })
}

export async function search(files: File[]): Promise<SearchResponse> {
  const form = new FormData()
  for (const file of files) form.append('selfies', file, file.name)

  const response = await fetch(`${BASE}/search`, {
    method: 'POST',
    credentials: 'same-origin',
    body: form,
  })
  if (!response.ok) await parseError(response)
  return (await response.json()) as SearchResponse
}
