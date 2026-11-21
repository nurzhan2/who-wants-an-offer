import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'

import { apiSend, apiUpload } from '@/api/client'
import { Button } from '@/components/ui'
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
 * Several files can be sent at once, each with its own slug and track. An
 * upload never switches the others off; «Заменить» replaces one resume with a
 * new version of itself once the new file has parsed.
 */

const ACCEPT = '.pdf,.docx,.txt,.md'
const HH_ID = /^[0-9a-f]{38}$/
const SLUG = /^[a-z0-9][a-z0-9_-]{0,49}$/

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

export function ProfilesPanel({
  selected,
  onSelect,
}: {
  selected: string | null
  onSelect: (profileId: string) => void
}) {
  const client = useQueryClient()
  const profiles = useProfiles()
  const picker = useRef<HTMLInputElement>(null)
  const [staged, setStaged] = useState<Staged[]>([])

  const refresh = () => {
    for (const key of [['profile'], ['overview'], ['vacancies'], ['vacancy']]) {
      void client.invalidateQueries({ queryKey: key })
    }
  }

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

  const replace = useMutation({
    mutationFn: ({ slug, file }: { slug: string; file: File }) => {
      const form = new FormData()
      form.append('file', file)
      return apiUpload<Accepted>(`/api/v1/resume/upload?replace=${encodeURIComponent(slug)}`, form)
    },
    onSuccess: refresh,
  })

  const patch = useMutation({
    mutationFn: ({ id, changes }: { id: string; changes: Partial<Profile> }) =>
      apiSend<Profile>('PATCH', `/api/v1/profile/${id}`, changes),
    onSuccess: refresh,
  })

  const stagedValid = staged.every((item) => item.slug === '' || SLUG.test(item.slug))

  return (
    <section className="border border-hairline p-card">
      <p className="font-semibold">Резюме по трекам</p>
      <p className="mt-1 max-w-2xl text-small text-muted">
        Каждая вакансия оценивается по всем включённым резюме и идёт от того, с которым совпала
        лучше: им пишется письмо и им отправляется отклик. При равной оценке выигрывает меньший
        приоритет. Без hh-id резюме агент не знает, какое выбрать в форме hh, — такие вакансии
        уходят в «посмотреть руками».
      </p>

      {profiles.isError ? (
        <p className="mt-3 text-small font-semibold" role="alert">
          {explain(profiles.error).reason}
        </p>
      ) : null}

      <div className="mt-4 flex flex-col divide-y divide-hairline border-y border-hairline">
        {(profiles.data ?? []).map((profile) => (
          <ProfileRow
            key={profile.id}
            profile={profile}
            selected={selected === profile.id}
            onSelect={() => {
              onSelect(profile.id)
            }}
            onSave={(changes) => {
              patch.mutate({ id: profile.id, changes })
            }}
            onReplace={(file) => {
              if (profile.slug) replace.mutate({ slug: profile.slug, file })
            }}
          />
        ))}
        {profiles.data?.length === 0 ? (
          <p className="py-3 text-small text-muted">
            Резюме ещё нет — загрузите одно или несколько.
          </p>
        ) : null}
      </div>
      {patch.isError || replace.isError ? (
        <p className="mt-3 text-small font-semibold" role="alert">
          {explain(patch.error ?? replace.error).reason}
        </p>
      ) : null}

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
      <div className="mt-4 flex flex-wrap items-center gap-4">
        <Button
          outline
          onClick={() => {
            picker.current?.click()
          }}
        >
          Выбрать файлы
        </Button>
        {staged.length > 0 ? (
          <Button
            busy={batch.isPending ? 'Загружаем…' : false}
            disabled={!stagedValid}
            onClick={() => {
              batch.mutate(staged)
            }}
          >
            Загрузить {staged.length}
          </Button>
        ) : null}
      </div>
      {staged.length > 0 ? (
        <ul className="mt-3 flex flex-col gap-2">
          {staged.map((item, index) => (
            <li key={item.file.name} className="flex flex-wrap items-center gap-2 text-small">
              <span className="min-w-40 break-anywhere">{item.file.name}</span>
              <input
                className="border border-hairline px-2 py-1"
                aria-label={`Метка для ${item.file.name}`}
                placeholder="метка: backend"
                value={item.slug}
                onChange={(event) => {
                  const slug = event.target.value
                  setStaged((rows) => rows.map((row, i) => (i === index ? { ...row, slug } : row)))
                }}
              />
              <input
                className="border border-hairline px-2 py-1"
                aria-label={`Трек для ${item.file.name}`}
                placeholder="трек: backend, llm, ml…"
                value={item.track}
                onChange={(event) => {
                  const track = event.target.value
                  setStaged((rows) => rows.map((row, i) => (i === index ? { ...row, track } : row)))
                }}
              />
            </li>
          ))}
        </ul>
      ) : null}
      {batch.isError ? (
        <p className="mt-3 text-small font-semibold" role="alert">
          {explain(batch.error).reason}
        </p>
      ) : null}
    </section>
  )
}

function ProfileRow({
  profile,
  selected,
  onSelect,
  onSave,
  onReplace,
}: {
  profile: Profile
  selected: boolean
  onSelect: () => void
  onSave: (changes: Partial<Profile>) => void
  onReplace: (file: File) => void
}) {
  const file = useRef<HTMLInputElement>(null)
  const [hhId, setHhId] = useState(profile.hh_resume_id ?? '')
  const [track, setTrack] = useState(profile.track ?? '')
  const [priority, setPriority] = useState(String(profile.priority))
  const hhValid = hhId === '' || HH_ID.test(hhId)
  const dirty =
    hhId !== (profile.hh_resume_id ?? '') ||
    track !== (profile.track ?? '') ||
    priority !== String(profile.priority)

  return (
    <div className={`flex flex-col gap-2 py-3 ${selected ? 'font-semibold' : ''}`}>
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-small">
          <input type="radio" name="profile" checked={selected} onChange={onSelect} />
          <span>{profile.slug ?? '—'}</span>
        </label>
        <span className="text-small text-muted">
          {profile.headline ?? profile.resume_filename ?? 'без заголовка'}
          {profile.parse_status === 'pending' ? ' · разбирается…' : ''}
          {profile.parse_status === 'failed' ? ' · не разобралось' : ''}
        </span>
        <label className="flex items-center gap-1 text-small">
          <input
            type="checkbox"
            checked={profile.is_active}
            disabled={profile.parse_status !== 'ready'}
            onChange={(event) => {
              onSave({ is_active: event.target.checked })
            }}
          />
          включено
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-small">
        <input
          className="border border-hairline px-2 py-1"
          aria-label={`Трек ${profile.slug ?? ''}`}
          placeholder="трек"
          value={track}
          onChange={(event) => {
            setTrack(event.target.value)
          }}
        />
        <input
          className="w-20 border border-hairline px-2 py-1"
          aria-label={`Приоритет ${profile.slug ?? ''}`}
          inputMode="numeric"
          value={priority}
          onChange={(event) => {
            setPriority(event.target.value.replace(/\D/g, ''))
          }}
        />
        <input
          className="w-96 max-w-full border border-hairline px-2 py-1 font-mono"
          aria-label={`hh-id резюме ${profile.slug ?? ''}`}
          aria-invalid={!hhValid}
          placeholder="hh-id резюме: 38 символов из ссылки hh.kz/resume/…"
          value={hhId}
          onChange={(event) => {
            setHhId(event.target.value.trim().toLowerCase())
          }}
        />
        <button
          type="button"
          className="underline disabled:opacity-50"
          disabled={!dirty || !hhValid}
          onClick={() => {
            onSave({
              hh_resume_id: hhId === '' ? null : hhId,
              track: track.trim() === '' ? null : track.trim(),
              priority: Number(priority || '0'),
            })
          }}
        >
          Сохранить
        </button>
        <button
          type="button"
          className="underline"
          onClick={() => {
            file.current?.click()
          }}
        >
          Заменить файл
        </button>
        <input
          ref={file}
          type="file"
          accept={ACCEPT}
          className="sr-only"
          aria-label={`Новая версия резюме ${profile.slug ?? ''}`}
          onChange={(event) => {
            const chosen = event.target.files?.[0]
            if (chosen) onReplace(chosen)
            event.target.value = ''
          }}
        />
      </div>
      {profile.parse_status === 'failed' && profile.parse_error ? (
        <p className="break-anywhere text-small font-semibold" role="alert">
          Почему не разобралось: {profile.parse_error}
        </p>
      ) : null}
      {!hhValid ? (
        <p className="text-small font-semibold" role="alert">
          hh-id — 38 символов 0–9 и a–f из адреса резюме на hh.
        </p>
      ) : null}
    </div>
  )
}
