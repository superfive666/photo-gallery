import { useCallback, useEffect, useState } from 'react'

import {
  ApiError,
  type Album,
  checkSession,
  listAlbums,
  logout,
  search,
  type SearchResponse,
} from './api'
import { AlbumFilter } from './components/AlbumFilter'
import { DiscardNotice } from './components/DiscardNotice'
import { EmptyState } from './components/EmptyState'
import { InviteGate } from './components/InviteGate'
import { Lightbox } from './components/Lightbox'
import { ResultGrid } from './components/ResultGrid'
import { SelfieUploader, type SelfieItem } from './components/SelfieUploader'

type AuthState = 'checking' | 'anonymous' | 'authenticated'

export function App() {
  const [auth, setAuth] = useState<AuthState>('checking')
  const [albums, setAlbums] = useState<Album[]>([])
  const [album, setAlbum] = useState('')
  const [selfies, setSelfies] = useState<SelfieItem[]>([])
  const [result, setResult] = useState<SearchResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null)

  useEffect(() => {
    // 先问一次 /session/me，避免每次刷新都弹邀请码框
    void checkSession().then((ok) => setAuth(ok ? 'authenticated' : 'anonymous'))
  }, [])

  useEffect(() => {
    if (auth !== 'authenticated') return
    // 相册列表拿不到不影响主流程（默认「所有活动」），所以失败静默
    void listAlbums()
      .then(setAlbums)
      .catch(() => setAlbums([]))
  }, [auth])

  const runSearch = useCallback(async () => {
    setPending(true)
    setError(null)
    try {
      const response = await search(
        selfies.map((item) => item.file),
        album || undefined,
      )
      setResult(response)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setAuth('anonymous')
        return
      }
      setError(err instanceof ApiError ? err.message : '检索失败，请稍后再试')
    } finally {
      setPending(false)
    }
  }, [selfies, album])

  function reset() {
    for (const item of selfies) URL.revokeObjectURL(item.previewUrl)
    setSelfies([])
    setResult(null)
    setError(null)
  }

  if (auth === 'checking') {
    return (
      <div className="grid min-h-dvh place-items-center">
        <p className="text-ink-600 text-sm">加载中…</p>
      </div>
    )
  }

  if (auth === 'anonymous') {
    return <InviteGate onAuthenticated={() => setAuth('authenticated')} />
  }

  const matches = result?.matches ?? []

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-16">
      <header className="flex items-baseline justify-between gap-3 py-5">
        <h1 className="text-lg font-semibold tracking-tight">找我的照片</h1>
        <button
          type="button"
          onClick={() => void logout().then(() => setAuth('anonymous'))}
          className="text-ink-600 hover:text-ink-200 text-xs transition-colors"
        >
          退出
        </button>
      </header>

      <SelfieUploader
        items={selfies}
        onChange={setSelfies}
        onSubmit={() => void runSearch()}
        pending={pending}
        albumFilter={
          <AlbumFilter albums={albums} value={album} onChange={setAlbum} disabled={pending} />
        }
      />

      {error && (
        <p role="alert" className="text-danger-500 mt-5 text-sm">
          {error}
        </p>
      )}

      {result && (
        <div className="mt-10 space-y-6">
          <div className="border-ink-800 flex items-baseline justify-between gap-3 border-t pt-5">
            <p className="text-sm">
              {matches.length > 0 ? (
                <>
                  找到 <strong className="font-semibold">{matches.length}</strong> 张照片
                </>
              ) : (
                '检索完成'
              )}
            </p>
            <button
              type="button"
              onClick={reset}
              className="text-ink-600 hover:text-ink-200 text-xs transition-colors"
            >
              重新开始
            </button>
          </div>

          {/* 检索一结束就确认自拍已销毁，不管有没有结果 */}
          <DiscardNotice confirmed={result.selfie_discarded} />

          {matches.length > 0 ? (
            <ResultGrid matches={matches} onOpen={setLightboxIndex} />
          ) : (
            <EmptyState status={result.status} message={result.message} />
          )}
        </div>
      )}

      {lightboxIndex !== null && (
        <Lightbox
          matches={matches}
          index={lightboxIndex}
          onClose={() => setLightboxIndex(null)}
          onNavigate={setLightboxIndex}
        />
      )}
    </div>
  )
}
