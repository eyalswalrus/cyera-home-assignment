"""Jira integration errors, each carrying a user-facing message and a stable machine-readable code.

They are raised from anywhere in the Jira layer and turned into JSON responses by one exception
handler (see `app.main`), so the UI and API clients always get `{"detail": ..., "code": ...}`.
"""

from fastapi import status


class JiraError(Exception):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "jira_error"
    message = "Something went wrong while talking to Jira. Please try again."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.message
        super().__init__(self.message)


class JiraNotConfigured(JiraError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "jira_not_configured"
    message = (
        "The Jira integration hasn't been set up on this server yet. An administrator needs to "
        "add the Atlassian OAuth app credentials."
    )


class JiraNotConnected(JiraError):
    status_code = status.HTTP_409_CONFLICT
    code = "jira_not_connected"
    message = "Connect your Jira account first."


class JiraSiteSelectionRequired(JiraError):
    status_code = status.HTTP_409_CONFLICT
    code = "jira_site_required"
    message = "Your Atlassian account has access to several Jira sites. Choose which one to use."


class JiraReauthRequired(JiraError):
    status_code = status.HTTP_409_CONFLICT
    code = "jira_reauth_required"
    message = "Your Jira connection has expired or was revoked. Reconnect Jira to continue."


class JiraNoSites(JiraError):
    status_code = status.HTTP_409_CONFLICT
    code = "jira_no_sites"
    message = "Your Atlassian account doesn't have access to any Jira Cloud site."


class JiraSiteNotFound(JiraError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "jira_site_not_found"
    message = "That Jira site isn't available to your Atlassian account."


class JiraUnavailable(JiraError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "jira_unavailable"
    message = "Jira couldn't be reached. Please try again in a moment."
