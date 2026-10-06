import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api, call } from './client'
import type { components } from './schema'

export type User = components['schemas']['UserRead']
export type JiraConnection = components['schemas']['ConnectionOut']
export type JiraSite = components['schemas']['SiteOut']
export type Project = components['schemas']['Project']
export type FindingCreate = components['schemas']['FindingCreate']
export type FindingCreated = components['schemas']['FindingCreated']
export type RecentTicket = components['schemas']['RecentTicket']

export const keys = {
  me: ['me'] as const,
  connection: ['jira', 'connection'] as const,
  sites: ['jira', 'sites'] as const,
  projects: (query: string) => ['jira', 'projects', query] as const,
  recent: (projectKey: string) => ['findings', 'recent', projectKey] as const,
}

// --- Session ---------------------------------------------------------------------------------

/** The logged-in user, or null when there is no valid session. */
export function useMe() {
  return useQuery({
    queryKey: keys.me,
    queryFn: async (): Promise<User | null> => {
      try {
        return await call(api.GET('/api/auth/me'))
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null
        throw error
      }
    },
    staleTime: Infinity,
  })
}

export function useLogin() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ email, password }: { email: string; password: string }) =>
      call(
        api.POST('/api/auth/login', {
          // fastapi-users' login takes an OAuth2 password form, not JSON.
          body: { username: email, password, scope: '' },
          bodySerializer: (body) => new URLSearchParams(body as Record<string, string>),
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.me }),
  })
}

export function useRegister() {
  return useMutation({
    mutationFn: ({ email, password }: { email: string; password: string }) =>
      call(api.POST('/api/auth/register', { body: { email, password } })),
  })
}

export function useLogout() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      try {
        await call(api.POST('/api/auth/logout'))
      } catch (error) {
        // Session already expired: the user is signed out either way.
        if (!(error instanceof ApiError && error.status === 401)) throw error
      }
    },
    // Drop every cached response so the next user in this browser starts clean.
    onSettled: () => {
      queryClient.clear()
      queryClient.setQueryData(keys.me, null)
    },
  })
}

// --- Jira connection -------------------------------------------------------------------------

export function useJiraConnection() {
  return useQuery({ queryKey: keys.connection, queryFn: () => call(api.GET('/api/jira/connection')) })
}

export function useJiraSites(enabled: boolean) {
  return useQuery({ queryKey: keys.sites, queryFn: () => call(api.GET('/api/jira/sites')), enabled })
}

export function useSelectSite() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (cloudId: string) => call(api.PUT('/api/jira/connection/site', { body: { cloud_id: cloudId } })),
    onSuccess: (connection) => queryClient.setQueryData(keys.connection, connection),
  })
}

export function useDisconnectJira() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => call(api.DELETE('/api/jira/connection')),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jira'] }),
  })
}

// --- Projects and findings -------------------------------------------------------------------

export function useProjects(query: string, enabled: boolean) {
  return useQuery({
    queryKey: keys.projects(query),
    queryFn: () => call(api.GET('/api/jira/projects', { params: { query: { query: query || undefined } } })),
    enabled,
    staleTime: 60_000,
    placeholderData: (previous) => previous, // keep showing results while the next search loads
  })
}

export function useCreateFinding() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (finding: FindingCreate) => call(api.POST('/api/findings', { body: finding })),
    onSuccess: (_created, finding) =>
      queryClient.invalidateQueries({ queryKey: keys.recent(finding.project_key) }),
    onError: (error) => {
      // e.g. Jira rejected the token mid-request: refresh the banner that says "Reconnect Jira".
      if (error instanceof ApiError && error.code === 'jira_reauth_required') {
        queryClient.invalidateQueries({ queryKey: keys.connection })
      }
    },
  })
}

export function useRecentTickets(projectKey: string | null) {
  return useQuery({
    queryKey: keys.recent(projectKey ?? ''),
    queryFn: () =>
      call(api.GET('/api/findings/recent', { params: { query: { project_key: projectKey! } } })),
    enabled: Boolean(projectKey),
  })
}
