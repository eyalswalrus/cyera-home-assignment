import { Paper, Stack, Text, Title } from '@mantine/core'
import { ProjectPicker } from '../components/ProjectPicker'
import { RecentTickets } from '../components/RecentTickets'
import { RequireJira, useRememberedProject } from '../components/RequireJira'

export function RecentTicketsPage() {
  return (
    <RequireJira>
      {({ site }) => (
        <Stack gap="lg" maw={760}>
          <div>
            <Title order={1} size="h2">
              Recent tickets
            </Title>
            <Text c="dimmed" size="sm">
              The 10 newest tickets created from IdentityHub in a project: findings and blog digests, from the app and
              the REST API.
            </Text>
          </div>
          <ProjectTickets cloudId={site?.cloud_id} />
        </Stack>
      )}
    </RequireJira>
  )
}

function ProjectTickets({ cloudId }: { cloudId?: string }) {
  const [project, setProject] = useRememberedProject(cloudId)
  return (
    <Paper withBorder p="lg">
      <Stack>
        <ProjectPicker value={project} onChange={setProject} />
        <RecentTickets project={project} />
      </Stack>
    </Paper>
  )
}
