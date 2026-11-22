/**
 * The Mini App's calls. Each carries Telegram's signed initData; the server
 * checks the signature and that the caller is the owner (no dashboard password
 * inside Telegram).
 */

import { tg } from './telegram'

export interface Stats {
  found_today: number
  found_week: number
  matched_today: number
  matched_week: number
  tracks_week: Record<string, number>
  outreach_today: number
  outreach_limit: number
  letters_today: number
  spent_usd_today: number
  budget_usd: number
}

export interface Day {
  date: string
  count: number
  tracks: Record<string, number>
}

export interface Home {
  stats: Stats
  days: Day[]
}

export interface Brief {
  vacancy_id: string
  slug: string
  score: number
  title: string
  company: string | null
  source: string
  url: string
  published: string
  known_date: boolean
}

export interface Detail extends Omit<Brief, 'published'> {
  published: string | null
  contact: string | null
  channel: string | null
  is_hh: boolean
  letter: string | null
  cv_filename: string | null
}

async function call<T>(path: string, method: 'GET' | 'POST' = 'GET'): Promise<T> {
  const response = await fetch(`/api/v1/miniapp${path}`, {
    method,
    headers: { 'X-Telegram-Init-Data': tg()?.initData ?? '' },
  })
  if (!response.ok) {
    let detail = `Ошибка ${String(response.status)}`
    try {
      const body = (await response.json()) as { detail?: string }
      if (body.detail) detail = body.detail
    } catch {
      // not JSON: keep the status line
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export interface Filter {
  minScore: number
  withHh: boolean
}

function query(filter: Filter): string {
  return `?min_score=${String(filter.minScore)}&with_hh=${String(filter.withHh)}`
}

export const api = {
  home: (filter: Filter) => call<Home>(`/home${query(filter)}`),
  dayTrack: (day: string, slug: string, filter: Filter) =>
    call<Brief[]>(`/days/${day}/${encodeURIComponent(slug)}${query(filter)}`),
  vacancy: (id: string) => call<Detail>(`/vacancies/${id}`),
  writeLetter: (id: string) => call<{ letter: string }>(`/vacancies/${id}/letter`, 'POST'),
  sendCv: (id: string) => call<{ sent: boolean }>(`/vacancies/${id}/cv`, 'POST'),
}
