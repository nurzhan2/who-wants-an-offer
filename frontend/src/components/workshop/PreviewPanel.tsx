/**
 * «Сгенерировать пробное письмо по этим правилам» on any vacancy in the database.
 *
 * The refusal is the interesting half of this panel, not the letter. When the
 * hard rules cannot all be kept, the API answers 200 with `written: false` and
 * the list of what stopped it — that is the answer to "what do my rules
 * actually do", and it is rendered as an answer rather than as an error, in the
 * owner's own words, because those are the words they wrote the rule in.
 *
 * Nothing here is saved. The panel says so, because a person who thinks a trial
 * letter was stored will go looking for it in the apply queue.
 */
import { useState } from 'react'

import { Button } from '@/components/ui'
import type { Preview, VacancyChoice, Violation } from '@/types/workshop'

interface Props {
  vacancies: VacancyChoice[]
  query: string
  onQuery: (value: string) => void
  busy: boolean
  error: unknown
  preview: Preview | undefined
  onRun: (vacancyId: string) => void
}

function Violations({ title, tone, items }: { title: string; tone: 'red' | 'amber'; items: Violation[] }) {
  const tones = {
    red: 'border-ink',
    amber: 'border-hairline',
  }
  return (
    <div className={`border p-3 text-small ${tones[tone]}`}>
      <p className="font-semibold">{title}</p>
      <ul className="mt-2 space-y-1">
        {items.map((violation) => (
          <li key={`${violation.rule_id}-${violation.detail}`}>
            <span className="font-semibold">{violation.message}</span>
            <span className="block text-xs opacity-80">{violation.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

export function PreviewPanel({ vacancies, query, onQuery, busy, error, preview, onRun }: Props) {
  const [selected, setSelected] = useState('')
  const chosen = selected || vacancies[0]?.id || ''

  return (
    <section className="space-y-4 border border-hairline p-5 sm:p-6">
      <div>
        <h2 className="text-heading font-semibold">Пробное письмо</h2>
        <p className="mt-1 text-small text-muted">
          Проверьте правила и эталоны на любой вакансии. Ничего не сохраняется и никуда не
          уходит.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-[1fr_2fr]">
        <input
          className="w-full rounded-field border border-hairline bg-paper px-3 py-2 text-small text-ink outline-none transition-colors focus:border-ink"
          value={query}
          onChange={(event) => { onQuery(event.target.value); }}
          placeholder="Поиск вакансии"
        />
        <select
          className="w-full rounded-field border border-hairline bg-paper px-3 py-2 text-small text-ink outline-none transition-colors focus:border-ink"
          value={chosen}
          onChange={(event) => { setSelected(event.target.value); }}
        >
          {vacancies.length === 0 && <option value="">В базе пока нет вакансий</option>}
          {vacancies.map((vacancy) => (
            <option key={vacancy.id} value={vacancy.id}>
              {vacancy.title}
              {vacancy.company ? ` — ${vacancy.company}` : ''}
              {vacancy.city ? `, ${vacancy.city}` : ''}
            </option>
          ))}
        </select>
      </div>

      <div>
        <Button
          disabled={!chosen}
          busy={busy ? 'Пишем пробное письмо…' : false}
          title={chosen ? undefined : 'Выберите вакансию'}
          onClick={() => { onRun(chosen); }}
        >
          Сгенерировать пробное письмо
        </Button>
      </div>

      {error != null && (
        <p className="border border-ink p-3 text-small font-semibold">
          {error instanceof Error ? error.message : 'Не удалось сгенерировать письмо'}
        </p>
      )}

      {preview && (
        <div className="space-y-3">
          <p className="text-xs text-muted">
            Правил применено: {preview.rules_applied} · эталонов показано:{' '}
            {preview.references_used} · прошлых писем в примерах: {preview.examples_used}
            {preview.written && ` · попыток: ${String(preview.attempts)}`}
            {preview.written &&
              (preview.source === 'fallback'
                ? ' · текст собран по шаблону, без модели'
                : ' · текст написан моделью')}
          </p>

          {preview.broken_rules.length > 0 && (
            <Violations
              title="Письмо не написано: нарушены жёсткие правила"
              tone="red"
              items={preview.broken_rules}
            />
          )}

          {!preview.written && preview.detail && (
            <p className="text-xs text-muted">{preview.detail}</p>
          )}

          {preview.warnings.length > 0 && (
            <Violations
              title="Мягкие правила, которые письмо всё равно нарушает"
              tone="amber"
              items={preview.warnings}
            />
          )}

          {preview.written && preview.text && (
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap border border-hairline p-4 font-sans text-small text-ink">
              {preview.text}
            </pre>
          )}
        </div>
      )}
    </section>
  )
}
