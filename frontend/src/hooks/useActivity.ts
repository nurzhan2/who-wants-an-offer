/**
 * The server's record of long actions — letters, CVs, resume parses — so a
 * reloaded page knows what is still running and how the rest ended.
 *
 * Polls every two seconds while something runs and every fifteen otherwise.
 * When an action finishes, every query is refreshed: the letter, the CV
 * versions, the resume card all changed on the server, and the page was
 * showing the state from before.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'

import { apiGet } from '@/api/client'

export type ActivityKind = 'letter' | 'cv' | 'cover_letter' | 'resume'

export interface Activity {
  id: string
  kind: ActivityKind
  key: string
  label: string
  state: 'running' | 'done' | 'failed'
  started_at: string
  finished_at: string | null
  detail: string | null
}

export function useActivity() {
  const client = useQueryClient()
  const query = useQuery({
    queryKey: ['activity'],
    queryFn: () => apiGet<Activity[]>('/api/v1/activity'),
    refetchInterval: (current) =>
      current.state.data?.some((item) => item.state === 'running') ? 2_000 : 15_000,
  })
  const running = useRef<Set<string>>(new Set())

  useEffect(() => {
    const items = query.data ?? []
    const now = new Set(items.filter((item) => item.state === 'running').map((item) => item.id))
    const finished = [...running.current].some((id) => !now.has(id))
    running.current = now
    if (finished) {
      void client.invalidateQueries({ predicate: (q) => q.queryKey[0] !== 'activity' })
    }
  }, [query.data, client])

  return query
}

/** The newest action of these kinds about this thing, if any. */
export function latestFor(
  items: Activity[] | undefined,
  kinds: ActivityKind[],
  key: string,
): Activity | undefined {
  return items?.find((item) => item.key === key && kinds.includes(item.kind))
}

export const ACTIVITY_NAMES: Record<ActivityKind, string> = {
  letter: 'письмо',
  cover_letter: 'письмо',
  cv: 'CV',
  resume: 'разбор резюме',
}

export function elapsed(from: string, to?: string | null): string {
  const seconds = Math.max(
    0,
    Math.round(((to ? new Date(to).getTime() : Date.now()) - new Date(from).getTime()) / 1000),
  )
  const minutes = Math.floor(seconds / 60)
  return `${String(minutes)}:${String(seconds % 60).padStart(2, '0')}`
}
