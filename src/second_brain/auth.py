"""Owner-managed local bearer credentials; no remote OAuth service is required."""

import logging
import math
import secrets
import time
from collections import OrderedDict, deque
from functools import wraps

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse


class LocalTokens:
    def __init__(self, settings):
        self.write = settings.mcp_write_token.get_secret_value()
        self.read = settings.mcp_read_token.get_secret_value()
        if self.write and len(self.write) < 32:
            raise ValueError("SB_MCP_WRITE_TOKEN must contain at least 32 characters")
        if self.read and (len(self.read) < 32 or self.read == self.write):
            raise ValueError("SB_MCP_READ_TOKEN must be distinct and at least 32 characters")

    async def verify_token(self, token: str) -> AccessToken | None:
        if self.write and secrets.compare_digest(token.encode(), self.write.encode()):
            return AccessToken(
                token=token, client_id="owner", scopes=["memory:read", "memory:write"]
            )
        if self.read and secrets.compare_digest(token.encode(), self.read.encode()):
            return AccessToken(token=token, client_id="reader", scopes=["memory:read"])
        return None


def write_access(function):
    @wraps(function)
    async def guarded(*args, **kwargs):
        token = get_access_token()
        # Stdio and in-process calls inherit the owner's OS permissions.
        if token is not None and "memory:write" not in token.scopes:
            raise PermissionError("This credential only permits memory reads")
        return await function(*args, **kwargs)

    return guarded


class RequestLimits:
    def __init__(self, app, *, per_minute: int, max_inflight: int, token_verifier=None):
        self.app, self.per_minute, self.max_inflight = app, per_minute, max_inflight
        self.token_verifier = token_verifier
        self.clients = OrderedDict()
        self.inflight = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/mcp":
            return await self.app(scope, receive, send)
        client = (scope.get("client") or ("local",))[0]
        principal = "anonymous"
        if self.token_verifier is not None:
            authorization = dict(scope.get("headers", [])).get(b"authorization", b"")
            scheme, _, credential = authorization.partition(b" ")
            if scheme.lower() == b"bearer":
                token = await self.token_verifier.verify_token(credential.decode("latin-1"))
                if token is not None:
                    principal = token.client_id
        client = (client, principal)
        now = time.monotonic()
        requests = self.clients.setdefault(client, deque())
        while requests and now - requests[0] >= 60:
            requests.popleft()
        self.clients.move_to_end(client)
        if len(self.clients) > 1024:
            self.clients.popitem(last=False)
        reason, retry = None, 1
        if len(requests) >= self.per_minute:
            reason = "rate_limit_exceeded"
            retry = max(1, math.ceil(60 - (now - requests[0])))
        elif self.inflight >= self.max_inflight:
            reason = "concurrency_limit_exceeded"
        if reason:
            logging.getLogger(__name__).warning("MCP request throttled: %s", reason)
            response = JSONResponse(
                {"error": reason, "retry_after_seconds": retry},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
            return await response(scope, receive, send)
        # Rejected requests do not spend the remaining rate budget.
        requests.append(now)
        self.inflight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.inflight -= 1


class ProtectedMCP(FastMCP):
    def streamable_http_app(self):
        if not self._token_verifier.write:
            raise ValueError("HTTP requires SB_MCP_WRITE_TOKEN; run ./scripts/init.sh")
        app = super().streamable_http_app()
        app.add_middleware(
            RequestLimits,
            per_minute=self.memory_settings.mcp_requests_per_minute,
            max_inflight=self.memory_settings.mcp_max_inflight,
            token_verifier=self._token_verifier,
        )
        return app

    async def run_streamable_http_async(self):
        import uvicorn

        # This owner-local server has no trusted proxy. A caller cannot spoof its
        # client IP with forwarded headers to evade the per-IP request limit.
        server = uvicorn.Server(
            uvicorn.Config(
                self.streamable_http_app(),
                host=self.settings.host,
                port=self.settings.port,
                log_level=self.settings.log_level.lower(),
                proxy_headers=False,
            )
        )
        await server.serve()
