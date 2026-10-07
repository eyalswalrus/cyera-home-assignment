import { Skeleton } from '@mantine/core'
import { useLocalStorage } from '@mantine/hooks'
import type { ReactNode } from 'react'
import { type JiraConnection, type Project, useJiraConnection, useMe } from '../api/hooks'
import { ErrorState } from './ErrorState'
import { JiraNotReady } from './JiraStatus'

type ActiveConnection = JiraConnection & { status: 'active' }

/** Renders `children` once the user's Jira connection is usable; otherwise explains what to do. */
export function RequireJira({ children }: { children: (connection: ActiveConnection) => ReactNode }) {
  const connection = useJiraConnection()
  if (connection.isPending) return <Skeleton h={400} />
  if (connection.isError) return <ErrorState error={connection.error} onRetry={() => connection.refetch()} />
  if (connection.data.status !== 'active') return <JiraNotReady connection={connection.data} />
  return children(connection.data as ActiveConnection)
}

/** The project the user last worked with, per user and Jira site, remembered in this browser (a
 * convenience, not shared state). Shared by the report and recent-tickets pages, so moving between
 * them keeps the project. Per site, because the same key can be another project, or none,
 * elsewhere. */
export function useRememberedProject(cloudId: string | undefined) {
  const me = useMe()
  return useLocalStorage<Project | null>({
    key: `identityhub.project.${me.data?.id}.${cloudId}`,
    defaultValue: null,
    getInitialValueInEffect: false,
  })
}
