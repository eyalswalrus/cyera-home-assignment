import { useDebouncedValue } from '@mantine/hooks'
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
export type ApiKey = components['schemas']['ApiKeyOut']
export type ApiKeyCreate = components['schemas']['ApiKeyCreate']
export type ApiKeyCreated = components['schemas']['ApiKeyCreated']
export type ApiKeyScope = components['schemas']['Scope']
export type DigestStatus = components['schemas']['DigestStatus']
export type DigestSubscription = components['schemas']['SubscriptionOut']

export const keys = {
  me: ['me'] as const,
  connection: ['jira', 'connection'] as const,
  sites: ['jira', 'sites'] as const,
  projects: (query: string) => ['jira', 'projects', query] as const,
  recent: (projectKey: string) => ['findings', 'recent', projectKey] as const,
  apiKeys: ['api-keys'] as const,
  digest: ['digest'] as const,
  digestProjects: (query: string) => ['digest', 'projects', query] as const,
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

/** Where a project picker gets its options: projects the user can create issues in, or (for the
 * blog digest) those both the user and the digest bot can create issues in. */
export type ProjectSource = 'jira' | 'digest'

export function useProjects(query: string, enabled: boolean, source: ProjectSource = 'jira') {
  return useQuery({
    queryKey: source === 'jira' ? keys.projects(query) : keys.digestProjects(query),
    queryFn: () =>
      source === 'jira'
        ? call(api.GET('/api/jira/projects', { params: { query: { query: query || undefined } } }))
        : call(api.GET('/api/digest/projects', { params: { query: { query: query || undefined } } })),
    enabled,
    staleTime: 60_000,
  })
}

/** Options for a project picker: the first page of projects (loaded once, so typing filters them
 * instantly) merged with Jira's server-side search results (for sites with many projects). */
export function useProjectOptions(search: string, enabled: boolean, source: ProjectSource = 'jira') {
  const [query] = useDebouncedValue(search.trim(), 250)
  const firstPage = useProjects('', enabled, source)
  const searched = useProjects(query, enabled && query !== '', source)
  const merged = new Map<string, Project>()
  for (const p of [...(firstPage.data ?? []), ...(query ? (searched.data ?? []) : [])]) merged.set(p.key, p)
  return {
    projects: [...merged.values()],
    isFetching: firstPage.isFetching || searched.isFetching || query !== search.trim(),
    error: firstPage.error ?? searched.error,
  }
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

// --- API keys --------------------------------------------------------------------------------

export function useApiKeys() {
  return useQuery({ queryKey: keys.apiKeys, queryFn: () => call(api.GET('/api/api-keys')) })
}

export function useCreateApiKey() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: ApiKeyCreate) => call(api.POST('/api/api-keys', { body })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.apiKeys }),
  })
}

export function useUpdateApiKeyNotes() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, notes }: { id: string; notes: string }) =>
      call(api.PATCH('/api/api-keys/{key_id}', { params: { path: { key_id: id } }, body: { notes: notes || null } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.apiKeys }),
  })
}

export function useRevokeApiKey() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => call(api.DELETE('/api/api-keys/{key_id}', { params: { path: { key_id: id } } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.apiKeys }),
  })
}

// --- Blog digest -----------------------------------------------------------------------------

export function useDigest() {
  return useQuery({
    queryKey: keys.digest,
    queryFn: () => call(api.GET('/api/digest')),
    // Poll while a run is in progress so the result shows up without a reload.
    refetchInterval: (query) => (query.state.data?.running ? 2000 : false),
  })
}

export function useSetDigestSubscriptions() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (projectKeys: string[]) =>
      call(api.PUT('/api/digest/subscriptions', { body: { project_keys: projectKeys } })),
    onSuccess: (status) => queryClient.setQueryData(keys.digest, status),
  })
}

export function useRunDigest() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => call(api.POST('/api/digest/run')),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.digest }),
  })
}
