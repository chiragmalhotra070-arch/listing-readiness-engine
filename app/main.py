from fastapi import FastAPI, HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.routes import router
from app.config import get_settings


class RequestBodySizeLimitMiddleware:
    """Hard request-body cap at the ASGI receive boundary (HTTP 413).

    ``Content-Length`` may be absent (chunked transfer) or unusable, and
    neither ``request.json()`` (which concatenates every chunk) nor
    ``request.form()`` (which spools file parts to disk) bounds the aggregate
    body.  This counts each ``http.request`` message and aborts as soon as the
    running total crosses ``effective_max_request_body_bytes``, so the body
    stops accumulating at the limit instead of being read fully first.  One
    shared boundary covers JSON, multipart and the n8n path; the header check
    in ``enforce_request_body_size`` remains as the earlier fast rejection
    when ``Content-Length`` is present and valid.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = get_settings().effective_max_request_body_bytes()
        seen = 0

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise HTTPException(status_code=413, detail=f"request body exceeds {limit} bytes")
            return message

        await self.app(scope, limited_receive, send)


app = FastAPI(title="Listing Readiness Engine", version="0.1.0")
app.include_router(router, prefix="/v1")
app.add_middleware(RequestBodySizeLimitMiddleware)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
