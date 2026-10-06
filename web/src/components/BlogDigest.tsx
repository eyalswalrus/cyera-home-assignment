import { Alert, Anchor, Badge, Button, Group, Paper, Skeleton, Stack, Text, Tooltip } from '@mantine/core'
import { IconAlertTriangle, IconExternalLink, IconPlayerPlay, IconRobot } from '@tabler/icons-react'
import { useState } from 'react'
import {
  type DigestStatus,
  type DigestSubscription,
  useDigest,
  useRunDigest,
  useSetDigestSubscriptions,
} from '../api/hooks'
import { formatDateTime, timeAgo } from '../lib/time'
import { ErrorState } from './ErrorState'
import { ProjectMultiSelect } from './ProjectMultiSelect'

/** The NHI Blog Digest part of Settings: which projects receive it, and how it's doing. */
export function BlogDigestSection() {
  const digest = useDigest()
  if (digest.isPending) return <Skeleton h={120} />
  if (digest.isError) return <ErrorState error={digest.error} onRetry={() => digest.refetch()} />

  const status = digest.data
  if (!status.configured) {
    return (
      <Alert color="gray" title="Not set up on this server">
        An administrator needs to configure the digest bot account (DIGEST_JIRA_* settings, see the README) before
        projects can receive the digest.
      </Alert>
    )
  }
  return (
    <Stack>
      <About status={status} />
      {status.unavailable_reason && (
        <Alert color="yellow" icon={<IconAlertTriangle />} title="You can't change subscriptions right now">
          {status.unavailable_reason}
        </Alert>
      )}
      <SubscriptionEditor
        key={status.subscriptions.map((s) => s.project_key).join(',')} // reset when saved state changes
        status={status}
      />
      {status.subscriptions.length > 0 && <SubscriptionList subscriptions={status.subscriptions} />}
      <LastRun status={status} />
    </Stack>
  )
}

function About({ status }: { status: DigestStatus }) {
  const site = status.site_url ? new URL(status.site_url).host : 'Jira'
  return (
    <Group gap="xs" wrap="nowrap" align="flex-start">
      <IconRobot size={18} style={{ flexShrink: 0, marginTop: 2 }} />
      <Text size="sm">
        Tickets are filed on {site} by{' '}
        {status.bot_account ? (
          <>
            the <strong>{status.bot_account}</strong> bot account
          </>
        ) : (
          'the digest bot account'
        )}
        , not by you. Summaries: {status.summarizer ?? 'unknown'}. Runs daily at {status.daily_at_utc} UTC
        {status.next_run_at && <> (next: {formatDateTime(status.next_run_at)})</>}.
      </Text>
    </Group>
  )
}

function SubscriptionEditor({ status }: { status: DigestStatus }) {
  const saved = status.subscriptions.map((s) => s.project_key)
  const [projects, setProjects] = useState<string[]>(saved)
  const save = useSetDigestSubscriptions()
  const changed = projects.length !== saved.length || projects.some((k) => !saved.includes(k))
  const disabled = Boolean(status.unavailable_reason)

  return (
    <Stack gap="xs">
      {disabled ? (
        <Text size="sm" c="dimmed">
          Subscribed projects: {saved.length ? saved.join(', ') : 'none'}
        </Text>
      ) : (
        <ProjectMultiSelect
          label="Projects that receive the digest"
          description="Only projects that both you and the bot can create issues in are listed. A new subscription receives posts published after you subscribe."
          source="digest"
          maxValues={20}
          value={projects}
          onChange={setProjects}
          knownProjects={status.subscriptions.map((s) => ({ key: s.project_key, name: s.project_name }))}
          error={save.error?.message}
        />
      )}
      {changed && !disabled && (
        <Group gap="xs">
          <Button size="xs" loading={save.isPending} onClick={() => save.mutate(projects)}>
            Save projects
          </Button>
          <Button size="xs" variant="default" onClick={() => setProjects(saved)}>
            Cancel
          </Button>
        </Group>
      )}
    </Stack>
  )
}

function SubscriptionList({ subscriptions }: { subscriptions: DigestSubscription[] }) {
  return (
    <Stack gap={6} component="ul" p={0} m={0} style={{ listStyle: 'none' }}>
      {subscriptions.map((sub) => (
        <Paper component="li" key={sub.project_key} withBorder p="xs" aria-label={`Digest for ${sub.project_key}`}>
          <Group justify="space-between" wrap="nowrap" gap="xs">
            <Text size="sm" fw={500}>
              {sub.project_name} <Badge variant="outline" size="xs">{sub.project_key}</Badge>
            </Text>
            {sub.last_ticket ? (
              <Tooltip label={`${sub.last_ticket.post_title} · ${formatDateTime(sub.last_ticket.created_at)}`}>
                <Anchor href={sub.last_ticket.url} target="_blank" rel="noopener noreferrer" size="sm">
                  {sub.last_ticket.key}, {timeAgo(sub.last_ticket.created_at)} <IconExternalLink size={12} />
                </Anchor>
              </Tooltip>
            ) : (
              <Text size="sm" c="dimmed">
                No digest filed yet
              </Text>
            )}
          </Group>
          {sub.last_error && (
            <Text size="xs" c="red" mt={4}>
              {sub.last_error}
            </Text>
          )}
        </Paper>
      ))}
    </Stack>
  )
}

function LastRun({ status }: { status: DigestStatus }) {
  const run = useRunDigest()
  const last = status.last_run
  return (
    <Group justify="space-between" align="flex-start" wrap="nowrap">
      <Text size="sm" c="dimmed">
        {status.running ? (
          'Running now…'
        ) : last ? (
          <>
            Last run {timeAgo(last.finished_at)}: {last.outcome}
            {last.post_title && last.post_url && (
              <>
                {' '}
                (
                <Anchor href={last.post_url} target="_blank" rel="noopener noreferrer" inherit>
                  {last.post_title}
                </Anchor>
                )
              </>
            )}
            {last.error && (
              <Text span c="red" inherit display="block">
                {last.error}
              </Text>
            )}
          </>
        ) : (
          'No run since the server started.'
        )}
      </Text>
      <Tooltip label="Already-filed posts are skipped, so this never creates duplicates">
        <Button
          size="xs"
          variant="light"
          leftSection={<IconPlayerPlay size={14} />}
          loading={status.running || run.isPending}
          disabled={status.subscriptions.length === 0}
          onClick={() => run.mutate()}
          style={{ flexShrink: 0 }}
        >
          Run now
        </Button>
      </Tooltip>
      {run.error && (
        <Text size="xs" c="red">
          {run.error.message}
        </Text>
      )}
    </Group>
  )
}
