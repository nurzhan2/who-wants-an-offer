import { useEffect, useState } from 'react'

import { ApiError } from '@/api/client'
import { Field } from '@/components/Field'
import { LinkRows } from '@/components/LinkRows'
import { ProfilesPanel } from '@/components/ProfilesPanel'
import { TargetTitles } from '@/components/TargetTitles'
import { useActiveProfile, useContacts, useSaveContacts } from '@/hooks/useContacts'
import { useProfiles } from '@/hooks/useProfiles'
import type { ContactDraft } from '@/lib/contacts'
import { diffContacts, hasChanges, toDraft } from '@/lib/contacts'
import { explain } from '@/lib/errors'
import type { ContactEdits } from '@/types/contact'

/** What the badge under a field says about where its value came from. */
function origin(edited: boolean, value: string): string | undefined {
  if (value.trim() === '') {
    return undefined
  }
  return edited ? 'Исправлено вручную' : 'Взято из резюме — обновится при загрузке нового'
}

/** Field paths as the API names them, in the words this screen uses. */
const FIELD_NAMES: Record<string, string> = {
  full_name: 'имя и фамилия',
  phone: 'телефон',
  email: 'почта',
  city: 'город',
  links: 'ссылки',
}

/**
 * A 422 in Russian.
 *
 * The messages inside a validation error are Pydantic's, in English, aimed at
 * whoever wrote the schema. What is useful here is *which* field was refused,
 * and that the paths carry ("links.0.url" is the first link's address), so the
 * sentence is composed from the paths rather than passed through.
 */
function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return 'Не удалось сохранить. Попробуйте ещё раз.'
  }
  if (error.status === 422 && error.fields !== undefined) {
    const named = [
      ...new Set(
        error.fields.map((path) => {
          const [head, index] = path.split('.')
          const name = FIELD_NAMES[head ?? ''] ?? (head ?? 'поле')
          return head === 'links' && index !== undefined
            ? `ссылка ${String(Number(index) + 1)}`
            : name
        }),
      ),
    ]
    return `Проверьте поля: ${named.join(', ')}.`
  }
  return error.detail ?? `Не удалось сохранить (${String(error.status)}).`
}

/**
 * «Мои данные» — the contact block, as the owner edits it.
 *
 * The screen exists because these values are the only part of a generated CV
 * that is the same for every vacancy, and until now they lived inside the
 * parsed text where nobody could reach them.
 *
 * Two things it is careful about. It submits only the fields that actually
 * changed, because a field named in a PATCH is recorded as settled by a human
 * and stops being refreshed from future resumes — sending the whole form would
 * freeze the lot. And it says, under each field, whether the value was read off
 * the CV or typed, so that "this will change when I upload a new resume" is
 * visible rather than surprising.
 */
/**
 * The resumes (and their upload) are always at the top: until 2026-09-16 this
 * screen said «Резюме ещё не загружено» and offered no way to load one.
 */
export function MyData() {
  // Which resume the rest of the screen edits. Null until the owner picks one:
  // then it is the default profile, as before phase 22.
  const [selected, setSelected] = useState<string | null>(null)
  return (
    <div className="flex flex-col gap-4">
      <ProfilesPanel />
      <ContactsForm selected={selected} onSelect={setSelected} />
    </div>
  )
}

/**
 * Which resume the titles and contacts below belong to. A picker rather than
 * a radio in each card: the cards are about the set, this is about one of them.
 */
function ProfilePicker({
  value,
  onSelect,
}: {
  value: string
  onSelect: (profileId: string) => void
}) {
  const list = useProfiles()
  const options = list.data?.filter((row) => row.parse_status === 'ready') ?? []
  if (options.length < 2) return null
  return (
    <label className="flex flex-wrap items-center gap-3 text-small">
      <span className="text-muted">Названия должностей и контакты ниже — для резюме</span>
      <select
        className="border border-hairline bg-paper px-3 py-2 text-ink"
        value={value}
        onChange={(event) => {
          onSelect(event.target.value)
        }}
      >
        {options.map((row) => (
          <option key={row.id} value={row.id}>
            {row.slug ?? row.headline ?? row.resume_filename ?? row.id}
            {row.track ? ` · ${row.track}` : ''}
          </option>
        ))}
      </select>
    </label>
  )
}

function ContactsForm({
  selected,
  onSelect,
}: {
  selected: string | null
  onSelect: (profileId: string) => void
}) {
  const profile = useActiveProfile()
  const list = useProfiles()
  const chosen = list.data?.find((row) => row.id === selected) ?? profile.data
  const profileId = chosen?.id
  const contacts = useContacts(profileId)
  const save = useSaveContacts(profileId)

  const [draft, setDraft] = useState<ContactDraft | null>(null)
  const stored = contacts.data

  // Re-seeded whenever the server's copy changes, which after the initial load
  // means "when a save came back". Refetch on focus is off application-wide, so
  // this cannot overwrite something half-typed.
  useEffect(() => {
    if (stored !== undefined) {
      setDraft(toDraft(stored))
    }
  }, [stored])

  if (profile.isPending || contacts.isPending) {
    return <Note>Загружаем…</Note>
  }

  if (profile.isError) {
    const missing = profile.error instanceof ApiError && profile.error.status === 404
    return (
      <Note>
        {missing
          ? 'Контакты появятся здесь, когда загруженное резюме разберётся.'
          : `${explain(profile.error).reason} ${explain(profile.error).remedy}`}
      </Note>
    )
  }

  if (contacts.isError || stored === undefined || draft === null) {
    return <Note>Не удалось прочитать контакты.</Note>
  }

  const changes = diffContacts(stored, draft)
  const dirty = hasChanges(changes)
  const edits: ContactEdits = stored.edited

  function set<K extends keyof ContactDraft>(key: K, value: ContactDraft[K]) {
    setDraft((current) => (current === null ? current : { ...current, [key]: value }))
  }

  return (
    <form
      className="flex flex-col gap-12"
      onSubmit={(event) => {
        event.preventDefault()
        if (dirty) {
          save.mutate(changes)
        }
      }}
    >
      {chosen ? <ProfilePicker value={chosen.id} onSelect={onSelect} /> : null}

      {/* Inside the form only for the layout: it has its own save button, and
          none of its controls submit the contact block. */}
      {chosen ? (
        <TargetTitles key={chosen.id} profileId={chosen.id} stored={chosen.target_titles} />
      ) : null}

      <section className="flex flex-col gap-6">
        <SectionTitle
          title="Контакты для CV"
          note="Печатаются в каждом собранном CV. В подборе вакансий не участвуют."
        />
        <div className="grid grid-cols-1 gap-6 sm:grid-cols-2">
          <Field
            id="full_name"
            label="Имя и фамилия"
            value={draft.full_name}
            onChange={(value) => {
              set('full_name', value)
            }}
            autoComplete="name"
            hint={origin(edits.full_name, draft.full_name)}
          />
          <Field
            id="city"
            label="Город"
            value={draft.city}
            onChange={(value) => {
              set('city', value)
            }}
            autoComplete="address-level2"
            hint={origin(edits.city, draft.city)}
          />
          <Field
            id="phone"
            label="Телефон"
            type="tel"
            value={draft.phone}
            onChange={(value) => {
              set('phone', value)
            }}
            autoComplete="tel"
            placeholder="+7 700 000 00 00"
            hint={origin(edits.phone, draft.phone)}
          />
          <Field
            id="email"
            label="Почта"
            type="email"
            value={draft.email}
            onChange={(value) => {
              set('email', value)
            }}
            autoComplete="email"
            hint={origin(edits.email, draft.email)}
          />
        </div>
      </section>

      <section className="flex flex-col gap-4">
        <div>
          <h3 className="text-small font-semibold uppercase tracking-widest">Ссылки</h3>
          <p className="mt-1 text-small text-muted">GitHub, Telegram, сайт, портфолио.</p>
        </div>
        <LinkRows
          links={draft.links}
          onChange={(links) => {
            set('links', links)
          }}
        />
      </section>

      <div className="flex flex-wrap items-center gap-6 border-t border-hairline pt-8">
        <button
          type="submit"
          disabled={!dirty || save.isPending}
          className="rounded-pill bg-ink px-10 py-3 text-label uppercase text-paper transition-colors disabled:cursor-not-allowed disabled:bg-hairline disabled:text-muted"
        >
          {save.isPending ? 'Сохраняем…' : 'Сохранить'}
        </button>
        {save.isError && (
          <p className="border border-ink px-3 py-2 text-small font-semibold" role="alert">
            Не сохранено. {errorMessage(save.error)}
          </p>
        )}
        {!dirty && save.isSuccess && (
          <p className="text-small font-semibold" role="status">
            Сохранено.
          </p>
        )}
        {dirty && !save.isError && <p className="text-small text-muted">Есть несохранённые правки.</p>}
      </div>
    </form>
  )
}

/** The same heading as every other screen's sections. */
function SectionTitle({ title, note }: { title: string; note: string }) {
  return (
    <header className="border-b border-hairline pb-4">
      <h2 className="text-heading font-semibold">{title}</h2>
      <p className="mt-1 max-w-2xl text-small text-muted">{note}</p>
    </header>
  )
}

function Note({ children }: { children: string }) {
  return <p className="text-sm text-muted">{children}</p>
}
