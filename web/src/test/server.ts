import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import type { JiraConnection, Project, RecentTicket, User } from '../api/hooks'

export const alice: User = {
  id: '6f1c2d3e-0000-4000-8000-000000000001',
  email: 'alice@example.com',
  is_active: true,
  is_superuser: false,
  is_verified: false,
}
export const activeConnection: JiraConnection = {
  configured: true,
  status: 'active',
  site: { cloud_id: 'cloud-a', name: 'acme', url: 'https://acme.atlassian.net' },
  account_name: 'Alice Atlassian',
}
export const projects: Project[] = [
  { id: '1', key: 'SEC', name: 'Security' },
  { id: '2', key: 'OPS', name: 'Operations' },
]
export const tickets: RecentTicket[] = [
  {
    key: 'SEC-2',
    summary: 'Exposed key in CI logs',
    url: 'https://acme.atlassian.net/browse/SEC-2',
    created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
  },
]

/** Default API: Alice is signed in with Jira connected. Tests override what they need. */
export const handlers = [
  http.get('*/api/health', () =>
    HttpResponse.json({ status: 'ok', jira_configured: true }, { headers: { 'Set-Cookie': 'csrftoken=test-token' } }),
  ),
  http.get('*/api/auth/me', () => HttpResponse.json(alice)),
  http.get('*/api/jira/connection', () => HttpResponse.json(activeConnection)),
  http.get('*/api/jira/projects', ({ request }) => {
    const query = new URL(request.url).searchParams.get('query')?.toLowerCase() ?? ''
    return HttpResponse.json(projects.filter((p) => `${p.name} ${p.key}`.toLowerCase().includes(query)))
  }),
  http.get('*/api/findings/recent', () => HttpResponse.json(tickets)),
]

export const server = setupServer(...handlers)
