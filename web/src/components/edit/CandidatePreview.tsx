import { useEffect, useRef } from 'react'

import type { Candidate } from '../../editApi'
import { scenePreviewUrl, sceneThumbUrl } from '../../editApi'
import { formatMs } from '../../lib/time'

/**
 * 候选详看：视频候选循环播放**匹配到的那几秒**（scene 边界），照片候选放大关键帧。
 *
 * 视频不自建播放器：src 用媒体片段标识 `#t=start` 原生定位到段首，
 * timeupdate 里过段尾拉回段首实现循环。用户主动往前拖不拦 —— 想看上下文是合理需求。
 */
export function CandidatePreview({
  candidate,
  shotIdx,
}: {
  candidate: Candidate
  shotIdx: number
}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const startS = candidate.start_ms / 1000
  const endS = candidate.end_ms / 1000

  // 切换候选时把播放头拉回新候选的段首（媒体片段只在首次加载生效，这里兜底）
  useEffect(() => {
    const el = videoRef.current
    if (el && el.readyState > 0) el.currentTime = startS
  }, [candidate.id, startS])

  if (candidate.kind !== 'video') {
    return (
      <img
        src={sceneThumbUrl(candidate.scene_id)}
        alt={`镜头 ${shotIdx} 候选 ${candidate.rank} 放大查看`}
        className="bg-ink-900 aspect-video w-full rounded-lg object-contain"
      />
    )
  }

  return (
    <figure className="space-y-1.5">
      <video
        key={candidate.id}
        ref={videoRef}
        src={`${scenePreviewUrl(candidate.scene_id)}#t=${startS.toFixed(2)}`}
        poster={sceneThumbUrl(candidate.scene_id)}
        className="bg-ink-900 aspect-video w-full rounded-lg"
        autoPlay
        muted
        playsInline
        controls
        preload="metadata"
        aria-label={`镜头 ${shotIdx} 候选 ${candidate.rank} 的匹配片段`}
        onTimeUpdate={(e) => {
          const el = e.currentTarget
          if (endS > startS && el.currentTime >= endS) el.currentTime = startS
        }}
      />
      <figcaption className="text-ink-600 text-xs">
        匹配片段 {formatMs(candidate.start_ms)} – {formatMs(candidate.end_ms)}，循环播放中
      </figcaption>
    </figure>
  )
}
