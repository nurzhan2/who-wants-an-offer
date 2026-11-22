import type { ReactNode } from 'react'

import { href, ROUTES, type RouteName } from '@/app/routes'
import { ActivityBar } from '@/components/ActivityBar'
import { NoticeBanner } from '@/components/NoticeBanner'
import { useHealth } from '@/hooks/useHealth'
import { PARSE_STATUS } from '@/lib/labels'
import type { ProfileBrief } from '@/types/api'

/**
 * The frame: one column, 1078px, centred, and a row of screens across the top.
 *
 * The header carries the two facts that decide whether anything below it can be
 * trusted — whose resume this is drawn for, and whether the backend is
 * answering — because every number on every screen is a claim about one
 * database read through one API, and a page that renders stale figures with no
 * sign of the connection being gone is the failure mode a dashboard has.
 *
 * «Отклики» carries the one number that asks for the owner: how many written
 * letters wait for a confirmation. It is the next thing to do, wherever they are.
 */
export function Shell({
  route,
  profile,
  waiting,
  children,
}: {
  route: RouteName
  /** undefined: not known yet; null: known to be absent. */
  profile: ProfileBrief | null | undefined
  /** Applications with a letter, waiting for the owner's confirmation. */
  waiting?: number | undefined
  children: ReactNode
}) {
  return (
    <div className="min-h-screen bg-paper text-ink">
      <header className="border-b border-hairline">
        <div className="mx-auto flex max-w-shell flex-wrap items-baseline justify-between gap-x-4 gap-y-1 px-4 pt-6 sm:px-6">
          <a href={href('vacancies')} className="text-small font-semibold uppercase tracking-widest">
            Who wants an offer?
          </a>
          <Connection />
        </div>
        <nav className="mx-auto max-w-shell px-4 sm:px-6" aria-label="Разделы">
          <ul className="-mx-3 flex flex-wrap">
            {(Object.keys(ROUTES) as RouteName[]).map((name) => (
              <li key={name}>
                <a
                  href={href(name)}
                  aria-current={name === route ? 'page' : undefined}
                  className={`inline-flex items-baseline gap-1.5 border-b px-3 py-4 text-small transition-colors duration-800 ease-slow ${
                    name === route
                      ? 'border-ink font-semibold'
                      : 'border-transparent text-muted hover:text-ink'
                  }`}
                >
                  {ROUTES[name]}
                  {name === 'applications' && waiting ? (
                    <span
                      className="tnum rounded-pill bg-ink px-1.5 text-micro leading-4 text-paper"
                      aria-label={`ждут подтверждения: ${String(waiting)}`}
                    >
                      {waiting}
                    </span>
                  ) : null}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      <ActivityBar />
      <NoticeBanner />

      <main key={route} className="rise mx-auto max-w-shell px-4 py-section sm:px-6">
        <Owner profile={profile} />
        {children}
      </main>

      <footer className="mx-auto max-w-shell px-4 pb-section text-small text-muted sm:px-6">
        <p className="border-t border-hairline pt-6">
          Ничего не уходит без вашего подтверждения: отклик отправляет агент на вашем компьютере —
          ровно то, что вы подтвердили.
        </p>
      </footer>
    </div>
  )
}

/**
 * Whose resume everything below is scored against.
 *
 * Name and headline and nothing else. The resume holds contact details; they
 * belong on the screen that is about the resume, not in a header that every
 * other screen inherits — and nothing here is logged or measured anywhere.
 */
function Owner({ profile }: { profile: ProfileBrief | null | undefined }) {
  if (profile === undefined) {
    return null
  }
  if (profile === null) {
    return (
      <p className="mb-section text-small text-muted">
        Резюме ещё нет.{' '}
        <a href={href('profile')} className="text-ink underline underline-offset-4">
          Загрузите его
        </a>
        , чтобы вакансии получили оценку.
      </p>
    )
  }
  return (
    <div className="mb-8 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-small">
      <span className="font-semibold">{profile.name ?? 'Резюме без имени'}</span>
      {profile.headline ? <span className="text-muted">{profile.headline}</span> : null}
      <span className="text-muted">
        {profile.skills} навыков · {PARSE_STATUS[profile.parse_status]}
      </span>
    </div>
  )
}

/**
 * Whether the backend is answering.
 *
 * Encoded in the word and in the weight, not in a dot's colour: this system has
 * no chromatic values at all. The version and environment are for whoever runs
 * the server, so they sit in the tooltip rather than in the line.
 */
function Connection() {
  const { data, isPending, isError } = useHealth()

  if (isPending) return <span className="text-small text-muted">проверяем связь…</span>
  if (isError) return <span className="text-small font-semibold">сервер недоступен</span>

  return (
    <span className="text-small text-muted" title={`${data.version} · ${data.environment}`}>
      {data.status === 'ok' ? (
        'сервер на связи'
      ) : (
        <strong className="font-semibold">сервер работает с перебоями</strong>
      )}
    </span>
  )
}
