import { useCallback, useEffect, useState } from 'react'

import { ApiError, type Captcha, fetchCaptcha, login } from '../api'
import { ConsentCheckbox, PrivacyNotice } from './PrivacyNotice'

/**
 * 邀请码门。
 *
 * 站点公开开放意味着任何人拿一张他人照片就能扒出该人的全部活动照片 ——
 * 这道门不是形式主义。见 docs/privacy.md。
 *
 * 验证码挡的是脚本批量试码，不是真人 —— 所以只有 4 个字符、不区分大小写、
 * 答错可以对同一张图重试（token 只有验证通过才作废）。
 */
export function InviteGate({
  onAuthenticated,
}: {
  onAuthenticated: (album: string | null) => void
}) {
  const [code, setCode] = useState('')
  const [consent, setConsent] = useState(false)
  const [captcha, setCaptcha] = useState<Captcha | null>(null)
  const [answer, setAnswer] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  const refreshCaptcha = useCallback(async () => {
    setAnswer('')
    try {
      setCaptcha(await fetchCaptcha())
    } catch {
      // 图片拿不到时不阻塞输入框渲染，提交时会再给出明确错误
      setCaptcha(null)
    }
  }, [])

  useEffect(() => {
    void refreshCaptcha()
  }, [refreshCaptcha])

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!captcha) {
      setError('验证码加载失败，请点击「换一张」重试')
      return
    }
    setError(null)
    setPending(true)
    try {
      const session = await login(code.trim(), consent, captcha.token, answer.trim())
      onAuthenticated(session.album)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '登录失败，请稍后再试')
      // 验证码验证通过即作废（防重放），失败原因不管是码错还是答案错，
      // 都换一张新图最稳妥 —— 免得用户对着一个可能已失效的 token 反复碰壁
      void refreshCaptcha()
    } finally {
      setPending(false)
    }
  }

  const canSubmit = code.trim().length > 0 && answer.trim().length > 0 && consent && !pending

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

        <div className="space-y-2">
          <label htmlFor="captcha-answer" className="block text-sm font-medium">
            验证码
          </label>
          <div className="flex items-stretch gap-3">
            {captcha ? (
              <img
                src={`data:image/svg+xml;utf8,${encodeURIComponent(captcha.svg)}`}
                alt="验证码图片，看不清请点击右侧「换一张」"
                className="border-ink-800 h-[52px] w-36 shrink-0 rounded-xl border object-cover"
              />
            ) : (
              <div className="bg-ink-900 border-ink-800 text-ink-600 grid h-[52px] w-36 shrink-0 place-items-center rounded-xl border text-xs">
                加载中…
              </div>
            )}
            <input
              id="captcha-answer"
              type="text"
              value={answer}
              onChange={(event) => setAnswer(event.target.value)}
              autoComplete="off"
              inputMode="text"
              maxLength={8}
              className="bg-ink-900 border-ink-800 focus:border-accent-500 w-full min-w-0 rounded-xl border px-4 py-3 text-base outline-none transition-colors"
              placeholder="输入图中字符"
              // 服务端不区分大小写，也别让手机自作主张改写
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
            />
          </div>
          <button
            type="button"
            onClick={() => void refreshCaptcha()}
            className="text-ink-600 hover:text-ink-200 -m-2 p-2 text-xs transition-colors"
          >
            看不清？换一张
          </button>
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
