/** Messages for the Jira OAuth result codes the server puts in the redirect URL
 * (`/settings?jira_error=<code>`). Only known codes are shown; anything else gets a generic text. */
const JIRA_CONNECT_ERRORS: Record<string, string> = {
  access_denied: 'Jira access wasn’t granted. Connect again and choose “Accept” to let IdentityHub create tickets.',
  state_mismatch: 'The connection link expired or was opened in another tab. Please try again.',
  wrong_user: 'This Jira connection was started by a different IdentityHub user in this browser. Please try again.',
  not_logged_in: 'Your IdentityHub session ended before Jira finished connecting. Sign in and try again.',
  jira_not_configured:
    'The Jira integration hasn’t been set up on this server yet. An administrator needs to add the Atlassian OAuth app credentials.',
  jira_no_sites: 'Your Atlassian account doesn’t have access to any Jira Cloud site.',
  jira_unavailable: 'Atlassian couldn’t be reached. Please try again in a moment.',
  token_exchange_failed:
    'Atlassian rejected the connection request. If this keeps happening, the server’s Atlassian app credentials may be misconfigured.',
}

export function jiraConnectErrorMessage(code: string): string {
  return JIRA_CONNECT_ERRORS[code] ?? 'Connecting Jira failed. Please try again.'
}

/** Full-page navigation (not fetch): the server redirects to Atlassian's consent screen. */
export const JIRA_CONNECT_URL = '/api/jira/connect'
