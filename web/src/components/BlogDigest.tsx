import { Alert, Anchor, Badge, Button, Group, Paper, Skeleton, Stack, Text, Tooltip } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconAlertTriangle, IconExternalLink, IconSend } from '@tabler/icons-react'
import { useState } from 'react'
import {
  type DigestStatus,
  type DigestSubscription,
  useDigest,
  useSendLatestDigest,
  useSetDigestSubscriptions,
} from '../api/hooks'
import { formatDateTime, timeAgo } from '../lib/time'
import { ErrorState } from './ErrorState'
import { ProjectMultiSelect } from './ProjectMultiSelect'

/** The NHI Blog Digest part of Settings: which projects receive it, and their latest tickets.
 * How the server runs it (summarizer, schedule) is an administrator's concern and stays in the
 * API and the README. */
export function BlogDigestSection() {
  const digest = useDigest()
  if (digest.isPending) return <Skeleton h={120} />
  if (digest.isError) return <ErrorState error={digest.error} onRetry={() => digest.refetch()} />

  const status = digest.data
  if (!status.configured) {
    return (
      <Alert color="gray" title="Not available on this server">
        The digest files tickets through the Jira integration, which an administrator hasn't configured yet.
      </Alert>
    )
  }
  return (
    <Stack>
      {status.unavailable_reason && (
        <Alert color="yellow" icon={<IconAlertTriangle />} title="You can't change subscriptions right now">
          {status.unavailable_reason}
        </Alert>
      )}
      <SubscriptionEditor
        key={status.subscriptions.map((s) => s.project_key).join(',')} // reset when saved state changes
        status={status}
      />
      {status.subscriptions.length > 0 && (
        <SubscriptionList subscriptions={status.subscriptions} canSend={!status.unavailable_reason} />
      )}
    </Stack>
  )
}

function SubscriptionEditor({ status }: { status: DigestStatus }) {
  const saved = status.subscriptions.map((s) => s.project_key)
  const [projects, setProjects] = useState<string[]>(saved)
  const save = useSetDigestSubscriptions()
  const changed = projects.length !== saved.length || projects.some((k) => !saved.includes(k))

  if (status.unavailable_reason) {
    return (
      <Text size="sm" c="dimmed">
        Subscribed projects: {saved.length ? saved.join(', ') : 'none'}
      </Text>
    )
  }
  return (
    <Stack gap="xs">
      <ProjectMultiSelect
        label="Projects that receive the digest"
        maxValues={20}
        value={projects}
        onChange={setProjects}
        knownProjects={status.subscriptions.map((s) => ({ key: s.project_key, name: s.project_name }))}
        error={save.error?.message}
      />
      {changed && (
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

function SubscriptionList({ subscriptions, canSend }: { subscriptions: DigestSubscription[]; canSend: boolean }) {
  return (
    <Stack gap={6} component="ul" p={0} m={0} style={{ listStyle: 'none' }}>
      {subscriptions.map((sub) => (
        <Paper component="li" key={sub.project_key} withBorder p="xs" aria-label={`Digest for ${sub.project_key}`}>
          <Group justify="space-between" wrap="nowrap" gap="xs">
            <Stack gap={2} style={{ minWidth: 0 }}>
              <Text size="sm" fw={500} truncate>
                {sub.project_name} <Badge variant="outline" size="xs">{sub.project_key}</Badge>
              </Text>
              {sub.last_ticket ? (
                <Tooltip label={`${sub.last_ticket.post_title} · ${formatDateTime(sub.last_ticket.created_at)}`}>
                  <Anchor href={sub.last_ticket.url} target="_blank" rel="noopener noreferrer" size="xs">
                    Latest: {sub.last_ticket.key}, {timeAgo(sub.last_ticket.created_at)} <IconExternalLink size={11} />
                  </Anchor>
                </Tooltip>
              ) : (
                <Text size="xs" c="dimmed">
                  No digest filed yet
                </Text>
              )}
            </Stack>
            {canSend && <SendLatestButton subscription={sub} />}
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

function SendLatestButton({ subscription }: { subscription: DigestSubscription }) {
  const send = useSendLatestDigest()
  const { project_key: key, project_name: name } = subscription

  const onClick = () =>
    send.mutate(key, {
      onSuccess: ({ filed, ticket }) =>
        notifications.show({
          color: filed ? 'green' : 'blue',
          title: filed ? `${ticket.key} created in ${name}` : `The latest post is already in ${name}`,
          message: (
            <Anchor href={ticket.url} target="_blank" rel="noopener noreferrer" size="sm">
              {ticket.key}: {ticket.post_title} <IconExternalLink size={12} />
            </Anchor>
          ),
          autoClose: 8000,
        }),
      onError: (error) =>
        notifications.show({ color: 'red', title: `Couldn't send the digest to ${name}`, message: error.message }),
    })

  return (
    <Tooltip label="File the blog's latest post in this project now">
      <Button
        size="xs"
        variant="light"
        leftSection={<IconSend size={14} />}
        loading={send.isPending}
        onClick={onClick}
        style={{ flexShrink: 0 }}
        aria-label={`Send the latest post to ${key}`}
      >
        Send latest post
      </Button>
    </Tooltip>
  )
}
