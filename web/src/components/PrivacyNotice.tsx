/**
 * 隐私告知。必须常驻在上传区附近，不允许折叠隐藏 ——
 * 同意必须是知情的，藏起来的告知等于没有告知。见 docs/privacy.md。
 */
export function PrivacyNotice() {
  return (
    <p className="text-ink-400 text-[13px] leading-relaxed">
      你的自拍<strong className="text-ink-200 font-medium">仅用于本次检索</strong>
      ，处理完成后立即从内存中销毁，不会被保存到任何位置。照片库中的人脸特征数据用于匹配，
      你可以随时联系管理员将自己从检索中移除。
    </p>
  )
}

export function ConsentCheckbox({
  checked,
  onChange,
}: {
  checked: boolean
  onChange: (value: boolean) => void
}) {
  return (
    <label className="flex cursor-pointer items-start gap-3 text-sm">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="accent-accent-500 mt-0.5 size-4 shrink-0"
      />
      <span className="text-ink-200">
        我已阅读并理解上述说明，同意使用我的自拍进行一次人脸比对。
      </span>
    </label>
  )
}
