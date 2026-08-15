"""Perform — a multichannel playback server.

It renders what it's given, where it's told, in the order it's told. It
coordinates nothing.

Module-scope `app` plus router registration, same as conduct's `main.py`. See
SPEC.md for the architecture and lifespan.py for what happens at boot.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from lifespan import lifespan
from routes import assets, buses, health, items, speak, status, surface, ui

app = FastAPI(
    title="Perform",
    description="Multichannel playback server",
    version="0.1.0",
    lifespan=lifespan,
)

_here = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(_here / "static")), name="static")
app.state.templates = Jinja2Templates(directory=str(_here / "templates"))

app.include_router(health.router)
app.include_router(speak.router)
app.include_router(items.router)
app.include_router(buses.router)
app.include_router(status.router)
app.include_router(assets.router)
app.include_router(surface.router)
app.include_router(ui.router)

# Must run at import time, not in the lifespan: Starlette freezes the middleware
# stack before the lifespan body executes. Conduct hit this and left a comment
# saying so (conduct#49); the same trap applies here.
FastAPIInstrumentor.instrument_app(app)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/ui")
