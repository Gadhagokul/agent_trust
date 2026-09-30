# app\observability\request_context.py
import re
import uuid
from contextvars import ContextVar

from fastapi import Request

request_id_context: ContextVar[str] = ContextVar("request_id", default="unknown")

# Any value a caller may put in x-request-id ends up in JSON logs (via the
# %(request_id)s formatter) and in the echoed response header, so it must be an
# allowlisted token. Anything less (control characters, quotes, path tricks,
# oversized values) falls back to a generated UUID (senior §38 sanitized IDs).
_REQUEST_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        raw = (request.headers.get("x-request-id") or "").strip()
        request_id = raw if _REQUEST_ID_RE.fullmatch(raw) else str(uuid.uuid4())
        request_id_context.set(request_id)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        await self.app(scope, receive, send_wrapper)
