const BASE = import.meta.env.VITE_API_BASE_URL ?? '/api'

export interface Segment {
  // 命中人脸在视频里的出现区间（毫秒）。跳转用 original_url + #t=秒
  start_ms: number
  end_ms: number
  score: number
}

export interface Match {
  photo_id: string
  // album slug，形如 '2026-08-10'，与源站 /album/<slug> 一致
  album: string
  score: number
  thumb_url: string | null
  original_url: string
  kind: 'image' | 'video'
  duration_ms: number | null
  // 仅视频：出现时间段（相邻段服务端已合并）。照片恒为空数组
  segments: Segment[]
}

export interface Album {
  album: string
  photo_count: number
  face_count: number
}

export interface Captcha {
  token: string
  // SVG 文本，转 data URI 后塞进 <img> 展示
  svg: string
}

export interface SessionState {
  authenticated: boolean
  // 本 session 绑定的相册；null = 可搜全部相册
  album: string | null
}

export type SearchStatus = 'ok' | 'no_face' | 'no_match'

export interface SearchResponse {
  matches: Match[]
  // 从几张自拍里取到了可用的人脸（每张最多取一张 —— 最明显的那张）
  faces_used: number
  status: SearchStatus
  message: string | null
  latency_ms: number
  // 服务端确认自拍已销毁。恒为 true，但要显式展示给用户而不是让他猜。
  selfie_discarded: boolean
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

/**
 * CSRF 双提交：登录时后端会种一个**非** HttpOnly 的 zrc_csrf cookie，
 * 这里读出来回填到 X-CSRF-Token 头。跨站攻击者发得出请求但读不到 cookie，
 * 所以头对得上就证明请求来自本站页面。
 */
function csrfHeaders(): Record<string, string> {
  const token = document.cookie.match(/(?:^|;\s*)zrc_csrf=([^;]+)/)?.[1]
  return token ? { 'X-CSRF-Token': token } : {}
}

export async function fetchCaptcha(): Promise<Captcha> {
  const response = await fetch(`${BASE}/session/captcha`, { credentials: 'same-origin' })
  if (!response.ok) await parseError(response)
  return (await response.json()) as Captcha
}

export async function login(
  inviteCode: string,
  consent: boolean,
  captchaToken: string,
  captchaAnswer: string,
): Promise<SessionState> {
  const response = await fetch(`${BASE}/session/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // session 走 HttpOnly cookie，必须带上凭据
    credentials: 'same-origin',
    body: JSON.stringify({
      invite_code: inviteCode,
      consent,
      captcha_token: captchaToken,
      captcha_answer: captchaAnswer,
    }),
  })
  if (!response.ok) await parseError(response)
  const body = (await response.json()) as { album?: string | null }
  return { authenticated: true, album: body.album ?? null }
}

export async function checkSession(): Promise<SessionState> {
  const response = await fetch(`${BASE}/session/me`, { credentials: 'same-origin' })
  if (!response.ok) return { authenticated: false, album: null }
  const body = (await response.json()) as { authenticated?: boolean; album?: string | null }
  return { authenticated: body.authenticated === true, album: body.album ?? null }
}

export async function logout(): Promise<void> {
  await fetch(`${BASE}/session/logout`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: csrfHeaders(),
  })
}

export async function listAlbums(): Promise<Album[]> {
  const response = await fetch(`${BASE}/albums`, { credentials: 'same-origin' })
  if (!response.ok) await parseError(response)
  return (await response.json()) as Album[]
}

export interface PhotoItem {
  photo_id: string
  album: string
  thumb_url: string | null
  original_url: string
  face_count: number
  kind: 'image' | 'video'
  duration_ms: number | null
}

export interface PhotoPage {
  items: PhotoItem[]
  total: number
  page: number
  per_page: number
}

export interface FaceItem {
  face_id: string
  // 小图可能还没回填（旧数据），null 时前端展示占位
  thumb_url: string | null
}

export async function listPhotos(page: number, album?: string): Promise<PhotoPage> {
  const params = new URLSearchParams({ page: String(page) })
  if (album) params.set('album', album)
  const response = await fetch(`${BASE}/photos?${params}`, { credentials: 'same-origin' })
  if (!response.ok) await parseError(response)
  return (await response.json()) as PhotoPage
}

export async function listFaces(photoId: string): Promise<FaceItem[]> {
  const response = await fetch(`${BASE}/photos/${photoId}/faces`, {
    credentials: 'same-origin',
  })
  if (!response.ok) await parseError(response)
  return (await response.json()) as FaceItem[]
}

/** 用一张已入库人脸检索相关照片。与自拍检索独立限流（默认 4 次/小时/设备）。 */
export async function searchByFace(faceId: string): Promise<SearchResponse> {
  const response = await fetch(`${BASE}/search/by-face`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders() },
    body: JSON.stringify({ face_id: faceId }),
  })
  if (!response.ok) await parseError(response)
  return (await response.json()) as SearchResponse
}

export async function search(files: File[], album?: string): Promise<SearchResponse> {
  const form = new FormData()
  for (const file of files) form.append('selfies', file, file.name)
  // 带上 album 只在那一个相册里检索。绑定相册的邀请码不需要带 —— 后端会强制限定
  if (album) form.append('album', album)

  const response = await fetch(`${BASE}/search`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: csrfHeaders(),
    body: form,
  })
  if (!response.ok) await parseError(response)
  return (await response.json()) as SearchResponse
}
