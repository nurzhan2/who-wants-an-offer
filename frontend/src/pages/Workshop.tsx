/**
 * Мастерская: эталоны, правила, предпросмотр.
 *
 * The screen is arranged in the order the two levers are used. References come
 * first because they are the softer one — form by example, nothing enforced.
 * Rules come second because a hard one changes what may be saved. The preview
 * comes last because it is how a person finds out what the two of them did.
 *
 * The page holds no state the server owns: everything is a query, and every
 * mutation invalidates the list it changed. A rule that was refused must never
 * appear in the list because an optimistic update put it there.
 */
import { type ReactNode, useState } from 'react'

import { Section } from '@/components/ui'
import { PreviewPanel } from '@/components/workshop/PreviewPanel'
import { ReferenceForm, ReferenceList } from '@/components/workshop/References'
import { RuleForm } from '@/components/workshop/RuleForm'
import { RuleList } from '@/components/workshop/RuleList'
import {
  useCreateReference,
  useCreateRule,
  useDeleteReference,
  useDeleteRule,
  usePreview,
  useReferences,
  useRules,
  useUpdateReference,
  useUpdateRule,
  useVacancyChoices,
} from '@/hooks/useWorkshop'

/**
 * The form that adds one, folded until it is wanted: the lists are what the
 * page is read for, and two open forms above them pushed them off the screen.
 */
function AddForm({ label, children }: { label: string; children: ReactNode }) {
  return (
    <details className="more group">
      <summary className="press inline-flex cursor-pointer select-none items-center rounded-pill border border-ink px-6 py-2 text-small transition-colors hover:bg-ink hover:text-paper">
        <span className="group-open:hidden">{label}</span>
        <span className="hidden group-open:inline">Свернуть форму</span>
      </summary>
      <div className="mt-4">{children}</div>
    </details>
  )
}

export function Workshop() {
  const [query, setQuery] = useState('')

  const rules = useRules()
  const references = useReferences()
  const vacancies = useVacancyChoices(query)

  const createRule = useCreateRule()
  const updateRule = useUpdateRule()
  const deleteRule = useDeleteRule()
  const createReference = useCreateReference()
  const updateReference = useUpdateReference()
  const deleteReference = useDeleteReference()
  const preview = usePreview()

  const ruleBusyId = updateRule.isPending
    ? updateRule.variables.id
    : deleteRule.isPending
      ? deleteRule.variables
      : null
  const ruleBusyAction = updateRule.isPending ? 'toggle' : deleteRule.isPending ? 'delete' : null
  const referenceBusyId = updateReference.isPending
    ? updateReference.variables.id
    : deleteReference.isPending
      ? deleteReference.variables
      : null
  const referenceBusyAction = updateReference.isPending
    ? 'toggle'
    : deleteReference.isPending
      ? 'delete'
      : null

  return (
    <div>
      <Section
        title="Правила"
        note="Требования к письмам и CV. Не выполнено жёсткое правило — текст не выдаётся."
        more={
          <>
            <p>
              Жёсткие правила проверяются кодом после генерации; мягкие только отмечаются.
              Правило описывает форму — потребовать написать неправду им нельзя.
            </p>
            <p>Встроенные правила выключить нельзя.</p>
          </>
        }
      >
        <div className="space-y-4">
          {rules.isPending && <p className="text-small text-muted">Загружаем правила…</p>}
          {rules.isError && (
            <p className="text-small font-semibold" role="alert">
              Не удалось загрузить правила.
            </p>
          )}
          {rules.data && (
            <RuleList
              rules={rules.data}
              busyId={ruleBusyId}
              busyAction={ruleBusyAction}
              onToggle={(rule) => {
                updateRule.mutate({ id: rule.id, changes: { is_active: !rule.is_active } })
              }}
              onDelete={(rule) => { deleteRule.mutate(rule.id); }}
            />
          )}
          <AddForm label="Добавить правило">
            <RuleForm
              busy={createRule.isPending}
              error={createRule.error}
              onSubmit={(payload) => { createRule.mutate(payload); }}
            />
          </AddForm>
        </div>
      </Section>

      <Section
        title="Эталоны"
        note="Образцы писем и CV: из них берут форму и тон, но не факты."
        more={
          <p>
            Из эталона берут структуру, длину, порядок и тон. Содержание письма и резюме берут
            только из вашего профиля — факты из чужого документа не переносятся никогда.
          </p>
        }
      >
        <div className="space-y-4">
          {references.isPending && <p className="text-small text-muted">Загружаем эталоны…</p>}
          {references.isError && (
            <p className="text-small font-semibold" role="alert">
              Не удалось загрузить эталоны.
            </p>
          )}
          {references.data && (
            <ReferenceList
              references={references.data}
              busyId={referenceBusyId}
              busyAction={referenceBusyAction}
              onToggle={(reference) => {
                updateReference.mutate({
                  id: reference.id,
                  changes: { is_active: !reference.is_active },
                })
              }}
              onDelete={(reference) => { deleteReference.mutate(reference.id); }}
            />
          )}
          <AddForm label="Добавить эталон">
            <ReferenceForm
              busy={createReference.isPending}
              error={createReference.error}
              warnings={createReference.data?.warnings ?? []}
              onSubmit={(input) => { createReference.mutate(input); }}
            />
          </AddForm>
        </div>
      </Section>

      <PreviewPanel
        vacancies={vacancies.data ?? []}
        query={query}
        onQuery={setQuery}
        busy={preview.isPending}
        error={preview.error}
        preview={preview.data}
        onRun={(vacancyId) => { preview.mutate(vacancyId); }}
      />
    </div>
  )
}
