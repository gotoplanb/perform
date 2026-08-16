"""The status page.

A static shell. Everything in it is rendered client-side by `static/status.js`
from `GET /status`, which is why this route takes no engine and no library.

It used to render the whole payload server-side with a two-second meta refresh.
That had three problems: it lost scroll position on every tick, it emitted an
access-log line every two seconds forever, and — because this router carries no
auth while `/status` does — it served the full status payload unauthenticated
whenever `PERFORM_TOKEN` was set. Reducing it to a shell removes all three: the
shell contains no data, so there is nothing here to protect, and the fetch it
makes goes through the same auth as any other caller of `/status`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["ui"])


@router.get("/ui", response_class=HTMLResponse)
async def ui(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, "status.html", {})
