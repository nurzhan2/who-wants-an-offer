/**
 * Reference documents: the list, and the form that adds one.
 *
 * The form takes a file or pasted text, never both, because the endpoint does —
 * two ways of giving one document, and only one of them can be the stored one.
 *
 * The upload's warnings are rendered where the upload happened and not swallowed
 * into a toast. "This PDF is laid out in two columns and came out interleaved"
 * has to reach the person while they still have the file open and can paste the
 * text instead; a warning shown after they have navigated away is a warning
 * nobody acts on.
 */
import { useState } from 'react'

import { Button, Pill } from '@/components/ui'
import { REFERENCE_KIND_LABELS } from '@/components/workshop/labels'
import type { Reference, ReferenceKind } from '@/types/workshop'

const FIELD =
  'w-full rounded-field border border-hairline bg-paper px-3 py-2 text-small text-ink outline-none transition-colors focus:border-ink'
const LABEL = 'block text-label uppercase text-muted'

interface FormProps {
  busy: boolean
  error: unknown
  warnings: string[]
  onSubmit: (input: {
    kind: ReferenceKind
    title: string
    note: string
    file: File | null
    text: string
  }) => void
}

export function ReferenceForm({ busy, error, warnings, onSubmit }: FormProps) {
  const [kind, setKind] = useState<ReferenceKind>('cover_letter')
  const [title, setTitle] = useState('')
  const [note, setNote] = useState('')
  const [text, setText] = useState('')
  const [file, setFile] = useState<File | null>(null)

  return (
    <form
      className="space-y-4 border border-hairline p-5 sm:p-6"
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit({ kind, title, note, file, text })
      }}
    >
      <h3 className="text-sm font-semibold text-ink">Новый эталон</h3>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1">
          <span className={LABEL}>Вид</span>
          <select
            className={FIELD}
            value={kind}
            onChange={(event) => { setKind(event.target.value as ReferenceKind); }}
          >
            {(Object.keys(REFERENCE_KIND_LABELS) as ReferenceKind[]).map((option) => (
              <option key={option} value={option}>
                {REFERENCE_KIND_LABELS[option]}
              </option>
            ))}
          </select>
        </label>

        <label className="space-y-1">
          <span className={LABEL}>Название</span>
          <input
            className={FIELD}
            value={title}
            onChange={(event) => { setTitle(event.target.value); }}
            placeholder="Письмо, на которое ответили"
            required
          />
        </label>
      </div>

      <label className="space-y-1">
        <span className={LABEL}>Чем именно хорош</span>
        <input
          className={FIELD}
          value={note}
          onChange={(event) => { setNote(event.target.value); }}
          placeholder="Короткое, без канцелярита, сразу по требованиям"
        />
      </label>

      <label className="space-y-1">
        <span className={LABEL}>Файл (PDF, DOCX, TXT, MD)</span>
        <input
          className={FIELD}
          type="file"
          accept=".pdf,.docx,.txt,.md,.markdown"
          onChange={(event) => { setFile(event.target.files?.[0] ?? null); }}
        />
      </label>

      {file === null && (
        <label className="space-y-1">
          <span className={LABEL}>…или вставьте текст</span>
          <textarea
            className={`${FIELD} h-40 font-mono`}
            value={text}
            onChange={(event) => { setText(event.target.value); }}
            placeholder="Текст эталонного документа"
          />
        </label>
      )}

      {warnings.length > 0 && (
        <ul className="invert-surface space-y-1 p-3 text-small">
          {warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}

      {error != null && (
        <p className="border border-ink p-3 text-small font-semibold">
          {error instanceof Error ? error.message : 'Не удалось сохранить эталон'}
        </p>
      )}

      <div>
        <Button type="submit" busy={busy ? 'Загружаем эталон…' : false}>
          Добавить эталон
        </Button>
      </div>
    </form>
  )
}

interface ListProps {
  references: Reference[]
  busyId: string | null
  /** What is being done to the row `busyId` names. */
  busyAction: 'toggle' | 'delete' | null
  onToggle: (reference: Reference) => void
  onDelete: (reference: Reference) => void
}

export function ReferenceList({ references, busyId, busyAction, onToggle, onDelete }: ListProps) {
  if (references.length === 0) {
    return <p className="text-sm text-muted">Эталонов пока нет.</p>
  }

  return (
    <ul className="space-y-2">
      {references.map((reference) => (
        <li
          key={reference.id}
          className={`border border-hairline p-4 ${reference.is_active ? '' : 'text-muted'}`}
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0 space-y-1">
              <p className="font-semibold">
                {reference.title}{' '}
                <span className="text-xs font-normal text-muted">
                  · {REFERENCE_KIND_LABELS[reference.kind]} · {reference.characters} знаков
                  {reference.source_filename ? ` · ${reference.source_filename}` : ' · вставлен текстом'}
                </span>
              </p>
              {reference.note && <p className="text-sm text-muted">{reference.note}</p>}
              <p className="truncate text-xs text-muted">{reference.preview}</p>
              {!reference.is_active && <Pill>выключен</Pill>}
            </div>

            <div className="flex shrink-0 gap-2">
              <Button
                outline
                disabled={busyId === reference.id}
                busy={
                  busyId === reference.id && busyAction === 'toggle'
                    ? reference.is_active
                      ? 'Выключаем…'
                      : 'Включаем…'
                    : false
                }
                onClick={() => { onToggle(reference); }}
              >
                {reference.is_active ? 'Выключить' : 'Включить'}
              </Button>
              <Button
                outline
                disabled={busyId === reference.id}
                busy={busyId === reference.id && busyAction === 'delete' ? 'Удаляем…' : false}
                onClick={() => { onDelete(reference); }}
              >
                Удалить
              </Button>
            </div>
          </div>
        </li>
      ))}
    </ul>
  )
}
