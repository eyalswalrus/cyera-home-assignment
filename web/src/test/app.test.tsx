import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { toApiError } from '../api/client'
import { renderApp } from './render'
import { activeConnection, server } from './server'

beforeEach(() => {
  document.cookie = 'csrftoken=test-token; path=/'
})

type User = ReturnType<typeof renderApp>['user']

async function pickProject(user: User, name: string) {
  // The Select's label also names its dropdown list; target the input (an ARIA combobox).
  const [input] = await screen.findAllByRole('combobox', { name: 'Jira project' })
  await user.click(input)
  await user.click(await screen.findByRole('option', { name }))
}

const signedOut = http.get('*/api/auth/me', () => HttpResponse.json({ detail: 'Unauthorized' }, { status: 401 }))

describe('authentication', () => {
  it('sends signed-out visitors to the login page', async () => {
    server.use(signedOut)
    const { router } = renderApp('/settings')
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
  })

  it('explains wrong credentials in plain language', async () => {
    server.use(
      signedOut,
      http.post('*/api/auth/login', () => HttpResponse.json({ detail: 'LOGIN_BAD_CREDENTIALS' }, { status: 400 })),
    )
    const { user } = renderApp('/login')
    await user.type(await screen.findByLabelText('Email'), 'alice@example.com')
    await user.type(screen.getByLabelText('Password'), 'not-my-password')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByText('Incorrect email or password.')).toBeInTheDocument()
  })

  it('shows the server password policy on the password field', async () => {
    server.use(
      signedOut,
      http.post('*/api/auth/register', () =>
        HttpResponse.json(
          { detail: { code: 'REGISTER_INVALID_PASSWORD', reason: 'Password must not contain your email address.' } },
          { status: 400 },
        ),
      ),
    )
    const { user } = renderApp('/register')
    await user.type(await screen.findByLabelText('Email'), 'alice@example.com')
    await user.type(screen.getByLabelText('Password'), 'alice-password-123')
    await user.type(screen.getByLabelText('Confirm password'), 'alice-password-123')
    await user.click(screen.getByRole('button', { name: 'Create account' }))
    expect(await screen.findByText('Password must not contain your email address.')).toBeInTheDocument()
  })
})

describe('Jira connection states', () => {
  it('asks to connect Jira, linking to the OAuth flow', async () => {
    server.use(http.get('*/api/jira/connection', () => HttpResponse.json({ configured: true, status: 'not_connected' })))
    renderApp('/')
    const button = await screen.findByRole('link', { name: 'Connect Jira' })
    expect(button).toHaveAttribute('href', '/api/jira/connect')
  })

  it('asks to reconnect when the connection expired', async () => {
    server.use(
      http.get('*/api/jira/connection', () => HttpResponse.json({ ...activeConnection, status: 'needs_reauth' })),
    )
    renderApp('/')
    expect(await screen.findByRole('link', { name: 'Reconnect Jira' })).toHaveAttribute('href', '/api/jira/connect')
  })

  it('explains an OAuth failure from the redirect, then cleans the URL', async () => {
    server.use(http.get('*/api/jira/connection', () => HttpResponse.json({ configured: true, status: 'not_connected' })))
    const { router } = renderApp('/settings?jira_error=access_denied')
    expect(await screen.findByText(/Jira access wasn’t granted/)).toBeInTheDocument()
    await waitFor(() => expect(router.state.location.search).toBe(''))
  })
})

describe('reporting a finding', () => {
  it('creates a ticket', async () => {
    let sent: Record<string, unknown> | undefined
    let csrfHeader: string | null = null
    server.use(
      http.post('*/api/findings', async ({ request }) => {
        sent = (await request.json()) as Record<string, unknown>
        csrfHeader = request.headers.get('X-CSRFToken')
        return HttpResponse.json(
          { key: 'SEC-3', url: 'https://acme.atlassian.net/browse/SEC-3', summary: sent.summary },
          { status: 201 },
        )
      }),
    )
    const { user } = renderApp('/')

    await pickProject(user, 'Security (SEC)')
    await user.type(screen.getByLabelText(/Title/), 'Stale Service Account: svc-deploy-prod')
    await user.type(screen.getByLabelText('Affected identity'), 'svc-deploy-prod')
    await user.click(screen.getByRole('button', { name: 'Create Jira ticket' }))

    expect(await screen.findByText('SEC-3 created in Security')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Open in Jira/ })).toHaveAttribute('target', '_blank')
    expect(sent).toEqual({
      project_key: 'SEC',
      summary: 'Stale Service Account: svc-deploy-prod',
      description: '',
      identity_name: 'svc-deploy-prod',
    })
    expect(csrfHeader).toBe('test-token')
    expect(screen.getByLabelText(/Title/)).toHaveValue('') // form reset for the next finding
  })

  it('validates the title before calling the server', async () => {
    let called = false
    server.use(http.post('*/api/findings', () => ((called = true), HttpResponse.json({}))))
    const { user } = renderApp('/')
    await pickProject(user, 'Security (SEC)')
    await user.click(screen.getByRole('button', { name: 'Create Jira ticket' }))
    expect(await screen.findByText('Give the finding a title.')).toBeInTheDocument()
    expect(called).toBe(false)
  })

  it("shows Jira's reason and keeps what the user typed", async () => {
    server.use(
      http.post('*/api/findings', () =>
        HttpResponse.json(
          { detail: "Your Jira account doesn't have permission to create issues in SEC.", code: 'jira_forbidden' },
          { status: 403 },
        ),
      ),
    )
    const { user } = renderApp('/')
    await pickProject(user, 'Security (SEC)')
    await user.type(screen.getByLabelText(/Title/), 'Over-privileged key')
    await user.click(screen.getByRole('button', { name: 'Create Jira ticket' }))

    const alert = await screen.findByRole('alert', { name: "The ticket wasn't created" })
    expect(within(alert).getByText("Your Jira account doesn't have permission to create issues in SEC.")).toBeInTheDocument()
    expect(screen.getByLabelText(/Title/)).toHaveValue('Over-privileged key')
  })

  it('has no recent-tickets list: that is its own page', async () => {
    renderApp('/')
    expect(await screen.findByRole('heading', { name: 'Report an NHI finding' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Refresh recent tickets' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Newest/)).not.toBeInTheDocument()
  })
})

describe('recent tickets', () => {
  it('lists recent tickets as links that open in a new tab', async () => {
    const { user } = renderApp('/recent')
    await pickProject(user, 'Security (SEC)')
    const link = await screen.findByRole('link', { name: /SEC-2.*Exposed key in CI logs/ })
    expect(link).toHaveAttribute('href', 'https://acme.atlassian.net/browse/SEC-2')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(within(link).getByText('5 minutes ago')).toBeInTheDocument()
  })

  it('keeps the project chosen on the report page', async () => {
    const { user, router } = renderApp('/')
    await pickProject(user, 'Security (SEC)')
    await user.click(screen.getByRole('link', { name: 'Recent tickets' }))
    expect(router.state.location.pathname).toBe('/recent')
    expect(await screen.findByRole('link', { name: /SEC-2/ })).toBeInTheDocument()
  })

  it('flags your ticket that was deleted in Jira instead of dropping it', async () => {
    server.use(
      http.get('*/api/findings/recent', () =>
        HttpResponse.json([
          { key: 'SEC-3', summary: 'Rotated key', url: 'https://acme.atlassian.net/browse/SEC-3', created_at: new Date().toISOString(), deleted: false },
          { key: 'SEC-1', summary: 'Stale account', url: null, created_at: new Date(Date.now() - 86_400_000).toISOString(), deleted: true },
        ]),
      ),
    )
    const { user } = renderApp('/recent')
    await pickProject(user, 'Security (SEC)')
    const deleted = await screen.findByLabelText('SEC-1, deleted in Jira')
    expect(within(deleted).getByText('Deleted in Jira')).toBeInTheDocument()
    expect(within(deleted).getByText('This ticket no longer exists in Jira.')).toBeInTheDocument()
    expect(within(deleted).queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /SEC-3/ })).toHaveAttribute('href', 'https://acme.atlassian.net/browse/SEC-3')
  })
})

describe('navigation', () => {
  it('opens Settings from the account menu, not the top bar', async () => {
    const { user, router } = renderApp('/')
    await screen.findByRole('heading', { name: 'Report an NHI finding' })
    expect(screen.queryByRole('link', { name: 'Settings' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Account menu' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Settings' }))
    expect(router.state.location.pathname).toBe('/settings')
    // The API docs link opens the public API reference only.
    expect(await screen.findByRole('link', { name: 'REST API' })).toHaveAttribute('href', '/api/v1/docs')
  })
})

describe('toApiError', () => {
  it('maps validation errors to fields', () => {
    const error = toApiError(422, {
      detail: [{ loc: ['body', 'summary'], msg: 'Value error, must be a single line' }],
    })
    expect(error.fieldErrors).toEqual({ summary: 'must be a single line' })
  })

  it('keeps the server code for Jira errors', () => {
    const error = toApiError(409, { detail: 'Reconnect Jira to continue.', code: 'jira_reauth_required' })
    expect([error.message, error.code]).toEqual(['Reconnect Jira to continue.', 'jira_reauth_required'])
  })
})
