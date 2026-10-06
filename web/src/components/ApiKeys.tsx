import { zodResolver } from '@hookform/resolvers/zod'
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Checkbox,
  Code,
  CopyButton,
  Fieldset,
  Group,
  MultiSelect,
  Paper,
  Select,
  SimpleGrid,
  Skeleton,
  Stack,
  Text,
  Textarea,
  TextInput,
  Tooltip,
} from '@mantine/core'
import { IconCheck, IconCopy, IconKey, IconLock, IconPencil, IconPlus } from '@tabler/icons-react'
import { type ReactNode, useState } from 'react'
import { Controller, useForm, useWatch } from 'react-hook-form'
import { z } from 'zod'
import { ApiError } from '../api/client'
import {
  type ApiKey,
  type ApiKeyCreate,
  type ApiKeyCreated,
  type ApiKeyScope,
  type Project,
  useApiKeys,
  useCreateApiKey,
  useProjectOptions,
  useRevokeApiKey,
  useUpdateApiKeyNotes,
} from '../api/hooks'
import { formatDateTime, timeAgo } from '../lib/time'
import { ErrorState } from './ErrorState'

const LIFETIMES = [7, 30, 90, 180, 365] as const
const DEFAULT_LIFETIME = 90
const MAX_NOTES = 1000

/** Human labels for permission scopes. A scope added on the server later shows its raw name
 * until it is labelled here. */
const SCOPES: { value: ApiKeyScope; label: string; description: string }[] = [
  {
    value: 'findings:create',
    label: 'Create findings',
    description: 'POST /api/v1/findings: create NHI finding tickets in the projects below.',
  },
]
const scopeLabel = (scope: string) => SCOPES.find((s) => s.value === scope)?.label ?? scope

/** "ihub_ax3K************": enough to recognise a key; a fixed mask length doesn't reveal its size. */
const maskKey = (prefix: string) => `${prefix}${'*'.repeat(12)}`

/** The API keys part of Settings. `jiraReady` is false until a Jira site is connected, because a
 * key is limited to projects the user can create issues in, which only Jira can tell us. */
export function ApiKeysSection({ jiraReady }: { jiraReady: boolean }) {
  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<ApiKeyCreated | null>(null)

  return (
    <Stack>
      {created && <NewKeyReveal created={created} onDone={() => setCreated(null)} />}
      {creating ? (
        <CreateKeyForm
          onCancel={() => setCreating(false)}
          onCreated={(key) => {
            setCreating(false)
            setCreated(key)
          }}
        />
      ) : (
        <Group>
          <Button leftSection={<IconPlus size={16} />} onClick={() => setCreating(true)} disabled={!jiraReady}>
            Create API key
          </Button>
          {!jiraReady && (
            <Text size="sm" c="dimmed">
              Connect Jira first: a key can only post to projects you can create issues in.
            </Text>
          )}
        </Group>
      )}
      <KeyList />
    </Stack>
  )
}

// --- Creating ---------------------------------------------------------------------------------

const schema = z.object({
  name: z.string().trim().min(1, 'Name the key after where it will be used.').max(100, 'Keep the name under 100 characters.'),
  notes: z.string().max(MAX_NOTES, `Keep notes under ${MAX_NOTES} characters.`),
  scopes: z.array(z.string()).min(1, 'Choose at least one action.'),
  projects: z.array(z.string()).min(1, 'Choose at least one project.').max(50, 'Choose at most 50 projects.'),
  expires_in_days: z.enum(LIFETIMES.map(String) as [string, ...string[]]),
})
type Values = z.infer<typeof schema>

function CreateKeyForm({ onCancel, onCreated }: { onCancel: () => void; onCreated: (key: ApiKeyCreated) => void }) {
  const create = useCreateApiKey()
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: '',
      notes: '',
      scopes: ['findings:create'],
      projects: [],
      expires_in_days: String(DEFAULT_LIFETIME),
    },
  })
  const lifetime = Number(useWatch({ control: form.control, name: 'expires_in_days' }))
  const [openedAt] = useState(Date.now) // "expires on" preview, relative to when the form opened
  const expiresOn = new Date(openedAt + lifetime * 86_400_000).toLocaleDateString(undefined, { dateStyle: 'medium' })

  const onSubmit = form.handleSubmit((values) =>
    create.mutate(
      {
        name: values.name,
        notes: values.notes.trim() || null,
        expires_in_days: Number(values.expires_in_days) as ApiKeyCreate['expires_in_days'],
        permissions: { scopes: values.scopes as ApiKeyScope[], projects: values.projects },
      },
      {
        onSuccess: onCreated,
        onError: (error) => {
          if (error instanceof ApiError && error.code === 'api_key_projects_not_permitted') {
            form.setError('projects', { message: error.message })
          }
        },
      },
    ),
  )

  const showBanner =
    create.error && !(create.error instanceof ApiError && create.error.code === 'api_key_projects_not_permitted')
  return (
    <form onSubmit={onSubmit} noValidate>
      <Paper withBorder p="md">
        <Stack>
          <Text fw={600}>New API key</Text>
          {showBanner && <Alert color="red">{create.error.message}</Alert>}
          <TextInput
            label="Name"
            placeholder="e.g. GitHub Actions – infra repo"
            withAsterisk
            autoFocus
            {...form.register('name')}
            error={form.formState.errors.name?.message}
          />
          <Textarea
            label="Notes"
            description="What it's used for and who owns it. You can edit notes later."
            placeholder="e.g. Nightly secrets scan in infra-ci; owned by the platform team"
            autosize
            minRows={2}
            maxRows={5}
            {...form.register('notes')}
            error={form.formState.errors.notes?.message}
          />

          <Fieldset
            legend={
              <Group gap={6}>
                <IconLock size={14} />
                <Text size="sm" fw={500}>
                  Permissions
                </Text>
              </Group>
            }
          >
            <Stack>
              <Text size="xs" c="dimmed">
                Permissions can't be changed after the key is created. To change them, create a new key and revoke
                this one.
              </Text>
              <Controller
                control={form.control}
                name="scopes"
                render={({ field, fieldState }) => (
                  <Checkbox.Group label="Allowed actions" withAsterisk error={fieldState.error?.message} {...field}>
                    <Stack gap="xs" mt="xs">
                      {SCOPES.map((scope) => (
                        <Checkbox key={scope.value} value={scope.value} label={scope.label} description={scope.description} />
                      ))}
                    </Stack>
                  </Checkbox.Group>
                )}
              />
              <Controller
                control={form.control}
                name="projects"
                render={({ field, fieldState }) => (
                  <ProjectMultiSelect value={field.value} onChange={field.onChange} error={fieldState.error?.message} />
                )}
              />
              <Controller
                control={form.control}
                name="expires_in_days"
                render={({ field }) => (
                  <Select
                    label="Expires after"
                    description={`The key stops working on ${expiresOn}.`}
                    data={LIFETIMES.map((d) => ({ value: String(d), label: d === 365 ? '1 year' : `${d} days` }))}
                    allowDeselect={false}
                    {...field}
                  />
                )}
              />
            </Stack>
          </Fieldset>

          <Group justify="flex-end">
            <Button variant="default" onClick={onCancel}>
              Cancel
            </Button>
            <Button type="submit" loading={create.isPending} leftSection={<IconKey size={16} />}>
              Create key
            </Button>
          </Group>
        </Stack>
      </Paper>
    </form>
  )
}

/** Searchable multi-select of projects the user can create issues in (searched in Jira). */
function ProjectMultiSelect({
  value,
  onChange,
  error,
}: {
  value: string[]
  onChange: (keys: string[]) => void
  error?: string
}) {
  const [search, setSearch] = useState('')
  const projects = useProjectOptions(search, true)
  // Labels of chosen projects, captured when picked, so they survive later searches.
  const [chosenLabels, setChosenLabels] = useState<Record<string, string>>({})

  const options = new Map<string, string>()
  for (const key of value) options.set(key, chosenLabels[key] ?? key)
  for (const p of projects.projects) options.set(p.key, projectLabel(p))

  const change = (keys: string[]) => {
    setChosenLabels((prev) => {
      const next = { ...prev }
      for (const key of keys) next[key] ??= options.get(key) ?? key
      return next
    })
    onChange(keys)
  }

  return (
    <MultiSelect
      label="Projects"
      description="The key can only create tickets in these projects."
      placeholder={value.length ? undefined : 'Search by name or key'}
      withAsterisk
      searchable
      searchValue={search}
      onSearchChange={setSearch}
      data={[...options].map(([key, text]) => ({ value: key, label: text }))}
      value={value}
      onChange={change}
      // Typing narrows the loaded options instantly (Jira search fills in the rest) and highlights
      // the first match, so Enter picks it.
      selectFirstOptionOnChange
      onKeyDown={(event) => {
        // Enter in the picker selects a project; it must never submit the whole form.
        if (event.key === 'Enter') event.preventDefault()
      }}
      nothingFoundMessage={projects.isFetching ? 'Searching…' : 'No matching projects you can create issues in'}
      error={error ?? projects.error?.message}
      maxValues={50}
      hidePickedOptions
    />
  )
}

const projectLabel = (p: Project) => `${p.name} (${p.key})`

// --- Showing a new key once -------------------------------------------------------------------

function NewKeyReveal({ created, onDone }: { created: ApiKeyCreated; onDone: () => void }) {
  const curl = [
    `curl -X POST ${window.location.origin}/api/v1/findings \\`,
    `  -H "Authorization: Bearer ${created.key}" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '{"project_key": "${created.permissions.projects[0]}", "summary": "Stale Service Account: svc-deploy-prod"}'`,
  ].join('\n')

  return (
    <Alert color="green" icon={<IconKey />} title={`API key "${created.name}" created`}>
      <Stack gap="sm">
        <Text size="sm" fw={500}>
          Copy it now. For your security it won't be shown again.
        </Text>
        <Group gap="xs" wrap="nowrap">
          <Code block style={{ flex: 1, wordBreak: 'break-all', whiteSpace: 'pre-wrap' }}>
            {created.key}
          </Code>
          <CopyValueButton value={created.key} label="Copy key" />
        </Group>
        <Text size="sm">
          Example request (see the{' '}
          <Anchor href="/docs#/public%20API%20v1" target="_blank" rel="noopener noreferrer" inherit>
            API docs
          </Anchor>
          ):
        </Text>
        <Group gap="xs" wrap="nowrap" align="flex-start">
          <Code block style={{ flex: 1, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
            {curl}
          </Code>
          <CopyValueButton value={curl} label="Copy command" />
        </Group>
        <Group justify="flex-end">
          <Button variant="light" color="green" onClick={onDone}>
            I've saved it
          </Button>
        </Group>
      </Stack>
    </Alert>
  )
}

function CopyValueButton({ value, label }: { value: string; label: string }) {
  return (
    <CopyButton value={value}>
      {({ copied, copy }) => (
        <Tooltip label={copied ? 'Copied' : label}>
          <ActionIcon variant="light" color={copied ? 'teal' : 'gray'} onClick={copy} aria-label={label}>
            {copied ? <IconCheck size={16} /> : <IconCopy size={16} />}
          </ActionIcon>
        </Tooltip>
      )}
    </CopyButton>
  )
}

// --- Listing, notes and revoking --------------------------------------------------------------

const STATUS_COLOR = { active: 'green', expired: 'gray', revoked: 'red' } as const

function KeyList() {
  const keysQuery = useApiKeys()
  if (keysQuery.isPending) return <Skeleton h={80} />
  if (keysQuery.isError) return <ErrorState error={keysQuery.error} onRetry={() => keysQuery.refetch()} />
  if (keysQuery.data.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        You don't have any API keys yet.
      </Text>
    )
  }
  return (
    <Stack gap="sm" component="ul" p={0} m={0} style={{ listStyle: 'none' }}>
      {keysQuery.data.map((key) => (
        <li key={key.id}>
          <KeyCard apiKey={key} />
        </li>
      ))}
    </Stack>
  )
}

function KeyCard({ apiKey }: { apiKey: ApiKey }) {
  const validity =
    apiKey.status === 'revoked' && apiKey.revoked_at
      ? { text: `Revoked ${timeAgo(apiKey.revoked_at)}`, at: apiKey.revoked_at }
      : apiKey.status === 'expired'
        ? { text: `Expired ${timeAgo(apiKey.expires_at)}`, at: apiKey.expires_at }
        : { text: `Expires ${timeAgo(apiKey.expires_at)}`, at: apiKey.expires_at }

  return (
    <Paper withBorder p="md" opacity={apiKey.status === 'active' ? 1 : 0.65} aria-label={`API key ${apiKey.name}`}>
      <Stack gap="sm">
        <Group justify="space-between" wrap="nowrap" align="flex-start">
          <div style={{ minWidth: 0 }}>
            <Group gap="xs">
              <Text fw={600}>{apiKey.name}</Text>
              <Badge size="xs" variant="light" color={STATUS_COLOR[apiKey.status]}>
                {apiKey.status}
              </Badge>
            </Group>
            <Code>{maskKey(apiKey.prefix)}</Code>
          </div>
          {apiKey.status === 'active' && <RevokeButton apiKey={apiKey} />}
        </Group>

        <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="sm">
          <Detail label="Permissions">
            <Group gap={4}>
              {apiKey.permissions.scopes.map((s) => (
                <Badge key={s} size="sm" variant="light" leftSection={<IconLock size={10} />}>
                  {scopeLabel(s)}
                </Badge>
              ))}
              <Text size="xs" c="dimmed">
                in
              </Text>
              {apiKey.permissions.projects.map((k) => (
                <Badge key={k} variant="outline" size="sm">
                  {k}
                </Badge>
              ))}
            </Group>
          </Detail>
          <Detail label="Validity">
            <Tooltip label={formatDateTime(validity.at)}>
              <Text size="sm">{validity.text}</Text>
            </Tooltip>
          </Detail>
          <Detail label="Last used">
            <Text size="sm" c={apiKey.last_used_at ? undefined : 'dimmed'}>
              {apiKey.last_used_at ? timeAgo(apiKey.last_used_at) : 'Never'}
            </Text>
          </Detail>
        </SimpleGrid>

        <Notes apiKey={apiKey} />
      </Stack>
    </Paper>
  )
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <Text size="xs" c="dimmed" mb={2}>
        {label}
      </Text>
      {children}
    </div>
  )
}

function Notes({ apiKey }: { apiKey: ApiKey }) {
  const update = useUpdateApiKeyNotes()
  const [draft, setDraft] = useState<string | null>(null) // null = not editing

  if (draft === null) {
    return (
      <Group gap="xs" wrap="nowrap" align="flex-start">
        <Text size="sm" c={apiKey.notes ? undefined : 'dimmed'} style={{ whiteSpace: 'pre-wrap', flex: 1 }}>
          {apiKey.notes || 'No notes'}
        </Text>
        <Button
          size="compact-xs"
          variant="subtle"
          leftSection={<IconPencil size={12} />}
          onClick={() => setDraft(apiKey.notes ?? '')}
          aria-label={`Edit notes for ${apiKey.name}`}
        >
          Edit notes
        </Button>
      </Group>
    )
  }
  const tooLong = draft.length > MAX_NOTES
  return (
    <Stack gap="xs">
      <Textarea
        label="Notes"
        value={draft}
        onChange={(e) => setDraft(e.currentTarget.value)}
        autosize
        minRows={2}
        maxRows={6}
        autoFocus
        error={tooLong ? `Keep notes under ${MAX_NOTES} characters.` : update.error?.message}
      />
      <Group gap="xs" justify="flex-end">
        <Button size="compact-sm" variant="default" onClick={() => setDraft(null)}>
          Cancel
        </Button>
        <Button
          size="compact-sm"
          loading={update.isPending}
          disabled={tooLong}
          onClick={() => update.mutate({ id: apiKey.id, notes: draft.trim() }, { onSuccess: () => setDraft(null) })}
        >
          Save notes
        </Button>
      </Group>
    </Stack>
  )
}

function RevokeButton({ apiKey }: { apiKey: ApiKey }) {
  const revoke = useRevokeApiKey()
  const [confirming, setConfirming] = useState(false)
  if (!confirming) {
    return (
      <Button size="compact-sm" variant="subtle" color="red" onClick={() => setConfirming(true)}>
        Revoke
      </Button>
    )
  }
  return (
    <Stack gap={4} align="flex-end">
      <Group gap={4} wrap="nowrap">
        <Button size="compact-sm" color="red" loading={revoke.isPending} onClick={() => revoke.mutate(apiKey.id)}>
          Yes, revoke
        </Button>
        <Button size="compact-sm" variant="default" onClick={() => setConfirming(false)}>
          Cancel
        </Button>
      </Group>
      {revoke.error && (
        <Text size="xs" c="red">
          {revoke.error.message}
        </Text>
      )}
    </Stack>
  )
}
