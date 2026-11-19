import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { apiSend } from '@/api/client'
import { Button } from '@/components/ui'
import { explain } from '@/lib/errors'

/**
 * Добавить вакансию вручную — the third legal path to LinkedIn and Indeed.
 *
 * A link alone is fetched once, anonymously; a LinkedIn, Indeed or Glassdoor
 * link is never opened, so for those the text goes in with it. The vacancy
 * joins the common scoring on the next «Пересчитать подбор».
 */

interface Imported {
  vacancy_id: string
  created: boolean
  title: string
  company: string | null
  parsed_by: string
}

export function ImportVacancy() {
  const client = useQueryClient()
  const [open, setOpen] = useState(false)
  const [url, setUrl] = useState('')
  const [text, setText] = useState('')

  const save = useMutation({
    mutationFn: () =>
      apiSend<Imported>('POST', '/api/v1/vacancies/import', {
        url: url.trim() || null,
        text: text.trim() || null,
      }),
    onSuccess: () => {
      setUrl('')
      setText('')
      void client.invalidateQueries({ queryKey: ['vacancies'] })
    },
  })

  if (!open) {
    return (
      <button
        type="button"
        className="text-small underline"
        onClick={() => {
          setOpen(true)
        }}
      >
        Добавить вакансию вручную
      </button>
    )
  }

  return (
    <div className="mb-6 flex flex-col gap-3 border border-hairline p-card">
      <p className="text-small text-muted">
        Вставьте ссылку или текст вакансии. Страницы LinkedIn, Indeed и Glassdoor проект не
        открывает — для них вставьте текст вместе со ссылкой.
      </p>
      <input
        className="border border-hairline px-2 py-1"
        aria-label="Ссылка на вакансию"
        placeholder="https://…"
        value={url}
        onChange={(event) => {
          setUrl(event.target.value)
        }}
      />
      <textarea
        className="min-h-32 border border-hairline px-2 py-1"
        aria-label="Текст вакансии"
        placeholder="Текст вакансии"
        value={text}
        onChange={(event) => {
          setText(event.target.value)
        }}
      />
      <div className="flex items-center gap-4">
        <Button
          busy={save.isPending ? 'Добавляем…' : false}
          disabled={url.trim() === '' && text.trim() === ''}
          onClick={() => {
            save.mutate()
          }}
        >
          Добавить
        </Button>
        <button
          type="button"
          className="text-small underline"
          onClick={() => {
            setOpen(false)
          }}
        >
          Закрыть
        </button>
      </div>
      {save.isSuccess ? (
        <p className="text-small" role="status">
          {save.data.created ? 'Добавлено' : 'Уже была в базе'}: {save.data.title}
          {save.data.company ? ` — ${save.data.company}` : ''}. Оценка появится после «Пересчитать
          подбор».
        </p>
      ) : null}
      {save.isError ? (
        <p className="text-small font-semibold" role="alert">
          {explain(save.error).reason}
        </p>
      ) : null}
    </div>
  )
}
