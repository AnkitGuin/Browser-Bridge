from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from . import api, config, db, telemetry, web
from .errors import BridgeError

telemetry.configure("browserbridge-api")

app = FastAPI(
    title="BrowserBridge", version="1.0.0", docs_url="/docs" if config.DEMO else None, redoc_url=None
)
app.include_router(api.router)
app.include_router(web.router)
app.mount("/assets", StaticFiles(directory=Path(__file__).parent / "static"), name="portal-assets")
if config.CONSOLE.is_dir():
    app.mount(
        "/console-assets", StaticFiles(directory=config.CONSOLE / "console-assets"), name="console-assets"
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    request.state.correlation_id = uuid4().hex
    with telemetry.tracer.start_as_current_span("http.request") as span:
        span.set_attribute("http.request.method", request.method)
        span.set_attribute("browserbridge.correlation_id", request.state.correlation_id)
        response = await call_next(request)
        span.set_attribute("http.response.status_code", response.status_code)
    response.headers["X-Request-ID"] = request.state.correlation_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers.setdefault("Cache-Control", "no-store")
    if request.url.path not in {"/docs", "/docs/oauth2-redirect"}:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
    return response


@app.exception_handler(BridgeError)
async def business_error(request, exc):
    if request.url.path.startswith(("/support", "/crm", "/inventory")):
        response = web.render(
            request,
            "error.html",
            title="Sign in required" if exc.status == 401 else "Request could not be completed",
            code=exc.code,
            message=exc.message,
        )
        response.status_code = exc.status
        return response
    return JSONResponse(
        {"code": exc.code, "message": exc.message, "correlation_id": request.state.correlation_id},
        status_code=exc.status,
    )


@app.exception_handler(RequestValidationError)
async def invalid(request, exc):
    fields = [list(error["loc"]) for error in exc.errors()]
    return JSONResponse(
        {
            "code": "VALIDATION_FAILED",
            "message": "Request fields failed validation",
            "details": fields,
            "correlation_id": request.state.correlation_id,
        },
        status_code=422,
    )


@app.get("/health/live")
def live():
    return {"status": "live"}


@app.get("/health/ready")
def ready():
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM runs LIMIT 1"))
        return {"status": "ready"}
    except SQLAlchemyError:
        return JSONResponse({"status": "database unavailable or unmigrated"}, status_code=503)


@app.get("/health/metrics", include_in_schema=False)
def metrics():
    with db.engine.connect() as conn:
        counts = conn.execute(
            select(db.runs.c.status, func.count())
            .where(~db.runs.c.tenant_id.startswith("case-"))
            .group_by(db.runs.c.status)
        ).all()
    lines = ["# TYPE browserbridge_runs gauge"]
    lines.extend(f'browserbridge_runs{{status="{state}"}} {count}' for state, count in counts)
    return PlainTextResponse("\n".join(lines) + "\n")


@app.get("/")
def index():
    path = config.CONSOLE / "index.html"
    if path.is_file():
        return FileResponse(path)
    return JSONResponse(
        {
            "message": "Build the console with npm ci and npm run build in console/",
            "portals": ["/support", "/crm", "/inventory"],
            "login": "/login",
        },
        status_code=503,
    )
