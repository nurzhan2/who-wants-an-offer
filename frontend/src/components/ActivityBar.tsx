/**
 * One line per long action, under the header on every page: running, done, or
 * failed and why. The server keeps the record, so a reload shows the same.
 * Finished lines can be hidden; hidden ids are remembered for this tab only.
 */

import { useEffect, useState } from 'react'

import { ACTIVITY_NAMES, elapsed, useActivity } from '@/hooks/useActivity'

/** Finished actions older than this are not shown at the top of the page. */
const SHOW_FINISHED_MS = 10 * 60 * 1000

export function ActivityBar() {
  const { data } = useActivity()
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [, tick] = useState(0)

  const running = data?.some((item) => item.state === 'running') ?? false
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => {
      tick((value) => value + 1)
    }, 1000)
    return () => {
      window.clearInterval(timer)
    }
  }, [running])

  const shown = (data ?? []).filter(
    (item) =>
      !hidden.has(item.id) &&
      (item.state === 'running' ||
        (item.finished_at !== null &&
          Date.now() - new Date(item.finished_at).getTime() < SHOW_FINISHED_MS)),
  )
  if (shown.length === 0) return null

  return (
    <div className="border-b border-hairline" role="status" aria-live="polite">
      <ul className="mx-auto max-w-shell space-y-1 px-4 py-3 text-small sm:px-6">
        {shown.slice(0, 4).map((item) => (
          <li key={item.id} className="flex flex-wrap items-baseline gap-x-2">
            <span className={item.state === 'failed' ? 'font-semibold' : undefined}>
              {item.state === 'running' ? '⏳ Идёт' : item.state === 'done' ? '✓ Готово' : '✕ Не удалось'}
              : {ACTIVITY_NAMES[item.kind]}
            </span>
            {item.label ? <span className="text-muted">«{item.label}»</span> : null}
            <span className="text-muted">· {elapsed(item.started_at, item.finished_at)}</span>
            {item.detail ? <span>— {item.detail}</span> : null}
            {item.state !== 'running' ? (
              <button
                type="button"
                className="ml-auto text-muted underline underline-offset-4"
                onClick={() => {
                  setHidden((set) => new Set(set).add(item.id))
                }}
              >
                скрыть
              </button>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  )
}
