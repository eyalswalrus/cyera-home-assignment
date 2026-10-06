import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import type { DigestStatus } from '../api/hooks'
import { renderApp } from './render'
import { server } from './server'

const ready: DigestStatus = {
  configured: true,
  unavailable_reason: null,
  bot_account: 'IdentityHub',
  site_url: 'https://acme.atlassian.net',
  summarizer: 'local model llama3.2:3b (Ollama)',
  daily_at_utc: '09:00',
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
      last_error: 'The IdentityHub bot can no longer create issues in OPS. Ask a Jira admin to grant it access.',
      last_ticket: null,
    },
  ],
}

beforeEach(() => {
  document.cookie = 'csrftoken=test-token; path=/'
})

describe('NHI Blog Digest settings', () => {
  it('explains when the server has no digest bot configured', async () => {
    renderApp('/settings')
    expect(await screen.findByText('Not set up on this server')).toBeInTheDocument()
  })

  it('shows who files tickets, the summarizer, and each project’s last ticket or problem', async () => {
    server.use(http.get('*/api/digest', () => HttpResponse.json(ready)))
    renderApp('/settings')

    expect(await screen.findByText(/bot account, not by you/)).toHaveTextContent(
      'Tickets are filed on acme.atlassian.net by the IdentityHub bot account, not by you. Summaries: local model llama3.2:3b (Ollama). Runs daily at 09:00 UTC',
    )
    const sec = screen.getByLabelText('Digest for SEC')
    expect(within(sec).getByRole('link', { name: /SEC-42/ })).toHaveAttribute('target', '_blank')
    const ops = screen.getByLabelText('Digest for OPS')
    expect(within(ops).getByText(/bot can no longer create issues in OPS/)).toBeInTheDocument()
    expect(screen.getByText(/Last run .*: filed in SEC/)).toBeInTheDocument()
  })

  it('saves the chosen projects', async () => {
    let sent: unknown
    server.use(
      http.get('*/api/digest', () => HttpResponse.json({ ...ready, subscriptions: [] })),
      http.get('*/api/digest/projects', () => HttpResponse.json([{ id: '1', key: 'SEC', name: 'Security' }])),
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

  it('runs the digest on demand', async () => {
    let ran = false
    server.use(
      http.get('*/api/digest', () => HttpResponse.json(ready)),
      http.post('*/api/digest/run', () => ((ran = true), HttpResponse.json({ status: 'started' }, { status: 202 }))),
    )
    const { user } = renderApp('/settings')
    await user.click(await screen.findByRole('button', { name: 'Run now' }))
    await waitFor(() => expect(ran).toBe(true))
  })
})
