import type { SearchStatus } from '../api'

/**
 * 空状态。
 *
 * 「没检测到人脸」和「检测到了但没匹配上」是两种完全不同的情况，用户该做的事也不同 ——
 * 混成一句「没有结果」会让人不知道下一步该干什么。这两种都会高频出现，值得分别设计。
 */
export function EmptyState({
  status,
  message,
}: {
  status: SearchStatus
  message: string | null
}) {
  const content =
    status === 'no_face'
      ? {
          title: '没有识别到人脸',
          hint: message ?? '请用光线充足、正面清晰、脸部占画面较大的自拍再试一次。',
          tips: ['正对镜头，不要侧脸', '避免逆光和过暗环境', '摘掉墨镜和口罩'],
        }
      : {
          title: '没有找到匹配的照片',
          hint: message ?? '可能这些活动里没有你的照片。',
          tips: [
            '换一张更清晰的自拍，或多上传一两张不同角度的',
            '如果你在合影里站得较远、脸比较小，可能无法被识别',
            '照片库仍在陆续导入，过些天可以再试',
          ],
        }

  return (
    <section className="border-ink-800 space-y-4 rounded-xl border border-dashed px-5 py-8 text-center">
      <h2 className="text-base font-medium">{content.title}</h2>
      <p className="text-ink-400 mx-auto max-w-sm text-sm leading-relaxed">{content.hint}</p>
      <ul className="text-ink-600 mx-auto max-w-sm space-y-1.5 text-left text-[13px]">
        {content.tips.map((tip) => (
          <li key={tip} className="flex gap-2">
            <span aria-hidden="true">·</span>
            <span>{tip}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}
