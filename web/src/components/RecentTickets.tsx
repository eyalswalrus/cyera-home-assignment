import { ActionIcon, Alert, Anchor, Badge, Box, Group, Skeleton, Stack, Text, Tooltip } from '@mantine/core'
import { IconExternalLink, IconRefresh, IconTrashX } from '@tabler/icons-react'
import type { ReactNode } from 'react'
import { type Project, type RecentTicket, useRecentTickets } from '../api/hooks'
import { formatDateTime, timeAgo } from '../lib/time'

export function RecentTickets({ project }: { project: Project | null }) {
  const recent = useRecentTickets(project?.key ?? null)

  return (
    <Stack gap="sm">
      <Group justify="space-between" wrap="nowrap">
        <Text size="sm" fw={600}>
          {project ? `Newest in ${project.name}` : 'Newest tickets'}
        </Text>
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
            <li key={ticket.key}>{ticket.deleted ? <DeletedTicket ticket={ticket} /> : <TicketLink ticket={ticket} />}</li>
          ))}
        </Stack>
      )}
    </Stack>
  )
}

function TicketHeader({ ticket, children }: { ticket: RecentTicket; children: ReactNode }) {
  return (
    <Group gap="xs" wrap="nowrap" justify="space-between">
      <Badge variant="light" color={ticket.deleted ? 'gray' : undefined} td={ticket.deleted ? 'line-through' : undefined}>
        {ticket.key}
      </Badge>
      <Group gap={6} wrap="nowrap">
        <Tooltip label={formatDateTime(ticket.created_at)}>
          <Text size="xs" c="dimmed">
            {timeAgo(ticket.created_at)}
          </Text>
        </Tooltip>
        {children}
      </Group>
    </Group>
  )
}

function TicketLink({ ticket }: { ticket: RecentTicket }) {
  return (
    <Anchor
      href={ticket.url ?? undefined}
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
      <TicketHeader ticket={ticket}>
        <IconExternalLink size={14} aria-label="Opens in a new tab" />
      </TicketHeader>
      <Text size="sm" mt={4} lineClamp={2} title={ticket.summary}>
        {ticket.summary}
      </Text>
    </Anchor>
  )
}

/** A ticket you created that Jira no longer returns: kept in the list so a finding can't silently
 * disappear, clearly marked, and without a link since there is nothing to open. */
function DeletedTicket({ ticket }: { ticket: RecentTicket }) {
  return (
    <Box
      p="xs"
      aria-label={`${ticket.key}, deleted in Jira`}
      style={{
        borderRadius: 'var(--mantine-radius-sm)',
        borderLeft: '3px solid var(--mantine-color-red-6)',
        background: 'var(--mantine-color-red-light)',
      }}
    >
      <TicketHeader ticket={ticket}>
        <Tooltip
          label="You created this ticket, but Jira no longer has it. It was deleted, moved to another project, or you lost access to it."
          multiline
          w={260}
        >
          <Badge size="sm" color="red" variant="filled" leftSection={<IconTrashX size={12} />}>
            Deleted in Jira
          </Badge>
        </Tooltip>
      </TicketHeader>
      <Text size="sm" mt={4} lineClamp={2} c="dimmed" td="line-through" title={ticket.summary}>
        {ticket.summary}
      </Text>
      <Text size="xs" c="red" mt={2}>
        This ticket no longer exists in Jira.
      </Text>
    </Box>
  )
}
