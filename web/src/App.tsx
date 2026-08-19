import { useCallback, useEffect, useState } from 'react'

import {
  ApiError,
  type Album,
  checkSession,
  listAlbums,
  logout,
  search,
  searchByFace,
  type SearchResponse,
} from './api'
import { AlbumFilter } from './components/AlbumFilter'
import { BrowsePanel } from './components/BrowsePanel'
import { DiscardNotice } from './components/DiscardNotice'
import { EmptyState } from './components/EmptyState'
import { InviteGate } from './components/InviteGate'
import { Lightbox } from './components/Lightbox'
import { ResultGrid } from './components/ResultGrid'
import { SelfieUploader, type SelfieItem } from './components/SelfieUploader'
import { EditApp } from './EditApp'

type AuthState = 'checking' | 'anonymous' | 'authenticated'
// 两种检索方式：上传自拍，或浏览相册后点选照片上的脸
type Mode = 'selfie' | 'browse'

export function App() {
  const [auth, setAuth] = useState<AuthState>('checking')
  // 邀请码绑定的相册。非 null 时检索被后端硬性限制在这一个相册里，
  // 前端只是如实展示这个边界，不承担安全职责。
  const [scope, setScope] = useState<string | null>(null)
  // 剪辑码登录后走聊天窗 UI（一码一相册）；null = 查找码，走下面的检索 UI
  const [editAlbum, setEditAlbum] = useState<string | null>(null)
  const [mode, setMode] = useState<Mode>('selfie')
  const [albums, setAlbums] = useState<Album[]>([])
  const [album, setAlbum] = useState('')
  const [selfies, setSelfies] = useState<SelfieItem[]>([])
  const [result, setResult] = useState<SearchResponse | null>(null)
  // 「自拍已销毁」的确认只在自拍检索后展示 —— 按脸检索没有上传任何东西
  const [resultSource, setResultSource] = useState<Mode>('selfie')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null)

  const syncSession = useCallback(async () => {
    // 先问一次 /session/me，避免每次刷新都弹邀请码框；同时拿角色决定走哪套 UI
    const session = await checkSession()
    setScope(session.album)
    setEditAlbum(session.role === 'edit' && session.album ? session.album : null)
    setAuth(session.authenticated ? 'authenticated' : 'anonymous')
  }, [])

  useEffect(() => {
    void syncSession()
  }, [syncSession])

  useEffect(() => {
    if (auth !== 'authenticated' || editAlbum !== null) return
    // 相册列表拿不到不影响主流程（默认「所有活动」），所以失败静默
    void listAlbums()
      .then(setAlbums)
      .catch(() => setAlbums([]))
  }, [auth, editAlbum])

  const runSearch = useCallback(async () => {
    setPending(true)
    setError(null)
    try {
      // scoped session 不带 album 参数 —— 后端会强制限定到绑定相册，
      // 前端多传一个值只是徒增「不一致 → 403」的出错面
      const response = await search(
        selfies.map((item) => item.file),
        scope ? undefined : album || undefined,
      )
      setResult(response)
      setResultSource('selfie')
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setAuth('anonymous')
        return
      }
      setError(err instanceof ApiError ? err.message : '检索失败，请稍后再试')
    } finally {
      setPending(false)
    }
  }, [selfies, album, scope])

  const runFaceSearch = useCallback(async (faceId: string) => {
    setPending(true)
    setError(null)
    try {
      setResult(await searchByFace(faceId))
      setResultSource('browse')
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setAuth('anonymous')
        return
      }
      setError(err instanceof ApiError ? err.message : '检索失败，请稍后再试')
    } finally {
      setPending(false)
    }
  }, [])

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
    return (
      <InviteGate
        onAuthenticated={() => {
          // 重新问一次 /me：登录响应里有 album，但 role 决定走哪套 UI，统一以 /me 为准
          void syncSession()
        }}
      />
    )
  }

  if (editAlbum !== null) {
    return (
      <EditApp
        album={editAlbum}
        onLogout={() => void logout().then(() => void syncSession())}
      />
    )
  }

  const matches = result?.matches ?? []

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-16">
      <header className="flex items-baseline justify-between gap-3 py-5">
        <h1 className="text-lg font-semibold tracking-tight">找我的照片</h1>
        <button
          type="button"
          onClick={() =>
            void logout().then(() => {
              setScope(null)
              setAuth('anonymous')
            })
          }
          className="text-ink-600 hover:text-ink-200 text-xs transition-colors"
        >
          退出
        </button>
      </header>

      {/* 两种检索方式的切换。分段控件而不是 tab 条：只有两项，语义是「二选一」 */}
      <div
        role="tablist"
        aria-label="检索方式"
        className="bg-ink-900 mb-6 grid grid-cols-2 gap-1 rounded-xl p-1"
      >
        {(
          [
            ['selfie', '自拍搜索'],
            ['browse', '浏览相册'],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={mode === value}
            onClick={() => setMode(value)}
            className={`rounded-lg px-4 py-2.5 text-sm font-medium transition-colors ${
              mode === value ? 'bg-ink-800 text-ink-100' : 'text-ink-400 hover:text-ink-200'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {mode === 'selfie' ? (
        <SelfieUploader
          items={selfies}
          onChange={setSelfies}
          onSubmit={() => void runSearch()}
          pending={pending}
          albumFilter={
            <AlbumFilter
              albums={albums}
              value={album}
              onChange={setAlbum}
              disabled={pending}
              lockedAlbum={scope}
            />
          }
        />
      ) : (
        <div className="space-y-4">
          <AlbumFilter
            albums={albums}
            value={album}
            onChange={setAlbum}
            disabled={pending}
            lockedAlbum={scope}
          />
          <BrowsePanel
            album={scope ?? album}
            searching={pending}
            onSearchByFace={(faceId) => void runFaceSearch(faceId)}
          />
        </div>
      )}

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

          {/* 检索一结束就确认自拍已销毁，不管有没有结果。按脸检索没有上传，不展示 */}
          {resultSource === 'selfie' && <DiscardNotice confirmed={result.selfie_discarded} />}

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
