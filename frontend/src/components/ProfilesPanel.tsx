import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'

import { ApiError, apiSend, apiUpload } from '@/api/client'
import { Button, Pill, Section } from '@/components/ui'
import { elapsed, latestFor, useActivity } from '@/hooks/useActivity'
import { useFlash } from '@/hooks/useMotion'
import { useProfiles } from '@/hooks/useProfiles'
import { explain } from '@/lib/errors'
import type { ParseStatus, Profile } from '@/types/api'

/**
 * Резюме по трекам — several resumes live at once (phase 22).
 *
 * Every vacancy is scored against each active resume and is worked from the one
 * it scores best with, so this panel is where the owner keeps the set: which
 * are on, what each is called, which wins a tie, and which hh resume each one
 * is — the id the agent must pick in hh's form. A resume without that id still
 * scores; the vacancies it wins go to «посмотреть руками».
 *
 * It is also the one place a resume is uploaded: several files at once, each
 * with its own slug and track. An upload never switches the others off;
 * «Заменить файл» replaces one resume with a new version of itself once the new
 * file has parsed.
 *
 * Each resume is its own card with its own save: a refusal (a slug another
 * resume already holds, 409) is said under the card it belongs to, and the
 * button is only live while there is something to save.
 */

const ACCEPT = '.pdf,.docx,.txt,.md'
const HH_ID = /^[0-9a-f]{38}$/
const SLUG = /^[a-z0-9][a-z0-9_-]{0,49}$/

const INPUT =
  'w-full min-w-0 rounded-field border border-hairline bg-paper px-3 py-2 text-ink outline-none transition-colors placeholder:text-muted/60 focus:border-ink aria-[invalid=true]:border-ink'

interface Accepted {
  profile_id: string
  parse_status: ParseStatus
  slug: string | null
  filename: string | null
}

interface Staged {
  file: File
  slug: string
  track: string
}

function slugFrom(name: string): string {
  const base = name
    .replace(/\.[^.]+$/, '')
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return SLUG.test(base) ? base.slice(0, 50) : ''
}

/** Field paths as the API names them, in this panel's words. */
const FIELD_NAMES: Record<string, string> = {
  slug: 'метка',
  track: 'трек',
  priority: 'приоритет',
  hh_resume_id: 'hh-id',
  is_active: 'включено',
}

/** A failed save, as a sentence about this card. */
function saveError(error: unknown, slug: string): string {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      return slug
        ? `Метка «${slug}» уже занята другим резюме. Выберите другую.`
        : (error.detail ?? 'Такая метка уже есть у другого резюме.')
    }
    if (error.status === 422 && error.fields !== undefined) {
      const names = [...new Set(error.fields.map((path) => FIELD_NAMES[path] ?? path))]
      return `Сервер не принял поля: ${names.join(', ')}.`
    }
  }
  return explain(error).reason
}

export function ProfilesPanel() {
  const client = useQueryClient()
  const profiles = useProfiles()
  const picker = useRef<HTMLInputElement>(null)
  const [staged, setStaged] = useState<Staged[]>([])

  const refresh = () => {
    for (const key of [['profile'], ['overview'], ['vacancies'], ['vacancy'], ['documents']]) {
      void client.invalidateQueries({ queryKey: key })
    }
  }

  // A parse ends minutes after the upload: when the last pending resume
  // settles, the screens that depend on the set are read again.
  const pending = profiles.data?.some((profile) => profile.parse_status === 'pending') ?? false
  const wasPending = useRef(false)
  useEffect(() => {
    if (wasPending.current && !pending) {
      for (const key of [['overview'], ['vacancies'], ['vacancy'], ['documents']]) {
        void client.invalidateQueries({ queryKey: key })
      }
    }
    wasPending.current = pending
  }, [pending, client])

  const batch = useMutation({
    mutationFn: (items: Staged[]) => {
      const form = new FormData()
      for (const item of items) form.append('files', item.file)
      form.append(
        'meta',
        JSON.stringify(
          items.map((item) => ({
            slug: item.slug || null,
            track: item.track.trim() || null,
          })),
        ),
      )
      return apiUpload<{ items: Accepted[] }>('/api/v1/resume/batch', form)
    },
    onSuccess: () => {
      setStaged([])
      refresh()
    },
  })

  const stagedValid = staged.every((item) => item.slug === '' || SLUG.test(item.slug))
  const list = profiles.data ?? []

  return (
    <Section
      title="Резюме"
      note="Каждая вакансия сравнивается со всеми включёнными резюме — письмо и отклик идут от лучшего."
      more={
        <>
          <p>
            При равной оценке выигрывает резюме с меньшим приоритетом (0 — главный). Трек — ваше
            слово для направления: backend, llm, ml.
          </p>
          <p>
            hh-id — 38 символов из адреса резюме на hh (hh.kz/resume/…). Без него агент не знает,
            какое резюме выбрать в форме hh, и такие вакансии уходят в «посмотреть руками».
          </p>
          <p>
            Файлы: PDF, DOCX, TXT или Markdown, до 10 МБ. Разбор занимает около трёх минут; после
            него пересчитайте подбор на странице «Обзор».
          </p>
        </>
      }
      action={
        <Button
          outline={list.length > 0}
          onClick={() => {
            picker.current?.click()
          }}
        >
          Добавить резюме…
        </Button>
      }
    >
      <input
        ref={picker}
        type="file"
        multiple
        accept={ACCEPT}
        className="sr-only"
        aria-label="Файлы резюме"
        onChange={(event) => {
          const files = Array.from(event.target.files ?? [])
          setStaged(
            files.map((file) => ({
              file,
              slug: slugFrom(file.name),
              track: '',
            })),
          )
          event.target.value = ''
        }}
      />

      {staged.length > 0 ? (
        <div className="mb-6 border border-ink p-5 sm:p-6">
          <p className="font-semibold">
            Новые файлы: {staged.length}. Дайте каждому метку и трек — их можно поменять потом.
          </p>
          <ul className="mt-4 flex flex-col gap-4">
            {staged.map((item, index) => (
              <li
                key={item.file.name}
                className="grid gap-3 border-t border-hairline pt-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)]"
              >
                <span className="break-words self-center text-small font-semibold">
                  {item.file.name}
                </span>
                <label className="flex flex-col gap-1">
                  <span className="text-label uppercase text-muted">Метка</span>
                  <input
                    className={INPUT}
                    aria-label={`Метка для ${item.file.name}`}
                    aria-invalid={item.slug !== '' && !SLUG.test(item.slug)}
                    placeholder="backend"
                    value={item.slug}
                    onChange={(event) => {
                      const slug = event.target.value.trim().toLowerCase()
                      setStaged((rows) => rows.map((row, i) => (i === index ? { ...row, slug } : row)))
                    }}
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-label uppercase text-muted">Трек</span>
                  <input
                    className={INPUT}
                    aria-label={`Трек для ${item.file.name}`}
                    placeholder="backend, llm, ml…"
                    value={item.track}
                    onChange={(event) => {
                      const track = event.target.value
                      setStaged((rows) => rows.map((row, i) => (i === index ? { ...row, track } : row)))
                    }}
                  />
                </label>
              </li>
            ))}
          </ul>
          {!stagedValid ? (
            <p className="mt-3 text-small font-semibold" role="alert">
              Метка — латиница в нижнем регистре, цифры, «-» и «_», до 50 символов.
            </p>
          ) : null}
          <div className="mt-5 flex flex-wrap items-center gap-4">
            <Button
              busy={batch.isPending ? 'Загружаем…' : false}
              disabled={!stagedValid}
              onClick={() => {
                batch.mutate(staged)
              }}
            >
              Загрузить {staged.length}
            </Button>
            <button
              type="button"
              className="text-small text-muted underline underline-offset-4 hover:text-ink"
              onClick={() => {
                setStaged([])
              }}
            >
              Отменить
            </button>
          </div>
          {batch.isError ? (
            <p className="mt-3 text-small font-semibold" role="alert">
              {explain(batch.error).reason}
            </p>
          ) : null}
        </div>
      ) : null}

      {profiles.isError ? (
        <p className="mb-4 text-small font-semibold" role="alert">
          {explain(profiles.error).reason}
        </p>
      ) : null}

      {list.length === 0 && profiles.isSuccess ? (
        <p className="text-small text-muted">
          Резюме ещё нет — нажмите «Добавить резюме…» и выберите один или несколько файлов.
        </p>
      ) : (
        <div className="grid gap-4">
          {list.map((profile) => (
            <ProfileCard key={profile.id} profile={profile} onSaved={refresh} />
          ))}
        </div>
      )}
    </Section>
  )
}

/** The editable copy of a profile's fields. */
interface Draft {
  slug: string
  track: string
  priority: string
  hhId: string
}

function draftOf(profile: Profile): Draft {
  return {
    slug: profile.slug ?? '',
    track: profile.track ?? '',
    priority: String(profile.priority),
    hhId: profile.hh_resume_id ?? '',
  }
}

function ProfileCard({ profile, onSaved }: { profile: Profile; onSaved: () => void }) {
  const id = useId()
  const file = useRef<HTMLInputElement>(null)
  const [draft, setDraft] = useState<Draft>(() => draftOf(profile))
  const stored = draftOf(profile)

  const save = useMutation({
    mutationFn: (changes: Partial<Profile>) =>
      apiSend<Profile>('PATCH', `/api/v1/profile/${profile.id}`, changes),
    onSuccess: (saved) => {
      setDraft(draftOf(saved))
      onSaved()
    },
  })
  const toggle = useMutation({
    mutationFn: (active: boolean) =>
      apiSend<Profile>('PATCH', `/api/v1/profile/${profile.id}`, { is_active: active }),
    onSuccess: onSaved,
  })
  const replace = useMutation({
    mutationFn: ({ slug, chosen }: { slug: string; chosen: File }) => {
      const form = new FormData()
      form.append('file', chosen)
      return apiUpload<Accepted>(`/api/v1/resume/upload?replace=${encodeURIComponent(slug)}`, form)
    },
    onSuccess: onSaved,
  })
  const saved = useFlash(save.isSuccess ? save.submittedAt : 0)

  const slugValid = SLUG.test(draft.slug)
  const hhValid = draft.hhId === '' || HH_ID.test(draft.hhId)
  const dirty =
    draft.slug !== stored.slug ||
    draft.track.trim() !== stored.track ||
    draft.priority !== stored.priority ||
    draft.hhId !== stored.hhId
  const valid = slugValid && hhValid && draft.priority !== ''

  function set<K extends keyof Draft>(key: K, value: Draft[K]): void {
    // A new edit makes the last error and the last «Сохранено» stale.
    if (save.isError || save.isSuccess) save.reset()
    setDraft((current) => ({ ...current, [key]: value }))
  }

  const name = profile.slug ?? 'без метки'
  const subtitle = profile.headline ?? profile.resume_filename ?? 'без заголовка'

  return (
    <article
      className={`border p-5 sm:p-6 ${save.isError ? 'border-ink' : 'border-hairline'}`}
      aria-labelledby={`${id}-name`}
    >
      <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h3 id={`${id}-name`} className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="text-heading font-semibold leading-none">{name}</span>
            {profile.parse_status === 'pending' ? <Pill>разбирается…</Pill> : null}
            {profile.parse_status === 'failed' ? <Pill strong>не разобралось</Pill> : null}
            {profile.is_active ? null : <Pill>выключено</Pill>}
          </h3>
          <p className="break-words mt-2 text-small text-muted">
            {subtitle}
            {profile.headline && profile.resume_filename ? ` · ${profile.resume_filename}` : ''}
          </p>
        </div>
        <label
          className={`flex items-center gap-2 text-small ${profile.parse_status === 'ready' ? 'cursor-pointer' : 'text-muted'}`}
          title={
            profile.parse_status === 'ready'
              ? undefined
              : 'Включить можно только разобранное резюме'
          }
        >
          <input
            type="checkbox"
            className="h-4 w-4 accent-current"
            checked={profile.is_active}
            disabled={profile.parse_status !== 'ready' || toggle.isPending}
            aria-label={`Включено: ${name}`}
            onChange={(event) => {
              toggle.mutate(event.target.checked)
            }}
          />
          {toggle.isPending ? 'сохраняем…' : 'участвует в подборе'}
        </label>
      </header>

      <ReplacementStatus profileId={profile.id} />
      {profile.parse_status === 'failed' && profile.parse_error ? (
        <p className="break-words mt-4 border-l border-ink pl-4 text-small" role="alert">
          <span className="font-semibold">Почему не разобралось: </span>
          {profile.parse_error}
        </p>
      ) : null}

      <div className="mt-5 grid gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_7rem]">
        <label className="flex min-w-0 flex-col gap-1">
          <span className="text-label uppercase text-muted">Метка</span>
          <input
            className={INPUT}
            aria-label={`Метка ${profile.slug ?? ''}`}
            aria-invalid={!slugValid}
            placeholder="backend"
            value={draft.slug}
            onChange={(event) => {
              set('slug', event.target.value.trim().toLowerCase())
            }}
          />
          {!slugValid ? (
            <span className="text-small font-semibold" role="alert">
              Латиница в нижнем регистре, цифры, «-» и «_».
            </span>
          ) : null}
        </label>
        <label className="flex min-w-0 flex-col gap-1">
          <span className="text-label uppercase text-muted">Трек</span>
          <input
            className={INPUT}
            aria-label={`Трек ${profile.slug ?? ''}`}
            placeholder="backend, llm, ml…"
            value={draft.track}
            onChange={(event) => {
              set('track', event.target.value)
            }}
          />
        </label>
        <label className="flex min-w-0 flex-col gap-1">
          <span className="text-label uppercase text-muted">Приоритет</span>
          <input
            className={`${INPUT} tnum`}
            aria-label={`Приоритет ${profile.slug ?? ''}`}
            inputMode="numeric"
            value={draft.priority}
            onChange={(event) => {
              set('priority', event.target.value.replace(/\D/g, ''))
            }}
          />
        </label>
        <label className="flex min-w-0 flex-col gap-1 sm:col-span-3">
          <span className="text-label uppercase text-muted">hh-id резюме</span>
          <input
            className={`${INPUT} font-mono text-small`}
            aria-label={`hh-id резюме ${profile.slug ?? ''}`}
            aria-invalid={!hhValid}
            placeholder="38 символов из адреса hh.kz/resume/…"
            value={draft.hhId}
            onChange={(event) => {
              set('hhId', event.target.value.trim().toLowerCase())
            }}
          />
          {!hhValid ? (
            <span className="text-small font-semibold" role="alert">
              Нужно ровно 38 символов 0–9 и a–f из адреса резюме на hh.
            </span>
          ) : draft.hhId === '' ? (
            <span className="text-small text-muted">
              Не указан — агент не сможет откликаться этим резюме сам.
            </span>
          ) : null}
        </label>
      </div>

      {save.isError ? (
        <p className="break-words mt-4 border border-ink p-3 text-small font-semibold" role="alert">
          Не сохранено. {saveError(save.error, draft.slug)}
        </p>
      ) : null}
      {toggle.isError || replace.isError ? (
        <p className="break-words mt-4 border border-ink p-3 text-small font-semibold" role="alert">
          {saveError(toggle.error ?? replace.error, '')}
        </p>
      ) : null}
      {replace.isSuccess ? (
        <p className="mt-4 text-small" role="status">
          Новый файл загружен — разбор займёт около трёх минут.
        </p>
      ) : null}

      <div className="mt-5 flex flex-wrap items-center gap-x-4 gap-y-3 border-t border-hairline pt-4">
        <Button
          disabled={!dirty || !valid}
          busy={save.isPending ? 'Сохраняем…' : false}
          title={!dirty ? 'Изменений нет' : !valid ? 'Исправьте поля, отмеченные выше' : undefined}
          onClick={() => {
            save.mutate({
              ...(draft.slug !== stored.slug ? { slug: draft.slug } : {}),
              hh_resume_id: draft.hhId === '' ? null : draft.hhId,
              track: draft.track.trim() === '' ? null : draft.track.trim(),
              priority: Number(draft.priority || '0'),
            })
          }}
        >
          Сохранить
        </Button>
        {dirty && !save.isPending ? (
          <button
            type="button"
            className="text-small text-muted underline underline-offset-4 hover:text-ink"
            onClick={() => {
              save.reset()
              setDraft(stored)
            }}
          >
            Отменить правки
          </button>
        ) : null}
        <span className="text-small" role="status" aria-live="polite">
          {saved && !dirty ? (
            <span className="swap font-semibold">Сохранено</span>
          ) : dirty && !save.isError ? (
            <span className="text-muted">Есть несохранённые правки</span>
          ) : null}
        </span>
        <span className="flex-1" aria-hidden />
        <Button
          outline
          disabled={!profile.slug}
          busy={replace.isPending ? 'Загружаем…' : false}
          title={profile.slug ? undefined : 'Сначала задайте метку'}
          onClick={() => {
            file.current?.click()
          }}
        >
          Заменить файл…
        </Button>
        <input
          ref={file}
          type="file"
          accept={ACCEPT}
          className="sr-only"
          aria-label={`Новая версия резюме ${profile.slug ?? ''}`}
          onChange={(event) => {
            const chosen = event.target.files?.[0]
            if (chosen && profile.slug) replace.mutate({ slug: profile.slug, chosen })
            event.target.value = ''
          }}
        />
      </div>
    </article>
  )
}

/**
 * Where the last file sent for this card got to, from the server's record —
 * so «Новый файл загружен» does not vanish on a reload with no ending.
 */
function ReplacementStatus({ profileId }: { profileId: string }) {
  const { data } = useActivity()
  const item = latestFor(data, ['resume'], profileId)
  if (!item) return null
  if (item.state === 'running') {
    return (
      <p className="mt-4 text-small" role="status">
        Новый файл «{item.label}» разбирается · {elapsed(item.started_at)}. Можно закрыть страницу —
        разбор идёт на сервере.
      </p>
    )
  }
  if (item.state === 'failed') {
    return (
      <p className="mt-4 border border-ink p-3 text-small font-semibold" role="alert">
        Новый файл «{item.label}» не разобрался: {item.detail ?? 'причина в журнале сервера'}.
      </p>
    )
  }
  return (
    <p className="mt-4 text-small text-muted">
      Новый файл «{item.label}» разобран · {elapsed(item.started_at, item.finished_at)}.
    </p>
  )
}
