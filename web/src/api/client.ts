import createClient, { type Middleware } from 'openapi-fetch'
import type { paths } from './schema'

const CSRF_COOKIE = 'csrftoken'
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

function readCookie(name: string): string | undefined {
  return document.cookie
    .split('; ')
    .find((c) => c.startsWith(`${name}=`))
    ?.slice(name.length + 1)
}

/** The server hands out the CSRF cookie on any response; make sure we have one before a write. */
async function csrfToken(): Promise<string | undefined> {
  if (!readCookie(CSRF_COOKIE)) await fetch('/api/health', { credentials: 'same-origin' })
  return readCookie(CSRF_COOKIE)
}

const csrfMiddleware: Middleware = {
  async onRequest({ request }) {
    if (SAFE_METHODS.has(request.method)) return request
    const token = await csrfToken()
    if (token) request.headers.set('X-CSRFToken', token)
    return request
  },
}

// Same origin as the UI (FastAPI serves it; Vite proxies /api in development).
export const api = createClient<paths>({
  baseUrl: window.location.origin,
  credentials: 'same-origin',
  // Resolve fetch per call rather than capturing it at import, so test mocks (msw) can intercept.
  fetch: (request) => globalThis.fetch(request),
})
api.use(csrfMiddleware)

/** One error shape for the whole UI, whatever the server (or the network) produced. */
export class ApiError extends Error {
  readonly status: number
  readonly code?: string
  /** Field name -> message, for 422 validation errors. */
  readonly fieldErrors: Record<string, string>

  constructor(status: number, message: string, code?: string, fieldErrors: Record<string, string> = {}) {
    super(message)
    this.status = status
    this.code = code
    this.fieldErrors = fieldErrors
  }
}

// fastapi-users answers with bare error codes; translate the ones a user can hit.
const AUTH_CODES: Record<string, string> = {
  LOGIN_BAD_CREDENTIALS: 'Incorrect email or password.',
  REGISTER_USER_ALREADY_EXISTS: 'An account with this email already exists. Sign in instead.',
}

type ValidationIssue = { loc: (string | number)[]; msg: string }

export function toApiError(status: number, body: unknown): ApiError {
  const detail = (body as { detail?: unknown } | undefined)?.detail
  const code = (body as { code?: string } | undefined)?.code

  if (Array.isArray(detail)) {
    const fieldErrors: Record<string, string> = {}
    for (const issue of detail as ValidationIssue[]) {
      const field = String(issue.loc[issue.loc.length - 1])
      fieldErrors[field] ??= issue.msg.replace(/^Value error, /, '')
    }
    return new ApiError(status, 'Please fix the highlighted fields.', 'validation', fieldErrors)
  }
  if (typeof detail === 'string') return new ApiError(status, AUTH_CODES[detail] ?? detail, code ?? detail)
  if (detail && typeof detail === 'object' && 'reason' in detail) {
    // e.g. {"code": "REGISTER_INVALID_PASSWORD", "reason": "Password must be at least 12 characters long."}
    const { code: detailCode, reason } = detail as { code: string; reason: string }
    return new ApiError(status, reason, detailCode)
  }
  if (status === 401) return new ApiError(status, 'Your session has expired. Please sign in again.', 'unauthenticated')
  return new ApiError(status, 'Something went wrong. Please try again.')
}

/** Unwrap an openapi-fetch result: return the data or throw an ApiError. */
export async function call<T>(request: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  let result
  try {
    result = await request
  } catch {
    throw new ApiError(0, "Can't reach IdentityHub. Check your connection and try again.", 'network')
  }
  if (!result.response.ok) throw toApiError(result.response.status, result.error)
  return result.data as T
}
