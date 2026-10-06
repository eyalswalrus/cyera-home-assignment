"""HTTP security headers, via the `secure` library.

Two policies: a strict one for the app and API, and a relaxed one for the interactive API docs,
whose Swagger UI page loads its assets from a CDN and bootstraps with an inline script.
"""

from secure import (
    ContentSecurityPolicy,
    CrossOriginOpenerPolicy,
    PermissionsPolicy,
    ReferrerPolicy,
    Secure,
    Server,
    StrictTransportSecurity,
    XContentTypeOptions,
    XFrameOptions,
)
from secure.middleware import SecureASGIMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

DOCS_PATHS = ("/docs", "/redoc")
SWAGGER_CDN = "https://cdn.jsdelivr.net"


def _csp(*, https: bool, docs: bool) -> ContentSecurityPolicy:
    csp = (
        ContentSecurityPolicy()
        .default_src("'self'")
        .base_uri("'self'")
        .connect_src("'self'")
        .font_src("'self'", "data:")
        .form_action("'self'")
        .frame_ancestors("'none'")
        .img_src("'self'", "data:")
        .object_src("'none'")
        # Component libraries set inline style attributes; inline *scripts* stay blocked.
        .style_src("'self'", "'unsafe-inline'")
        .script_src("'self'")
    )
    if docs:
        csp = (
            csp.script_src("'self'", "'unsafe-inline'", SWAGGER_CDN)
            .style_src("'self'", "'unsafe-inline'", SWAGGER_CDN)
            .img_src("'self'", "data:", "https://fastapi.tiangolo.com")
        )
    if https:
        csp = csp.upgrade_insecure_requests()
    return csp


def _secure(*, https: bool, docs: bool) -> Secure:
    return Secure(
        csp=_csp(https=https, docs=docs),
        coop=CrossOriginOpenerPolicy().same_origin(),
        # HSTS is ignored over plain HTTP, so only send it when serving HTTPS.
        hsts=StrictTransportSecurity().max_age(31536000).include_subdomains() if https else None,
        permissions=PermissionsPolicy().geolocation().microphone().camera(),
        referrer=ReferrerPolicy().strict_origin_when_cross_origin(),
        server=Server().set(""),
        xcto=XContentTypeOptions().nosniff(),
        xfo=XFrameOptions().deny(),
    )


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, https: bool) -> None:
        self.default = SecureASGIMiddleware(app, secure=_secure(https=https, docs=False))
        self.docs = SecureASGIMiddleware(app, secure=_secure(https=https, docs=True))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        is_docs = scope["type"] == "http" and scope["path"].startswith(DOCS_PATHS)
        await (self.docs if is_docs else self.default)(scope, receive, send)
