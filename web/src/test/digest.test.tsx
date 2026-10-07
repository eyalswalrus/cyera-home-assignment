import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import type { DigestStatus } from '../api/hooks'
import { renderApp } from './render'
import { server } from './server'

const ready: DigestStatus = {
  configured: true,
  unavailable_reason: null,
  summarizer: 'local model llama3.2:3b (Ollama)',
  daily_at_utc: '09:00',
  jitter_minutes: 30,
  next_run_at: new Date(Date.now() + 3_600_000).toISOString(),
  running: false,
  last_run: {
    finished_at: new Date(Date.now() - 3_600_000).toISOString(),
    outcome: 'filed in SEC',
    post_title: 'When Shai-Hulud Steals Your Keys',
    post_url: 'https://www.oasis.security/blog/shai-hulud',
    error: null,
  },
  subscriptions: [
    {
      project_key: 'SEC',
      project_name: 'Security',
      last_error: null,
      last_ticket: {
        key: 'SEC-42',
        url: 'https://acme.atlassian.net/browse/SEC-42',
        post_title: 'When Shai-Hulud Steals Your Keys',
        created_at: new Date(Date.now() - 3_600_000).toISOString(),
      },
    },
    {
      project_key: 'OPS',
      project_name: 'Operations',
      last_error: 'You can no longer create issues in OPS, so the digest wasn’t filed there.',
      last_ticket: null,
    },
  ],
}

beforeEach(() => {
  document.cookie = 'csrftoken=test-token; path=/'
})

describe('NHI Blog Digest settings', () => {
  it('explains when the server has no Jira integration configured', async () => {
    renderApp('/settings')
    expect(await screen.findByText('Not available on this server')).toBeInTheDocument()
  })

  it('shows each project’s latest ticket or problem, and none of the server details', async () => {
    server.use(http.get('*/api/digest', () => HttpResponse.json(ready)))
    renderApp('/settings')

    const sec = await screen.findByLabelText('Digest for SEC')
    expect(within(sec).getByRole('link', { name: /SEC-42/ })).toHaveAttribute('target', '_blank')
    const ops = screen.getByLabelText('Digest for OPS')
    expect(within(ops).getByText(/You can no longer create issues in OPS/)).toBeInTheDocument()
    // How the server runs the digest is for administrators, not subscribers.
    expect(screen.queryByText(/llama3\.2|09:00 UTC|Last run/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Run now' })).not.toBeInTheDocument()
  })

  it('saves the chosen projects', async () => {
    let sent: unknown
    server.use(
      http.get('*/api/digest', () => HttpResponse.json({ ...ready, subscriptions: [] })),
      http.get('*/api/jira/projects', () => HttpResponse.json([{ id: '1', key: 'SEC', name: 'Security' }])),
      http.put('*/api/digest/subscriptions', async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json({ ...ready, subscriptions: [ready.subscriptions[0]] })
      }),
    )
    const { user } = renderApp('/settings')
    const [input] = await screen.findAllByRole('combobox', { name: /Projects that receive the digest/ })
    await user.click(input)
    await user.click(await screen.findByRole('option', { name: 'Security (SEC)' }))
    await user.click(screen.getByRole('button', { name: 'Save projects' }))
    await waitFor(() => expect(sent).toEqual({ project_keys: ['SEC'] }))
  })

  it('explains why subscriptions are locked', async () => {
    server.use(
      http.get('*/api/digest', () =>
        HttpResponse.json({ ...ready, unavailable_reason: 'Reconnect Jira to continue.' }),
      ),
    )
    renderApp('/settings')
    expect(await screen.findByText('Reconnect Jira to continue.')).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: /Projects that receive the digest/ })).not.toBeInTheDocument()
  })

  it('sends the latest post to a project on demand', async () => {
    let sentTo: string | undefined
    server.use(
      http.get('*/api/digest', () => HttpResponse.json(ready)),
      http.post('*/api/digest/subscriptions/:key/send-latest', ({ params }) => {
        sentTo = params.key as string
        return HttpResponse.json({ filed: 1, ticket: { ...ready.subscriptions[0].last_ticket, key: 'SEC-43' } })
      }),
    )
    const { user } = renderApp('/settings')
    await user.click(await screen.findByRole('button', { name: 'Send the latest post to SEC' }))
    expect(await screen.findByText('SEC-43 created in Security')).toBeInTheDocument()
    expect(sentTo).toBe('SEC')
  })

  it('says so when the latest post is already in the project', async () => {
    server.use(
      http.get('*/api/digest', () => HttpResponse.json(ready)),
      http.post('*/api/digest/subscriptions/:key/send-latest', () =>
        HttpResponse.json({ filed: 0, ticket: ready.subscriptions[0].last_ticket }),
      ),
    )
    const { user } = renderApp('/settings')
    await user.click(await screen.findByRole('button', { name: 'Send the latest post to SEC' }))
    expect(await screen.findByText('The latest post is already in Security')).toBeInTheDocument()
  })
})
