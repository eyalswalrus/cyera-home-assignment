import { Anchor, Grid, Paper, Skeleton, Stack, Text, Title } from '@mantine/core'
import { useLocalStorage } from '@mantine/hooks'
import { type Project, useJiraConnection, useMe } from '../api/hooks'
import { ErrorState } from '../components/ErrorState'
import { FindingForm } from '../components/FindingForm'
import { JiraNotReady } from '../components/JiraStatus'
import { ProjectPicker } from '../components/ProjectPicker'
import { RecentTickets } from '../components/RecentTickets'

export function DashboardPage() {
  const connection = useJiraConnection()

  if (connection.isPending) return <Skeleton h={400} />
  if (connection.isError) return <ErrorState error={connection.error} onRetry={() => connection.refetch()} />
  if (connection.data.status !== 'active') return <JiraNotReady connection={connection.data} />

  const { site, account_name } = connection.data
  return (
    <Stack gap="lg">
      <div>
        <Title order={1} size="h2">
          Report an NHI finding
        </Title>
        <Text c="dimmed" size="sm">
          Tickets are created in{' '}
          <Anchor href={site?.url} target="_blank" rel="noopener noreferrer" inherit>
            {site?.name}
          </Anchor>{' '}
          as {account_name ?? 'you'}.
        </Text>
      </div>
      <ReportFinding cloudId={site?.cloud_id} />
    </Stack>
  )
}

function ReportFinding({ cloudId }: { cloudId?: string }) {
  const me = useMe()
  // Remember the last project per user and Jira site in this browser (a convenience, not shared
  // state). Per site, because the same key can be a different project, or none, on another site.
  const [project, setProject] = useLocalStorage<Project | null>({
    key: `identityhub.project.${me.data?.id}.${cloudId}`,
    defaultValue: null,
    getInitialValueInEffect: false,
  })

  return (
    <Grid gap="lg">
      <Grid.Col span={{ base: 12, md: 7 }}>
        <Paper withBorder p="lg">
          <Stack>
            <ProjectPicker value={project} onChange={setProject} />
            <FindingForm project={project} />
          </Stack>
        </Paper>
      </Grid.Col>
      <Grid.Col span={{ base: 12, md: 5 }}>
        <Paper withBorder p="lg">
          <RecentTickets project={project} />
        </Paper>
      </Grid.Col>
    </Grid>
  )
}
