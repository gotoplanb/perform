"""The status page. Server-rendered Jinja, like conduct's admin UI.

Same payload as `GET /status`, laid out for a human. The reason it exists rather
than pointing people at the JSON is SPEC §8's target — "why is it quiet" in
seconds — and JSON in a terminal is not seconds when four buses each carry a
current item, a queue, and a duck level.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from deps import EngineDep, LibraryDep

router = APIRouter(tags=["ui"])


@router.get("/ui", response_class=HTMLResponse)
async def ui(request: Request, engine: EngineDep, library: LibraryDep) -> HTMLResponse:
    payload = engine.status()
    payload["library_warnings"] = library.warnings[-10:]
    return request.app.state.templates.TemplateResponse(
        request, "status.html", {"s": payload}
    )
