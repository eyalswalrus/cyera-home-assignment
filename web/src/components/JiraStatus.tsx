import { Alert, Button, Group, Paper, Stack, Text, ThemeIcon, Title } from '@mantine/core'
import { IconAlertTriangle, IconPlugConnected, IconSettings } from '@tabler/icons-react'
import { Link } from 'react-router'
import type { JiraConnection } from '../api/hooks'
import { JIRA_CONNECT_URL } from '../api/messages'

/** What the user must do before they can report findings, or null when Jira is ready. */
export function JiraNotReady({ connection }: { connection: JiraConnection }) {
  if (!connection.configured) {
    return (
      <Alert color="yellow" icon={<IconAlertTriangle />} title="Jira integration isn't set up yet">
        An administrator needs to add the Atlassian OAuth app credentials to this server (see the README) before
        anyone can connect Jira.
      </Alert>
    )
  }
  switch (connection.status) {
    case 'not_connected':
      return (
        <Paper withBorder p="xl" maw={560} mx="auto" mt="xl">
          <Stack align="center" ta="center">
            <ThemeIcon size={48} radius="xl" variant="light">
              <IconPlugConnected />
            </ThemeIcon>
            <Title order={2} size="h3">
              Connect your Jira account
            </Title>
            <Text c="dimmed">
              IdentityHub creates tickets as you, in projects your Jira account can access. You'll be sent to
              Atlassian to approve access, then brought back here.
            </Text>
            <Button component="a" href={JIRA_CONNECT_URL} size="md">
              Connect Jira
            </Button>
          </Stack>
        </Paper>
      )
    case 'needs_site':
      return (
        <Alert color="blue" title="Choose a Jira site">
          Your Atlassian account can access several Jira sites. Pick the one IdentityHub should use.
          <Group mt="sm">
            <Button component={Link} to="/settings" size="xs" leftSection={<IconSettings size={14} />}>
              Choose a site
            </Button>
          </Group>
        </Alert>
      )
    case 'needs_reauth':
      return (
        <Alert color="orange" icon={<IconAlertTriangle />} title="Reconnect Jira">
          Your Jira connection has expired or was revoked. Reconnect to keep reporting findings.
          <Group mt="sm">
            <Button component="a" href={JIRA_CONNECT_URL} size="xs" color="orange">
              Reconnect Jira
            </Button>
          </Group>
        </Alert>
      )
    default:
      return null
  }
}
