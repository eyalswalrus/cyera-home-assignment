import { Alert, Anchor, Button, Group, Paper, Radio, Skeleton, Stack, Text, Title } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconCircleCheck } from '@tabler/icons-react'
import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router'
import { type JiraConnection, useDisconnectJira, useJiraConnection, useJiraSites, useSelectSite } from '../api/hooks'
import { JIRA_CONNECT_URL, jiraConnectErrorMessage } from '../api/messages'
import { ErrorState } from '../components/ErrorState'
import { JiraNotReady } from '../components/JiraStatus'

export function SettingsPage() {
  return (
    <Stack gap="lg" maw={720}>
      <Title order={1} size="h2">
        Settings
      </Title>
      <Paper withBorder p="lg">
        <Stack>
          <div>
            <Title order={2} size="h4">
              Jira connection
            </Title>
            <Text size="sm" c="dimmed">
              IdentityHub acts as your Jira account, so it can only see and create tickets where you can.
            </Text>
          </div>
          <OAuthResult />
          <JiraConnectionSection />
        </Stack>
      </Paper>
    </Stack>
  )
}

/** Shows the outcome of the Atlassian redirect (`?jira=...` / `?jira_error=...`) once, then cleans the URL. */
function OAuthResult() {
  const [params, setParams] = useSearchParams()
  const [error] = useState(() => params.get('jira_error'))
  const outcome = params.get('jira')

  useEffect(() => {
    if (outcome === 'connected') notifications.show({ color: 'green', title: 'Jira connected', message: 'You can now report findings.' })
    if (params.has('jira') || params.has('jira_error')) setParams({}, { replace: true })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps -- run once for the landing URL

  if (!error) return null
  return (
    <Alert color="red" title="Jira wasn't connected">
      {jiraConnectErrorMessage(error)}
    </Alert>
  )
}

function JiraConnectionSection() {
  const connection = useJiraConnection()
  if (connection.isPending) return <Skeleton h={80} />
  if (connection.isError) return <ErrorState error={connection.error} onRetry={() => connection.refetch()} />

  const data = connection.data
  if (!data.configured || data.status === 'not_connected' || data.status === 'needs_reauth') {
    return <JiraNotReady connection={data} />
  }
  if (data.status === 'needs_site') return <SiteChooser />
  return <Connected connection={data} />
}

function Connected({ connection }: { connection: JiraConnection }) {
  const disconnect = useDisconnectJira()
  const [confirming, setConfirming] = useState(false)

  return (
    <Stack>
      <Group gap="xs" wrap="nowrap">
        <IconCircleCheck color="var(--mantine-color-green-6)" size={20} />
        <Text>
          Connected to{' '}
          <Anchor href={connection.site?.url} target="_blank" rel="noopener noreferrer">
            {connection.site?.name}
          </Anchor>
          {connection.account_name && (
            <>
              {' '}
              as <strong>{connection.account_name}</strong>
            </>
          )}
        </Text>
      </Group>
      {disconnect.error && <Alert color="red">{disconnect.error.message}</Alert>}
      {confirming ? (
        <Alert color="red" variant="outline" title="Disconnect Jira?">
          IdentityHub will delete its access to your Jira account. Existing tickets stay in Jira.
          <Group mt="sm" gap="xs">
            <Button color="red" size="xs" loading={disconnect.isPending} onClick={() => disconnect.mutate()}>
              Disconnect
            </Button>
            <Button variant="default" size="xs" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </Group>
        </Alert>
      ) : (
        <Group gap="xs">
          <Button component="a" href={JIRA_CONNECT_URL} variant="default">
            Reconnect or switch account
          </Button>
          <Button variant="subtle" color="red" onClick={() => setConfirming(true)}>
            Disconnect
          </Button>
        </Group>
      )}
    </Stack>
  )
}

function SiteChooser() {
  const sites = useJiraSites(true)
  const select = useSelectSite()
  const [choice, setChoice] = useState<string | null>(null)

  if (sites.isPending) return <Skeleton h={80} />
  if (sites.isError) return <ErrorState error={sites.error} onRetry={() => sites.refetch()} />
  return (
    <Stack>
      <Radio.Group
        label="Choose a Jira site"
        description="Your Atlassian account can access several sites. IdentityHub will create tickets in this one."
        value={choice}
        onChange={setChoice}
      >
        <Stack gap="xs" mt="xs">
          {sites.data.map((site) => (
            <Radio key={site.cloud_id} value={site.cloud_id} label={`${site.name} — ${site.url}`} />
          ))}
        </Stack>
      </Radio.Group>
      {select.error && <Alert color="red">{select.error.message}</Alert>}
      <Group>
        <Button disabled={!choice} loading={select.isPending} onClick={() => choice && select.mutate(choice)}>
          Use this site
        </Button>
      </Group>
    </Stack>
  )
}
