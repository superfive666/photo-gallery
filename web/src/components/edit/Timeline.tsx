import type { TimelineEvent } from '../../editApi'
import { describeEvent } from '../../lib/editEvents'

/** 聊天时间线：user 靠右、assistant/system 靠左，全部来自服务端事件流（断点恢复的根基）。 */
export function Timeline({ events }: { events: TimelineEvent[] }) {
  if (events.length === 0) {
    return <p className="text-ink-600 py-6 text-center text-sm">还没有内容</p>
  }
  return (
    <ol className="space-y-2">
      {events.map((event) => {
        const mine = event.actor === 'user'
        return (
          <li key={event.seq} className={`flex ${mine ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`max-w-[85%] rounded-xl px-4 py-2.5 text-sm leading-relaxed ${
                mine ? 'bg-ink-800' : 'bg-ink-900'
              }`}
            >
              <p>{describeEvent(event)}</p>
              <time className="text-ink-600 mt-1 block text-xs">
                {new Date(event.created_at).toLocaleString('zh-CN', {
                  month: 'numeric',
                  day: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                })}
              </time>
            </div>
          </li>
        )
      })}
    </ol>
  )
}
