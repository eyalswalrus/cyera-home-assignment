import { ActionIcon, Alert, Anchor, Badge, Group, Skeleton, Stack, Text, Tooltip } from '@mantine/core'
import { IconExternalLink, IconRefresh } from '@tabler/icons-react'
import { type Project, useRecentTickets } from '../api/hooks'
import { formatDateTime, timeAgo } from '../lib/time'

export function RecentTickets({ project }: { project: Project | null }) {
  const recent = useRecentTickets(project?.key ?? null)

  return (
    <Stack gap="sm">
      <Group justify="space-between" wrap="nowrap">
        <div>
          <Text fw={600}>Recent tickets</Text>
          <Text size="xs" c="dimmed">
            {project ? `Created from IdentityHub in ${project.name}` : 'Created from IdentityHub'}
          </Text>
        </div>
        {project && (
          <Tooltip label="Refresh">
            <ActionIcon
              variant="subtle"
              aria-label="Refresh recent tickets"
              onClick={() => recent.refetch()}
              loading={recent.isFetching}
            >
              <IconRefresh size={16} />
            </ActionIcon>
          </Tooltip>
        )}
      </Group>

      {!project ? (
        <Text size="sm" c="dimmed">
          Choose a project to see its recent tickets.
        </Text>
      ) : recent.isPending ? (
        <Stack gap="xs">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} h={44} />
          ))}
        </Stack>
      ) : recent.isError ? (
        <Alert color="red">{recent.error.message}</Alert>
      ) : recent.data.length === 0 ? (
        <Text size="sm" c="dimmed">
          No tickets have been created from IdentityHub in this project yet.
        </Text>
      ) : (
        <Stack gap={4} component="ul" p={0} m={0} style={{ listStyle: 'none' }}>
          {recent.data.map((ticket) => (
            <li key={ticket.key}>
              <Anchor
                href={ticket.url}
                target="_blank"
                rel="noopener noreferrer"
                underline="never"
                c="inherit"
                display="block"
                p="xs"
                style={{ borderRadius: 'var(--mantine-radius-sm)' }}
                className="ticket-row"
              >
                {/* The title gets its own line: it's what people scan for. */}
                <Group gap="xs" wrap="nowrap" justify="space-between">
                  <Badge variant="light">{ticket.key}</Badge>
                  <Group gap={6} wrap="nowrap">
                    <Tooltip label={formatDateTime(ticket.created_at)}>
                      <Text size="xs" c="dimmed">
                        {timeAgo(ticket.created_at)}
                      </Text>
                    </Tooltip>
                    <IconExternalLink size={14} aria-label="Opens in a new tab" />
                  </Group>
                </Group>
                <Text size="sm" mt={4} lineClamp={2} title={ticket.summary}>
                  {ticket.summary}
                </Text>
              </Anchor>
            </li>
          ))}
        </Stack>
      )}
    </Stack>
  )
}
