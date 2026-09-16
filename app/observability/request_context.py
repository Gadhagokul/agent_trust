# app\observability\request_context.py
import uuid
from contextvars import ContextVar

from fastapi import Request

request_id_context: ContextVar[str] = ContextVar("request_id", default="unknown")


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request_id_context.set(request_id)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        await self.app(scope, receive, send_wrapper)
