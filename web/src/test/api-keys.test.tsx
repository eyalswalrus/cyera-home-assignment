import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import type { ApiKey } from '../api/hooks'
import { renderApp } from './render'
import { server } from './server'

const existingKey: ApiKey = {
  id: '11111111-1111-4111-8111-111111111111',
  name: 'GitHub Actions',
  notes: 'Runs on every push to infra',
  prefix: 'ihub_ab3K',
  permissions: { version: 1, scopes: ['findings:create'], projects: ['SEC'] },
  status: 'active',
  created_at: new Date(Date.now() - 86_400_000).toISOString(),
  expires_at: new Date(Date.now() + 30 * 86_400_000).toISOString(),
  last_used_at: null,
  revoked_at: null,
}

beforeEach(() => {
  document.cookie = 'csrftoken=test-token; path=/'
  server.use(http.get('*/api/api-keys', () => HttpResponse.json([existingKey])))
})

type User = ReturnType<typeof renderApp>['user']

async function openCreateForm(user: User) {
  await user.click(await screen.findByRole('button', { name: 'Create API key' }))
  await user.type(screen.getByLabelText(/^Name/), 'Nightly scanner')
}

async function chooseProject(user: User, name: string) {
  const [input] = screen.getAllByRole('combobox', { name: /Allowed projects/ })
  await user.click(input)
  await user.click(await screen.findByRole('option', { name }))
}

describe('API keys', () => {
  it('lists keys masked, with their fixed permissions and notes', async () => {
    renderApp('/settings')
    const card = await screen.findByLabelText('API key GitHub Actions')
    expect(within(card).getByText('ihub_ab3K************')).toBeInTheDocument()
    expect(within(card).getByText('Create findings')).toBeInTheDocument()
    expect(within(card).getByText('SEC')).toBeInTheDocument()
    expect(within(card).getByText('Runs on every push to infra')).toBeInTheDocument()
    expect(within(card).getByText('Never')).toBeInTheDocument()
  })

  it('edits notes (the only thing that can change)', async () => {
    let sent: unknown
    server.use(
      http.patch('*/api/api-keys/:id', async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json({ ...existingKey, notes: 'Owned by platform' })
      }),
    )
    const { user } = renderApp('/settings')
    await user.click(await screen.findByRole('button', { name: 'Edit notes for GitHub Actions' }))
    const box = screen.getByRole('textbox', { name: 'Notes' })
    await user.clear(box)
    await user.type(box, 'Owned by platform')
    await user.click(screen.getByRole('button', { name: 'Save notes' }))
    await waitFor(() => expect(sent).toEqual({ notes: 'Owned by platform' }))
  })

  it('creates a scoped, expiring key and shows it exactly once', async () => {
    let sent: unknown
    server.use(
      http.post('*/api/api-keys', async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json(
          {
            ...existingKey,
            id: '2',
            name: 'Nightly scanner',
            permissions: { version: 1, scopes: ['findings:create'], projects: ['OPS'] },
            key: 'ihub_SECRET-value-123',
          },
          { status: 201 },
        )
      }),
    )
    const { user } = renderApp('/settings')
    await openCreateForm(user)
    await chooseProject(user, 'Operations (OPS)')
    await user.click(screen.getAllByRole('combobox', { name: /Expires after/ })[0])
    await user.click(await screen.findByRole('option', { name: '30 days' }))
    await user.click(screen.getByRole('button', { name: 'Create key' }))

    expect(await screen.findByText(/it won't be shown again/)).toBeInTheDocument()
    expect(screen.getAllByText(/ihub_SECRET-value-123/).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Copy key' })).toBeInTheDocument()
    expect(sent).toEqual({
      name: 'Nightly scanner',
      notes: null,
      expires_in_days: 30,
      permissions: { scopes: ['findings:create'], projects: ['OPS'] },
    })

    await user.click(screen.getByRole('button', { name: "I've saved it" }))
    expect(screen.queryByText(/ihub_SECRET-value-123/)).not.toBeInTheDocument()
  })

  it('creates a key for all projects', async () => {
    let sent: unknown
    server.use(
      http.post('*/api/api-keys', async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json(
          {
            ...existingKey,
            id: '3',
            name: 'Nightly scanner',
            permissions: { version: 1, scopes: ['findings:create'], projects: 'all' },
            key: 'ihub_ALL-value-456',
          },
          { status: 201 },
        )
      }),
    )
    const { user } = renderApp('/settings')
    await openCreateForm(user)
    await user.click(screen.getByRole('radio', { name: /All projects/ }))
    expect(screen.queryByRole('combobox', { name: /Allowed projects/ })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Create key' }))

    expect(await screen.findByText(/it won't be shown again/)).toBeInTheDocument()
    expect(sent).toMatchObject({ permissions: { scopes: ['findings:create'], projects: 'all' } })
  })

  it('requires at least one project', async () => {
    let called = false
    server.use(http.post('*/api/api-keys', () => ((called = true), HttpResponse.json({}))))
    const { user } = renderApp('/settings')
    await openCreateForm(user)
    await user.click(screen.getByRole('button', { name: 'Create key' }))
    expect(await screen.findByText('Choose at least one project.')).toBeInTheDocument()
    expect(called).toBe(false)
  })

  it("shows the server's reason on the projects field", async () => {
    server.use(
      http.post('*/api/api-keys', () =>
        HttpResponse.json(
          { detail: "Your Jira account can't create issues in OPS, so an API key can't be allowed to either.", code: 'api_key_projects_not_permitted' },
          { status: 400 },
        ),
      ),
    )
    const { user } = renderApp('/settings')
    await openCreateForm(user)
    await chooseProject(user, 'Operations (OPS)')
    await user.click(screen.getByRole('button', { name: 'Create key' }))
    expect(await screen.findByText(/can't create issues in OPS/)).toBeInTheDocument()
  })

  it('revokes after confirmation', async () => {
    let revoked: string | undefined
    server.use(
      http.delete('*/api/api-keys/:id', ({ params }) => {
        revoked = params.id as string
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { user } = renderApp('/settings')
    await user.click(await screen.findByRole('button', { name: 'Revoke' }))
    expect(revoked).toBeUndefined() // nothing happens without confirming
    await user.click(screen.getByRole('button', { name: 'Yes, revoke' }))
    await waitFor(() => expect(revoked).toBe(existingKey.id))
  })

  it('needs a Jira connection before keys can be created', async () => {
    server.use(http.get('*/api/jira/connection', () => HttpResponse.json({ configured: true, status: 'not_connected' })))
    renderApp('/settings')
    expect(await screen.findByRole('button', { name: 'Create API key' })).toBeDisabled()
    expect(screen.getByText(/Connect Jira first/)).toBeInTheDocument()
  })
})
