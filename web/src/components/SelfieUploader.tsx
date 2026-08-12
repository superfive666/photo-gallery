import { useRef, useState } from 'react'

import { compressImage } from '../lib/compress'
import { PrivacyNotice } from './PrivacyNotice'

const MAX_SELFIES = 3

export interface SelfieItem {
  id: string
  file: File
  previewUrl: string
}

/**
 * 自拍上传区。
 *
 * 支持多张（最多 3 张）：多角度自拍取均值后是一个更稳的查询点，是最便宜的召回率提升手段。
 * 所以 UI 要主动鼓励用户多传一张，而不是只当作可选项。
 */
export function SelfieUploader({
  items,
  onChange,
  onSubmit,
  pending,
  albumFilter,
}: {
  items: SelfieItem[]
  onChange: (items: SelfieItem[]) => void
  onSubmit: () => void
  pending: boolean
  // 相册筛选器由上层注入，放在「开始检索」之前 —— 它是可选的收窄手段，
  // 不该抢在上传之前挡住主流程。
  albumFilter?: React.ReactNode
}) {
  const cameraInput = useRef<HTMLInputElement>(null)
  const libraryInput = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)

  async function addFiles(fileList: FileList | null) {
    if (!fileList?.length) return
    setBusy(true)
    try {
      const room = MAX_SELFIES - items.length
      const incoming = Array.from(fileList).slice(0, room)
      const added: SelfieItem[] = []
      for (const file of incoming) {
        const { file: compressed } = await compressImage(file)
        added.push({
          id: crypto.randomUUID(),
          file: compressed,
          previewUrl: URL.createObjectURL(compressed),
        })
      }
      onChange([...items, ...added])
    } finally {
      setBusy(false)
    }
  }

  function remove(id: string) {
    const target = items.find((item) => item.id === id)
    // 释放 blob URL，否则连续换图会泄漏内存
    if (target) URL.revokeObjectURL(target.previewUrl)
    onChange(items.filter((item) => item.id !== id))
  }

  const full = items.length >= MAX_SELFIES

  return (
    <section className="space-y-4">
      <div className="grid grid-cols-3 gap-3">
        {items.map((item) => (
          <figure key={item.id} className="relative">
            <img
              src={item.previewUrl}
              alt="待检索的自拍"
              className="bg-ink-900 aspect-square w-full rounded-xl object-cover"
            />
            <button
              type="button"
              onClick={() => remove(item.id)}
              aria-label="移除这张自拍"
              className="bg-ink-950/80 hover:bg-danger-500 absolute top-1.5 right-1.5 grid size-7 place-items-center rounded-full text-sm transition-colors"
            >
              ×
            </button>
          </figure>
        ))}

        {!full && (
          <div className="border-ink-800 grid aspect-square place-items-center rounded-xl border border-dashed">
            <span className="text-ink-600 text-xs">
              {items.length === 0 ? '还没有照片' : `可再加 ${MAX_SELFIES - items.length} 张`}
            </span>
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3">
        {/* capture="user" 让手机直接开前置摄像头，省掉一次相册跳转 */}
        <input
          ref={cameraInput}
          type="file"
          accept="image/*"
          capture="user"
          hidden
          onChange={(event) => void addFiles(event.target.files)}
        />
        <input
          ref={libraryInput}
          type="file"
          accept="image/*"
          multiple
          hidden
          onChange={(event) => void addFiles(event.target.files)}
        />
        <button
          type="button"
          disabled={full || busy}
          onClick={() => cameraInput.current?.click()}
          className="bg-ink-900 hover:bg-ink-800 disabled:text-ink-600 rounded-xl px-4 py-3 text-sm font-medium transition-colors disabled:cursor-not-allowed"
        >
          拍一张
        </button>
        <button
          type="button"
          disabled={full || busy}
          onClick={() => libraryInput.current?.click()}
          className="bg-ink-900 hover:bg-ink-800 disabled:text-ink-600 rounded-xl px-4 py-3 text-sm font-medium transition-colors disabled:cursor-not-allowed"
        >
          从相册选
        </button>
      </div>

      {items.length === 1 && (
        <p className="text-ink-400 text-[13px]">
          再加一两张不同角度的自拍，能明显提高找到照片的概率。
        </p>
      )}

      {albumFilter}

      <button
        type="button"
        onClick={onSubmit}
        disabled={items.length === 0 || pending || busy}
        className="bg-accent-500 hover:bg-accent-600 disabled:bg-ink-800 disabled:text-ink-600 w-full rounded-xl px-4 py-3.5 font-medium transition-colors disabled:cursor-not-allowed"
      >
        {pending ? '正在检索…' : '开始检索'}
      </button>

      <PrivacyNotice />
    </section>
  )
}
