import { zodResolver } from '@hookform/resolvers/zod'
import { Alert, Anchor, Button, Fieldset, Group, Select, Stack, Textarea, TextInput } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconExternalLink } from '@tabler/icons-react'
import { Controller, useForm } from 'react-hook-form'
import { z } from 'zod'
import { ApiError } from '../api/client'
import { type Project, useCreateFinding } from '../api/hooks'

const FINDING_TYPES = [
  { value: 'stale_identity', label: 'Stale identity' },
  { value: 'overprivileged', label: 'Over-privileged' },
  { value: 'expiring_credential', label: 'Expiring credential' },
  { value: 'exposed_secret', label: 'Exposed secret' },
  { value: 'other', label: 'Other' },
] as const
const SEVERITIES = [
  { value: 'critical', label: 'Critical' },
  { value: 'high', label: 'High' },
  { value: 'medium', label: 'Medium' },
  { value: 'low', label: 'Low' },
] as const

// Mirrors the server's validation for instant feedback; the server remains the authority.
const schema = z.object({
  summary: z
    .string()
    .trim()
    .min(1, 'Give the finding a title.')
    .max(255, 'Keep the title under 255 characters.')
    .refine((v) => !/[\r\n]/.test(v), 'The title must be a single line.'),
  finding_type: z.enum(FINDING_TYPES.map((t) => t.value)).nullable(),
  severity: z.enum(SEVERITIES.map((s) => s.value)).nullable(),
  identity_name: z.string().trim().max(255, 'Keep the identity name under 255 characters.'),
  description: z.string().max(30_000, 'Keep the description under 30,000 characters.'),
})
type Values = z.infer<typeof schema>
const EMPTY: Values = { summary: '', finding_type: null, severity: null, identity_name: '', description: '' }

export function FindingForm({ project }: { project: Project | null }) {
  const createFinding = useCreateFinding()
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: EMPTY })
  const errors = form.formState.errors

  const onSubmit = form.handleSubmit((values) => {
    if (!project) return
    createFinding.mutate(
      {
        project_key: project.key,
        summary: values.summary,
        description: values.description,
        finding_type: values.finding_type ?? undefined,
        severity: values.severity ?? undefined,
        identity_name: values.identity_name || undefined,
      },
      {
        onSuccess: (created) => {
          notifications.show({
            color: 'green',
            title: `${created.key} created in ${project.name}`,
            message: (
              <Anchor href={created.url} target="_blank" rel="noopener noreferrer" size="sm">
                Open in Jira <IconExternalLink size={12} />
              </Anchor>
            ),
            autoClose: 8000,
          })
          form.reset(EMPTY)
        },
        onError: (error) => {
          if (error instanceof ApiError) {
            for (const [field, message] of Object.entries(error.fieldErrors)) {
              if (field in EMPTY) form.setError(field as keyof Values, { message })
            }
          }
        },
      },
    )
  })

  return (
    <form onSubmit={onSubmit} noValidate>
      {/* Disabled until a project is chosen; the inputs keep their values while submitting. */}
      <Fieldset variant="unstyled" disabled={!project}>
        <Stack>
          {createFinding.error && (
            <Alert color="red" title="The ticket wasn't created" withCloseButton onClose={() => createFinding.reset()}>
              {createFinding.error.message}
            </Alert>
          )}
          <TextInput
            label="Title"
            placeholder="e.g. Stale Service Account: svc-deploy-prod"
            withAsterisk
            maxLength={255}
            {...form.register('summary')}
            error={errors.summary?.message}
          />
          <Group grow align="flex-start">
            <Controller
              control={form.control}
              name="finding_type"
              render={({ field }) => (
                <Select label="Finding type" placeholder="Optional" data={FINDING_TYPES} clearable {...field} />
              )}
            />
            <Controller
              control={form.control}
              name="severity"
              render={({ field }) => (
                <Select label="Severity" placeholder="Optional" data={SEVERITIES} clearable {...field} />
              )}
            />
          </Group>
          <TextInput
            label="Affected identity"
            description="The service account, API key or principal this finding is about."
            placeholder="e.g. svc-deploy-prod"
            {...form.register('identity_name')}
            error={errors.identity_name?.message}
          />
          <Textarea
            label="Description"
            placeholder="What was found, why it matters, and what should be done."
            autosize
            minRows={5}
            maxRows={14}
            {...form.register('description')}
            error={errors.description?.message}
          />
          <Group justify="flex-end">
            {/* Disabled while sending, so a double click can't create two tickets. */}
            <Button type="submit" loading={createFinding.isPending}>
              Create Jira ticket
            </Button>
          </Group>
        </Stack>
      </Fieldset>
    </form>
  )
}
