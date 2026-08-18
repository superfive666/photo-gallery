/** 视频时间的展示与跳转工具。 */

/** 毫秒 → "3:12" / "1:03:12"。字幕式格式，跑团用户都熟。 */
export function formatMs(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m)
  const ss = String(s).padStart(2, '0')
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`
}

/**
 * 视频直链 + 媒体片段标识：浏览器打开 `video.mp4#t=192` 会从 192 秒开始播。
 * 原生行为，不需要自建播放器。往前找 1 秒，给「人刚出现」留个提前量。
 */
export function seekUrl(originalUrl: string, startMs: number): string {
  const seconds = Math.max(0, Math.floor(startMs / 1000) - 1)
  return `${originalUrl}#t=${seconds}`
}
