/**
 * The Mini App, as the owner drew it (22 Nov 2026):
 *
 *   statistics → a day → its tracks → one vacancy at a time
 *   (where to apply, the letter, the CV; ◀ back, forward ▶, «Списком»)
 *   ↔ the same vacancies as a list («Показывать по одному»).
 *
 * Telegram's own back button goes one level up. Everything the screen shows
 * comes from /api/v1/miniapp, which only answers the owner.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

import { api, type Brief, type Day, type Detail, type Filter, type Stats } from './api'
import { notify, open, tap, tg } from './telegram'

type Screen =
  | { kind: 'home' }
  | { kind: 'day'; day: string }
  | {
      kind: 'track'
      day: string
      slug: string
      mode: 'one' | 'list'
      index: number
    }

const MONTHS = [
  'января',
  'февраля',
  'марта',
  'апреля',
  'мая',
  'июня',
  'июля',
  'августа',
  'сентября',
  'октября',
  'ноября',
  'декабря',
]

function dayLabel(iso: string): string {
  const [year = 1970, month = 1, day = 1] = iso.split('-').map(Number)
  const today = new Date()
  const label = `${String(day)} ${MONTHS[month - 1] ?? ''}`
  const date = new Date(year, month - 1, day)
  const diff = Math.round(
    (new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime() - date.getTime()) /
      86_400_000,
  )
  if (diff === 0) return `Сегодня, ${label}`
  if (diff === 1) return `Вчера, ${label}`
  return label
}

function ago(iso: string | null, known: boolean): string {
  if (!iso) return 'дата неизвестна'
  const hours = Math.floor((Date.now() - new Date(iso).getTime()) / 3_600_000)
  const text =
    hours < 1
      ? 'только что'
      : hours < 48
        ? `${String(hours)} ч назад`
        : `${String(Math.floor(hours / 24))} дн назад`
  return known ? `опубликована ${text}` : `найдена ${text}`
}

const SOURCE_NAMES: Record<string, string> = {
  hh: 'hh',
  telegram: 'Telegram',
  email_alerts: 'LinkedIn / почта',
}

export function App() {
  const [filter, setFilterState] = useState<Filter>(loadFilter)
  const setFilter = useCallback((next: Filter) => {
    setFilterState(next)
    try {
      window.localStorage.setItem(FILTER_KEY, JSON.stringify(next))
    } catch {
      // storage unavailable: the choice lasts this session only
    }
  }, [])
  const value = useMemo(() => ({ filter, setFilter }), [filter, setFilter])
  return (
    <FilterContext.Provider value={value}>
      <Screens />
    </FilterContext.Provider>
  )
}

function Screens() {
  const [screen, setScreen] = useState<Screen>({ kind: 'home' })

  const up = useCallback(() => {
    setScreen((current) => {
      if (current.kind === 'track') return { kind: 'day', day: current.day }
      return { kind: 'home' }
    })
  }, [])

  useEffect(() => {
    const app = tg()
    if (!app) return
    if (screen.kind === 'home') app.BackButton.hide()
    else app.BackButton.show()
    app.BackButton.onClick(up)
    return () => {
      app.BackButton.offClick(up)
    }
  }, [screen.kind, up])

  if (screen.kind === 'home') {
    return (
      <HomeScreen
        onDay={(day) => {
          setScreen({ kind: 'day', day })
        }}
      />
    )
  }
  if (screen.kind === 'day') {
    return (
      <DayScreen
        day={screen.day}
        onTrack={(slug) => {
          setScreen({
            kind: 'track',
            day: screen.day,
            slug,
            mode: 'one',
            index: 0,
          })
        }}
      />
    )
  }
  return <TrackScreen screen={screen} onChange={setScreen} />
}

const SCORES = [60, 70, 80] as const
const FILTER_KEY = 'miniapp-filter'
const DEFAULT_FILTER: Filter = { minScore: 60, withHh: false }

function loadFilter(): Filter {
  try {
    const raw = window.localStorage.getItem(FILTER_KEY)
    return raw ? { ...DEFAULT_FILTER, ...(JSON.parse(raw) as Partial<Filter>) } : DEFAULT_FILTER
  } catch {
    return DEFAULT_FILTER
  }
}

const FilterContext = createContext<{ filter: Filter; setFilter: (f: Filter) => void }>({
  filter: DEFAULT_FILTER,
  setFilter: () => undefined,
})

function useFilter() {
  return useContext(FilterContext)
}

function useHome() {
  const { filter } = useFilter()
  return useQuery({
    queryKey: ['home', filter],
    queryFn: () => api.home(filter),
    staleTime: 60_000,
  })
}

function FilterBar() {
  const { filter, setFilter } = useFilter()
  return (
    <div className="filters">
      <span className="muted small">Оценка от</span>
      {SCORES.map((score) => (
        <button
          key={score}
          className={filter.minScore === score ? 'chip active' : 'chip'}
          onClick={() => {
            setFilter({ ...filter, minScore: score })
          }}
        >
          {score}
        </button>
      ))}
      <button
        className={filter.withHh ? 'chip active' : 'chip'}
        onClick={() => {
          setFilter({ ...filter, withHh: !filter.withHh })
        }}
      >
        {filter.withHh ? 'hh: показан' : 'hh: скрыт'}
      </button>
    </div>
  )
}

function HomeScreen({ onDay }: { onDay: (day: string) => void }) {
  const home = useHome()
  if (home.isLoading) return <Loading />
  if (home.error) return <Failure error={home.error} />
  const data = home.data
  if (!data) return null
  return (
    <main className="page">
      <h1>Offer Digest</h1>
      <StatsBlock stats={data.stats} />
      <h2>Вакансии по дням</h2>
      <FilterBar />
      {data.days.length === 0 ? (
        <p className="muted">За две недели подходящих вакансий нет. Снизьте порог оценки.</p>
      ) : (
        <div className="list">
          {data.days.map((day) => (
            <button
              key={day.date}
              className="row"
              onClick={() => {
                tap()
                onDay(day.date)
              }}
            >
              <span className="row-title">{dayLabel(day.date)}</span>
              <span className="row-meta">{day.count}</span>
            </button>
          ))}
        </div>
      )}
    </main>
  )
}

function StatsBlock({ stats }: { stats: Stats }) {
  const tracks = Object.entries(stats.tracks_week).sort((a, b) => b[1] - a[1])
  return (
    <section className="stats">
      <div className="tile">
        <b>{stats.matched_today}</b>
        <span>подходят сегодня</span>
      </div>
      <div className="tile">
        <b>{stats.matched_week}</b>
        <span>за неделю</span>
      </div>
      <div className="tile">
        <b>{stats.found_today}</b>
        <span>найдено за сутки</span>
      </div>
      <div className="tile">
        <b>
          {stats.outreach_today}/{stats.outreach_limit}
        </b>
        <span>автооткликов в TG сегодня</span>
      </div>
      <div className="tile">
        <b>{stats.letters_today}</b>
        <span>писем сегодня</span>
      </div>
      <div className="tile">
        <b>${stats.spent_usd_today.toFixed(2)}</b>
        <span>из ${stats.budget_usd} на модель</span>
      </div>
      {tracks.length > 0 && (
        <div className="tracks-week">
          {tracks.map(([slug, count]) => (
            <span key={slug} className="chip">
              {slug} · {count}
            </span>
          ))}
        </div>
      )}
    </section>
  )
}

function DayScreen({ day, onTrack }: { day: string; onTrack: (slug: string) => void }) {
  const home = useHome()
  const found: Day | undefined = home.data?.days.find((item) => item.date === day)
  if (home.isLoading) return <Loading />
  const tracks = Object.entries(found?.tracks ?? {}).sort((a, b) => b[1] - a[1])
  return (
    <main className="page">
      <h1>{dayLabel(day)}</h1>
      <p className="muted">Выберите трек</p>
      <div className="list">
        {tracks.map(([slug, count]) => (
          <button
            key={slug}
            className="row"
            onClick={() => {
              tap()
              onTrack(slug)
            }}
          >
            <span className="row-title">{slug}</span>
            <span className="row-meta">{count}</span>
          </button>
        ))}
      </div>
    </main>
  )
}

type TrackState = Extract<Screen, { kind: 'track' }>

function TrackScreen({ screen, onChange }: { screen: TrackState; onChange: (s: Screen) => void }) {
  const { filter } = useFilter()
  const list = useQuery({
    queryKey: ['day-track', screen.day, screen.slug, filter],
    queryFn: () => api.dayTrack(screen.day, screen.slug, filter),
    staleTime: 60_000,
  })
  if (list.isLoading) return <Loading />
  if (list.error) return <Failure error={list.error} />
  const items = list.data ?? []
  if (items.length === 0) {
    return (
      <main className="page">
        <h1>{screen.slug}</h1>
        <p className="muted">Вакансий нет.</p>
      </main>
    )
  }
  const title = `${screen.slug} · ${dayLabel(screen.day)}`
  if (screen.mode === 'list') {
    return (
      <main className="page with-bar">
        <h1>{title}</h1>
        <div className="list">
          {items.map((item, index) => (
            <button
              key={item.vacancy_id}
              className="row"
              onClick={() => {
                tap()
                onChange({ ...screen, mode: 'one', index })
              }}
            >
              <span className="row-title">
                {item.title}
                {item.company && <span className="muted"> — {item.company}</span>}
              </span>
              <span className="row-meta">⭐{Math.round(item.score)}</span>
            </button>
          ))}
        </div>
        <nav className="bar">
          <button
            className="primary wide"
            onClick={() => {
              onChange({ ...screen, mode: 'one' })
            }}
          >
            Показывать по одному
          </button>
        </nav>
      </main>
    )
  }
  const index = Math.min(screen.index, items.length - 1)
  const current = items[index]
  return (
    <main className="page with-bar">
      <p className="muted small">
        {title} · {index + 1} из {items.length}
      </p>
      {current && <VacancyCard brief={current} />}
      <nav className="bar">
        <button
          disabled={index === 0}
          onClick={() => {
            tap()
            onChange({ ...screen, index: index - 1 })
          }}
        >
          ◀ Назад
        </button>
        <button
          onClick={() => {
            onChange({ ...screen, mode: 'list' })
          }}
        >
          Списком
        </button>
        <button
          disabled={index >= items.length - 1}
          onClick={() => {
            tap()
            onChange({ ...screen, index: index + 1 })
          }}
        >
          Вперёд ▶
        </button>
      </nav>
    </main>
  )
}

function VacancyCard({ brief }: { brief: Brief }) {
  const client = useQueryClient()
  const detail = useQuery({
    queryKey: ['vacancy', brief.vacancy_id],
    queryFn: () => api.vacancy(brief.vacancy_id),
  })
  const [busy, setBusy] = useState<'letter' | 'cv' | null>(null)
  const data: Detail | undefined = detail.data

  const writeLetter = async () => {
    setBusy('letter')
    try {
      const { letter } = await api.writeLetter(brief.vacancy_id)
      client.setQueryData<Detail>(['vacancy', brief.vacancy_id], (old) =>
        old ? { ...old, letter } : old,
      )
      if (data?.is_hh)
        notify('Письмо написано. Вакансия в очереди агента hh — подтвердите на дашборде.')
    } catch (error) {
      notify((error as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const sendCv = async () => {
    setBusy('cv')
    try {
      await api.sendCv(brief.vacancy_id)
      notify('Резюме отправлено в чат с ботом — его можно переслать.')
    } catch (error) {
      notify((error as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      notify('Письмо скопировано')
    } catch {
      notify('Не удалось скопировать: выделите текст и скопируйте вручную')
    }
  }

  const contact = data?.contact?.replace(/^@/, '')
  const where = useMemo(() => {
    if (!data) return null
    if (data.source === 'telegram' && contact)
      return { label: `Написать @${contact}`, url: `https://t.me/${contact}` }
    return {
      label: data.is_hh ? 'Открыть на hh' : 'Открыть вакансию',
      url: data.url,
    }
  }, [data, contact])

  return (
    <article className="card">
      <h1 className="card-title">{brief.title}</h1>
      {brief.company && <p className="company">{brief.company}</p>}
      <p className="muted small">
        ⭐ {Math.round(brief.score)} · резюме «{brief.slug}» ·{' '}
        {SOURCE_NAMES[brief.source] ?? brief.source} ·{' '}
        {ago(data?.published ?? brief.published, data?.known_date ?? brief.known_date)}
      </p>

      <section className="block">
        <h2>Куда откликаться</h2>
        {where && (
          <button
            className="primary wide"
            onClick={() => {
              open(where.url)
            }}
          >
            {where.label}
          </button>
        )}
        {data?.source === 'telegram' && contact && (
          <p className="muted small">Отправьте @{contact} письмо и файл резюме ниже.</p>
        )}
        {data?.is_hh && (
          <p className="muted small">
            На hh откликается агент на ноутбуке: напишите письмо — вакансия встанет в его очередь.
          </p>
        )}
      </section>

      <section className="block">
        <h2>Сопроводительное</h2>
        {detail.isLoading ? (
          <p className="muted">Загрузка…</p>
        ) : data?.letter ? (
          <>
            <pre className="letter">{data.letter}</pre>
            <button className="wide" onClick={() => void copy(data.letter ?? '')}>
              Скопировать письмо
            </button>
          </>
        ) : (
          <button className="wide" disabled={busy !== null} onClick={() => void writeLetter()}>
            {busy === 'letter'
              ? 'Пишу… (до минуты)'
              : data?.is_hh
                ? 'Написать письмо и в очередь hh'
                : 'Написать письмо'}
          </button>
        )}
      </section>

      <section className="block">
        <h2>Резюме</h2>
        {data?.cv_filename ? (
          <>
            <p className="small">📄 {data.cv_filename}</p>
            <button className="wide" disabled={busy !== null} onClick={() => void sendCv()}>
              {busy === 'cv' ? 'Отправляю…' : 'Прислать файл в чат'}
            </button>
          </>
        ) : (
          <p className="muted small">
            Файла нет: на дашборде нажмите «Заменить файл» у резюме «{brief.slug}».
          </p>
        )}
      </section>
    </article>
  )
}

function Loading() {
  return (
    <main className="page">
      <p className="muted">Загрузка…</p>
    </main>
  )
}

function Failure({ error }: { error: unknown }) {
  return (
    <main className="page">
      <p className="error">{(error as Error).message}</p>
    </main>
  )
}
