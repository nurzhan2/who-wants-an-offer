import { useRef, useState } from 'react'

import { href } from '@/app/routes'
import { ApplyConfirm } from '@/components/ApplyConfirm'
import { Freshness } from '@/components/Freshness'
import { SendAll } from '@/components/SendAll'
import { Button, Card, Empty, Failure, Loading, More, NextStep, Pill, Section } from '@/components/ui'
import { useBoard } from '@/hooks/queries'
import { useOperations, useStartOperation } from '@/hooks/useOperations'
import { count, date, plainText, plural, score } from '@/lib/format'
import { APPLICATION_STATUS, OUTCOMES, outcomeLabel, STAGE_NOTES, STAGES } from '@/lib/labels'
import type { BoardCard, BoardColumn } from '@/types/api'

/**
 * Отклики: where every application is, on two axes.
 *
 * Where an application is in *this* project (queued, waiting for a person,
 * sent) and what hh has since said about it are different questions, and a sent
 * application has an answer to both — so there are two groups of lists and a
 * sent application appears in one of each. Collapsing them would either lose
 * the send or invent an outcome.
 *
 * The stages used to be four columns side by side. At 1078px that left each
 * card about 190px of text, and a reason or a status wrapped one word to a
 * line. They are full-width lists now, one under another, with a strip of
 * counts on top that jumps to each.
 *
 * **Statuses are encoded by position, label and weight — never by colour.**
 */
export function Applications() {
  const { data, isPending, isError, error, refetch } = useBoard()
  const groups = useRef<Record<string, HTMLElement | null>>({})

  if (isPending) return <Loading what="отклики" shape="board" />
  if (isError) {
    return (
      <Failure
        error={error}
        what="отклики"
        onRetry={() => {
          void refetch()
        }}
      />
    )
  }

  const nothing =
    data.stages.every((column) => column.cards.length === 0) && data.other.cards.length === 0

  return (
    <div className="rise">
      <Section
        title="Отклики"
        note="Письмо написано → вы подтверждаете → агент отправляет."
        more={
          <>
            <p>
              Отклик уходит только после вашего подтверждения: нажмите «Откликнуться…» у вакансии,
              прочитайте письмо и подтвердите. Отправляет агент на вашем компьютере.
            </p>
            <p>«Отправлено» — только то, что подтвердил сам hh.</p>
          </>
        }
        action={
          <div className="flex flex-wrap items-center gap-3">
            <SendAll />
            <RefreshOutcomes />
          </div>
        }
      >
        {nothing ? (
          <NextStep
            title="Откликов пока нет"
            action={
              <a
                href={href('vacancies')}
                className="rounded-pill border border-ink px-6 py-2 text-small transition-colors duration-800 ease-slow hover:bg-ink hover:text-paper"
              >
                К вакансиям
              </a>
            }
          >
            Откройте вакансию и нажмите «Написать письмо» — она появится здесь в «в очереди».
          </NextStep>
        ) : (
          <>
            <nav
              aria-label="Этапы откликов"
              className="mb-8 grid grid-cols-2 gap-px border border-hairline bg-hairline sm:grid-cols-4"
            >
              {data.stages.map((column) => (
                <button
                  key={column.key}
                  type="button"
                  className="bg-paper px-4 py-3 text-left transition-colors duration-800 ease-slow hover:bg-ink hover:text-paper"
                  onClick={() => {
                    groups.current[column.key]?.scrollIntoView({ block: 'start', behavior: 'smooth' })
                  }}
                >
                  <span className="tnum block text-heading font-light leading-none">
                    {count(column.cards.length)}
                  </span>
                  <span
                    className={`mt-2 block text-micro uppercase ${column.key === 'sent' ? 'font-semibold' : ''}`}
                  >
                    {STAGES[column.key] ?? column.key}
                  </span>
                </button>
              ))}
            </nav>
            <div className="space-y-10">
              {data.stages.map((column) => (
                <Group
                  key={column.key}
                  column={column}
                  label={STAGES[column.key] ?? column.key}
                  note={STAGE_NOTES[column.key]}
                  anchor={(node) => {
                    groups.current[column.key] = node
                  }}
                />
              ))}
              {data.other.cards.length > 0 ? (
                <Group
                  column={data.other}
                  label={STAGES.other ?? 'вне очереди'}
                  note={STAGE_NOTES.other}
                />
              ) : null}
            </div>
          </>
        )}
      </Section>

      <Section title="Ответы hh" note="Что hh сообщил по отправленным откликам — его словами.">
        {data.outcomes.length === 0 ? (
          <Empty>hh пока ничего не ответил ни по одному отклику.</Empty>
        ) : (
          <div className="space-y-10">
            {data.outcomes.map((column) => (
              <Group key={column.key} column={column} label={OUTCOMES[column.key] ?? column.key} />
            ))}
          </div>
        )}
      </Section>
    </div>
  )
}

/** The outcomes walk, from the screen where its result is read. */
function RefreshOutcomes() {
  const operations = useOperations()
  const start = useStartOperation()
  const busy = operations.data?.busy.includes('outcomes') ?? false
  return (
    <Button
      outline
      disabled={operations.data === undefined}
      busy={busy ? 'Ответы обновляются…' : start.isPending ? 'Запускаем…' : false}
      title="Агент откроет ваши отправленные отклики на hh и прочитает ответ"
      onClick={() => {
        start.mutate('outcomes')
      }}
    >
      Обновить ответы hh
    </Button>
  )
}

function Group({
  column,
  label,
  note,
  anchor,
}: {
  column: BoardColumn
  label: string
  note?: string | undefined
  anchor?: (node: HTMLElement | null) => void
}) {
  return (
    <section ref={anchor} className="scroll-mt-6" aria-label={label}>
      <div className="mb-4 flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-hairline pb-2">
        <h3 className={`text-small uppercase tracking-widest ${column.key === 'sent' ? 'font-semibold' : ''}`}>
          {label}
        </h3>
        <span className="tnum text-small text-muted">{count(column.cards.length)}</span>
        {note ? <span className="text-small text-muted">— {note}</span> : null}
      </div>
      {column.cards.length === 0 ? (
        <Empty>Пусто.</Empty>
      ) : (
        <div className="rise-list space-y-3">
          {column.cards.map((card) => (
            <ApplicationCard key={`${column.key}-${card.id}`} card={card} />
          ))}
        </div>
      )}
    </section>
  )
}

function ApplicationCard({ card }: { card: BoardCard }) {
  const [open, setOpen] = useState(false)
  const letter = card.sent_letter ?? card.cover_letter
  // Read off the row, not off the group it is being drawn in: the same
  // application appears in a stage group and in an outcome group, and a card
  // that decided "was this sent" from its surroundings said different things
  // about one row in two places on the same screen.
  const sent = card.sent_at !== null && card.send_confirmed
  const unconfirmed = card.sent_at !== null && !card.send_confirmed
  const claimsSend = card.agent_status === 'sent' && card.sent_at === null
  const archivedByAgent =
    card.agent_status === 'skipped' && (card.agent_reason ?? '').toLowerCase().includes('архив')
  const confirmable = card.sent_at === null && card.agent_status !== 'skipped' && letter !== null

  // One line of facts instead of a grid of labelled cells: a two-column grid
  // inside a narrow card is what broke «не отправлено» into three lines.
  const facts: string[] = []
  if (card.sent_at !== null) facts.push(`отправлено ${date(card.sent_at)}`)
  else facts.push(card.status === 'saved' ? 'не отправлено' : APPLICATION_STATUS[card.status])
  if (card.match_score !== null) facts.push(`оценка при отправке ${score(card.match_score)}`)
  if (card.hh_negotiations_total !== null) {
    facts.push(`откликов на вакансию по данным hh: ${count(card.hh_negotiations_total)}`)
  }

  return (
    <Card className="min-w-0 p-5 sm:p-6">
      <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
        <div className="min-w-0">
          <a href={href('vacancies', card.vacancy_id)} className="group block">
            <span className={`break-anywhere group-hover:underline ${sent ? 'font-semibold' : ''}`}>
              {card.title}
            </span>
            <span className="break-anywhere text-small text-muted">
              {' '}
              · {card.company ?? 'без компании'}
            </span>
          </a>
          <p className="mt-1 text-small">{facts.join(' · ')}</p>
          <div className="mt-2">
            <Freshness
              publishedAt={card.vacancy_published_at}
              lastSeenAt={card.vacancy_last_seen_at}
              active={card.vacancy_active}
              archivedByAgent={archivedByAgent}
            />
          </div>
        </div>
        {confirmable ? (
          <div className="flex flex-col items-start gap-2 sm:items-end">
            <ApplyConfirm vacancyId={card.vacancy_id} />
            {card.confirmed_at ? (
              <span className="text-small text-muted">
                подтверждено {date(card.confirmed_at)}, ждёт агента
              </span>
            ) : null}
          </div>
        ) : null}
      </div>

      {card.hh_last_state ? (
        <p className="mt-3 text-small">
          Ответ hh: <strong className="font-semibold">{outcomeLabel(card.hh_last_state)}</strong>
          {card.hh_last_state_at ? ` · ${date(card.hh_last_state_at)}` : ''}
        </p>
      ) : null}

      {card.agent_reason ? (
        <p className="break-words mt-4 max-w-3xl border-l border-ink pl-4 text-small">
          <span className="text-muted">Почему агент остановился: </span>
          {plainText(card.agent_reason)}
        </p>
      ) : null}

      {card.hh_warning ? (
        <p className="break-words mt-3 max-w-3xl text-small text-muted">
          hh при отправке: «{card.hh_warning}»
        </p>
      ) : null}
      {card.hh_blocking_warning ? (
        <p className="break-words mt-2 max-w-3xl text-small text-muted">
          hh про аккаунт: «{card.hh_blocking_warning}»
        </p>
      ) : null}

      {unconfirmed ? (
        <div className="mt-4 max-w-3xl">
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-small">
            <Pill strong>hh не подтвердил</Pill>
            <span>Не отправляйте повторно — нажмите «Обновить ответы hh».</span>
          </p>
          <More>
            <p>
              Агент отчитался об отправке {date(card.sent_at)}, но числа откликов от hh по этой
              вакансии в базе нет. Обновление ответов перечитает страницу вакансии.
            </p>
          </More>
        </div>
      ) : null}

      {claimsSend ? (
        <div className="mt-4 max-w-3xl">
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-small">
            <Pill strong>отправку не подтвердил</Pill>
            <span>Даты отправки нет — считать отклик отправленным нельзя.</span>
          </p>
          <More>
            <p>
              Агент дошёл до статуса «отправлено», но даты отправки не записал. Дату пишет только
              тот процесс, который печатал письмо.
            </p>
          </More>
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap items-baseline gap-x-6 gap-y-2 text-small">
        {letter ? (
          <button
            type="button"
            aria-expanded={open}
            className="underline underline-offset-4"
            onClick={() => {
              setOpen((value) => !value)
            }}
          >
            {open
              ? 'скрыть письмо'
              : `письмо (${count(letter.length)} ${plural(letter.length, 'знак', 'знака', 'знаков')})`}
          </button>
        ) : (
          <span className="text-muted">письма нет</span>
        )}
        {card.vacancy_key_skills !== null && card.vacancy_key_skills.length > 0 ? (
          <span className="break-words min-w-0 text-muted">
            вакансия просила: {card.vacancy_key_skills.join(', ')}
          </span>
        ) : null}
      </div>
      {open && letter ? (
        <>
          <p className="break-words mt-3 max-h-96 max-w-3xl overflow-y-auto whitespace-pre-wrap border-t border-hairline pt-4 text-small">
            {letter}
          </p>
          {card.sent_letter !== null &&
          card.cover_letter !== null &&
          card.sent_letter !== card.cover_letter ? (
            <p className="mt-3 text-small text-muted">
              Показан текст, который ушёл. Письмо потом перегенерировали — новая версия в базе
              отличается.
            </p>
          ) : null}
        </>
      ) : null}
    </Card>
  )
}
