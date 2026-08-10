import { useState } from 'react'

import { ApiError, login } from '../api'
import { ConsentCheckbox, PrivacyNotice } from './PrivacyNotice'

/**
 * 邀请码门。
 *
 * 站点公开开放意味着任何人拿一张他人照片就能扒出该人的全部活动照片 ——
 * 这道门不是形式主义。见 docs/privacy.md。
 */
export function InviteGate({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [code, setCode] = useState('')
  const [consent, setConsent] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setPending(true)
    try {
      await login(code.trim(), consent)
      onAuthenticated()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '登录失败，请稍后再试')
    } finally {
      setPending(false)
    }
  }

  const canSubmit = code.trim().length > 0 && consent && !pending

  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-md flex-col justify-center gap-8 px-6 py-12">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">找我的照片</h1>
        <p className="text-ink-400 text-sm">上传一张自拍，在历史活动相册里找到有你的照片。</p>
      </header>

      <form onSubmit={(event) => void submit(event)} className="space-y-5" noValidate>
        <div className="space-y-2">
          <label htmlFor="invite" className="block text-sm font-medium">
            邀请码
          </label>
          <input
            id="invite"
            type="password"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            autoComplete="one-time-code"
            className="bg-ink-900 border-ink-800 focus:border-accent-500 w-full rounded-xl border px-4 py-3 text-base outline-none transition-colors"
            placeholder="向管理员索取"
            // 移动端别自动大写/纠错，邀请码是区分大小写的
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
          />
        </div>

        <div className="bg-ink-900/60 border-ink-800 space-y-3 rounded-xl border p-4">
          <PrivacyNotice />
          <ConsentCheckbox checked={consent} onChange={setConsent} />
        </div>

        {error && (
          <p role="alert" className="text-danger-500 text-sm">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={!canSubmit}
          className="bg-accent-500 hover:bg-accent-600 disabled:bg-ink-800 disabled:text-ink-600 w-full rounded-xl px-4 py-3 font-medium transition-colors disabled:cursor-not-allowed"
        >
          {pending ? '验证中…' : '进入'}
        </button>
      </form>
    </main>
  )
}
