/**
 * 「你的自拍已经删了」确认。
 *
 * 检索完成后必须显式告知，而不是只在上传前承诺一句。上传前的说明是「我们打算怎么做」，
 * 检索后的这一行是「已经做了」—— 后者才是用户真正在意的那个时刻。
 *
 * 用 role="status" 而不是 role="alert"：这是正常完成的确认，不该打断屏幕阅读器
 * 正在播报的结果内容。
 */
export function DiscardNotice({ confirmed }: { confirmed: boolean }) {
  if (!confirmed) return null

  return (
    <p
      role="status"
      className="text-ink-400 flex items-start gap-2 text-[13px] leading-relaxed"
    >
      <span aria-hidden="true">✓</span>
      <span>
        你上传的自拍<strong className="text-ink-200 font-medium">已从服务器删除</strong>
        ，我们没有留底。下次检索需要重新上传。
      </span>
    </p>
  )
}
