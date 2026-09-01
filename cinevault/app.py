from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import math
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .config import AppSecrets, ConfigurationError, SecretVault, resolve_data_dir
from .database import Database
from .film_bot import BotRequestError, FilmBotClient
from .indexer import MediaCatalog
from .posters import PosterFetcher
from .ranges import RangeNotSatisfiable, parse_range_header
from .security import LoginRateLimiter, new_token, verify_password


PACKAGE_DIR = Path(__file__).resolve().parent
LIBRARY_SORTS = {"newest", "oldest", "name", "largest", "random"}
MEDIA_KINDS = {"all", "video", "photo", "audio", "file"}
MAX_RANDOM_SEED = 2_147_483_647


def format_bytes(value: int | str | None) -> str:
    size = float(value or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


def format_duration(value: int | str | None) -> str:
    seconds = max(0, int(value or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def format_date(value: str | None) -> str:
    if not value:
        return "Unknown date"
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        return value


def _random_seed(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        seed = int(value)
    except (TypeError, ValueError):
        return None
    return seed if 0 <= seed <= MAX_RANDOM_SEED else None


def _context_query(query: str, kind: str, sort: str, seed: int) -> str:
    values: list[tuple[str, str | int]] = [("kind", kind), ("sort", sort)]
    if query:
        values.insert(0, ("q", query))
    if sort == "random":
        values.append(("seed", seed))
    return urlencode(values)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            try:
                content_length = int(request.headers.get("content-length", "0") or 0)
            except ValueError:
                return Response("Invalid Content-Length", status_code=400)
            if content_length > 16 * 1024:
                return Response("Request too large", status_code=413)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
            "form-action 'self'; object-src 'none'; img-src 'self' data:; "
            "media-src 'self'; style-src 'self'; script-src 'self'",
        )
        return response


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_authenticated(request: Request, config: AppSecrets) -> bool:
    stored = str(request.session.get("user", ""))
    return bool(request.session.get("authenticated")) and secrets.compare_digest(
        stored, config.web_username
    )


def _access_allowed(request: Request, config: AppSecrets) -> bool:
    return config.access_mode == "public" or _is_authenticated(request, config)


def _csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = new_token(24)
        request.session["csrf"] = token
    return str(token)


def _valid_csrf(request: Request, supplied: str) -> bool:
    expected = str(request.session.get("csrf", ""))
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


def _auth_redirect(request: Request, config: AppSecrets) -> RedirectResponse | None:
    if _access_allowed(request, config):
        return None
    return RedirectResponse("/login", status_code=303)


def _stream_signature(config: AppSecrets, media_id: int, expires: int) -> str:
    payload = f"{int(media_id)}:{int(expires)}".encode("ascii")
    return hmac.new(
        config.stream_link_secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()


def create_app(
    data_dir: str | Path | None = None,
    telegram_factory: Callable[[AppSecrets], Any] | None = None,
) -> FastAPI:
    selected_data_dir = resolve_data_dir(data_dir)
    vault = SecretVault(selected_data_dir)
    try:
        config = vault.load()
        config_error = ""
    except ConfigurationError as exc:
        config = None
        config_error = str(exc)

    database = Database(selected_data_dir / "cinevault.db")
    database.initialise()
    limiter = LoginRateLimiter()
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    templates.env.filters["filesize"] = format_bytes
    templates.env.filters["duration"] = format_duration
    templates.env.filters["date"] = format_date

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.telegram = None
        application.state.telegram_error = config_error
        application.state.catalog = None
        application.state.periodic_task = None
        application.state.prune_job = None
        application.state.poster_task = None
        application.state.poster_fetcher = None
        if config is not None:
            factory = telegram_factory or FilmBotClient
            try:
                telegram = factory(config)
                application.state.telegram = telegram
                await telegram.connect()
                catalog = MediaCatalog(
                    database, telegram, selected_data_dir / "thumbnails"
                )
                application.state.catalog = catalog
                poster_fetcher = PosterFetcher(
                    database, PACKAGE_DIR.parent, selected_data_dir / "thumbnails"
                )
                application.state.poster_fetcher = poster_fetcher
                if hasattr(telegram, "set_delete_callback"):
                    telegram.set_delete_callback(catalog.purge_messages)
                application.state.telegram_error = ""

                async def periodic_prune() -> None:
                    while True:
                        await asyncio.sleep(max(60, config.prune_interval_seconds))
                        try:
                            await catalog.prune(limit=100)
                            if config.poster_lookup and poster_fetcher.available:
                                await poster_fetcher.enrich_missing(limit=100)
                            application.state.telegram_error = ""
                        except Exception as exc:
                            application.state.telegram_error = str(exc)

                application.state.periodic_task = asyncio.create_task(periodic_prune())
                if config.poster_lookup and poster_fetcher.available:
                    application.state.poster_task = asyncio.create_task(
                        poster_fetcher.enrich_missing(limit=100)
                    )
            except Exception as exc:
                application.state.telegram_error = str(exc)
        try:
            yield
        finally:
            for task_name in ("periodic_task", "prune_job", "poster_task"):
                task = getattr(application.state, task_name, None)
                if task and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            telegram = getattr(application.state, "telegram", None)
            if telegram is not None:
                with contextlib.suppress(Exception):
                    await telegram.disconnect()

    app = FastAPI(
        title="CineVault",
        version="1.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.cookie_secret if config else new_token(48),
        session_cookie="cinevault_session",
        max_age=12 * 60 * 60,
        same_site="strict",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        connected = bool(
            getattr(request.app.state, "telegram", None)
            and not getattr(request.app.state, "telegram_error", "")
        )
        return JSONResponse(
            {
                "status": "ok" if config and connected else "degraded",
                "configured": config is not None,
                "telegram_connected": connected,
            }
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if config and config.access_mode == "public":
            return RedirectResponse("/", status_code=303)
        if config and _is_authenticated(request, config):
            return RedirectResponse("/", status_code=303)
        response = templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf": _csrf_token(request),
                "error": "",
                "configuration_error": config_error,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        if config and config.access_mode == "public":
            return RedirectResponse("/", status_code=303)
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        csrf = str(form.get("csrf", ""))
        identity = _client_ip(request)
        allowed, retry_after = await limiter.allowed(identity)
        error = "Invalid username or password."
        status = 401
        if not allowed:
            error = f"Too many attempts. Try again in {math.ceil(retry_after / 60)} minute(s)."
            status = 429
        elif config is None:
            error = config_error
            status = 503
        elif not _valid_csrf(request, csrf):
            error = "Your login page expired. Refresh and try again."
            status = 403
        elif secrets.compare_digest(username, config.web_username) and verify_password(
            config.password_hash, password
        ):
            await limiter.success(identity)
            request.session.clear()
            request.session["authenticated"] = True
            request.session["user"] = config.web_username
            request.session["csrf"] = new_token(24)
            return RedirectResponse("/", status_code=303)
        else:
            await limiter.failure(identity)

        response = templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf": _csrf_token(request),
                "error": error,
                "configuration_error": config_error,
            },
            status_code=status,
        )
        response.headers["Cache-Control"] = "no-store"
        if status == 429:
            response.headers["Retry-After"] = str(retry_after)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        form = await request.form()
        if not _valid_csrf(request, str(form.get("csrf", ""))):
            return Response("Invalid CSRF token", status_code=403)
        request.session.clear()
        response = RedirectResponse(
            "/" if config and config.access_mode == "public" else "/login",
            status_code=303,
        )
        response.delete_cookie("cinevault_session")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def library(request: Request):
        if config is None:
            return RedirectResponse("/login", status_code=303)
        redirect = _auth_redirect(request, config)
        if redirect:
            return redirect
        query = request.query_params.get("q", "")[:120]
        kind = request.query_params.get("kind", "all")
        sort = request.query_params.get("sort", "newest")
        kind = kind if kind in MEDIA_KINDS else "all"
        sort = sort if sort in LIBRARY_SORTS else "newest"
        seed = _random_seed(request.query_params.get("seed"))
        if sort == "random" and seed is None:
            seed = secrets.randbelow(MAX_RANDOM_SEED) + 1
            return RedirectResponse(
                f"/?{_context_query(query, kind, sort, seed)}", status_code=303
            )
        seed = seed or 0
        batch_size = 36
        items, total = database.list_media(
            query=query,
            kind=kind,
            per_page=batch_size,
            sort=sort,
            offset=0,
            seed=seed,
        )
        context_query = _context_query(query, kind, sort, seed)
        featured_item = next(
            (item for item in items if str(item.get("kind")) == "video"),
            items[0] if items else None,
        )
        response = templates.TemplateResponse(
            request,
            "library.html",
            {
                "items": items,
                "featured_item": featured_item,
                "total": total,
                "stats": database.stats(),
                "query": query,
                "kind": kind,
                "sort": sort,
                "seed": seed,
                "context_query": context_query,
                "has_more": len(items) < total,
                "csrf": _csrf_token(request),
                "config": config,
                "last_prune_at": database.get_meta("last_prune_at"),
                "telegram_error": request.app.state.telegram_error,
            },
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.get("/api/media", response_class=HTMLResponse)
    async def media_batch(request: Request):
        if config is None or not _access_allowed(request, config):
            return Response(status_code=401)
        query = request.query_params.get("q", "")[:120]
        kind = request.query_params.get("kind", "all")
        sort = request.query_params.get("sort", "newest")
        kind = kind if kind in MEDIA_KINDS else "all"
        sort = sort if sort in LIBRARY_SORTS else "newest"
        seed = _random_seed(request.query_params.get("seed")) or 0
        try:
            offset = max(0, int(request.query_params.get("offset", "0")))
        except ValueError:
            offset = 0
        batch_size = 36
        items, total = database.list_media(
            query=query,
            kind=kind,
            per_page=batch_size,
            sort=sort,
            offset=offset,
            seed=seed,
        )
        next_offset = offset + len(items)
        response = templates.TemplateResponse(
            request,
            "media_cards.html",
            {
                "items": items,
                "context_query": _context_query(query, kind, sort, seed),
            },
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Next-Offset"] = str(next_offset)
        response.headers["X-Has-More"] = "true" if next_offset < total else "false"
        response.headers["X-Total-Count"] = str(total)
        return response

    @app.post("/api/bot/search")
    async def bot_search(request: Request):
        if config is None or not _access_allowed(request, config):
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        form = await request.form()
        if not _valid_csrf(request, str(form.get("csrf", ""))):
            return JSONResponse({"error": "Your page expired. Refresh and try again."}, status_code=403)
        telegram = request.app.state.telegram
        if telegram is None:
            return JSONResponse(
                {"error": request.app.state.telegram_error or "Telegram is offline."},
                status_code=503,
            )
        try:
            page = await telegram.search(str(form.get("query", "")))
            request.app.state.telegram_error = ""
            return JSONResponse({"type": "page", "page": page.public()})
        except BotRequestError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            request.app.state.telegram_error = str(exc)[:300]
            return JSONResponse(
                {"error": "Telegram could not complete that search. Try again shortly."},
                status_code=502,
            )

    @app.post("/api/bot/action")
    async def bot_action(request: Request):
        if config is None or not _access_allowed(request, config):
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        form = await request.form()
        if not _valid_csrf(request, str(form.get("csrf", ""))):
            return JSONResponse({"error": "Your page expired. Refresh and try again."}, status_code=403)
        telegram = request.app.state.telegram
        catalog = request.app.state.catalog
        if telegram is None or catalog is None:
            return JSONResponse(
                {"error": request.app.state.telegram_error or "Telegram is offline."},
                status_code=503,
            )
        try:
            outcome = await telegram.activate(
                str(form.get("page", "")), str(form.get("action", ""))
            )
            request.app.state.telegram_error = ""
            if outcome.kind == "page" and outcome.page is not None:
                return JSONResponse({"type": "page", "page": outcome.page.public()})
            if outcome.kind == "media" and outcome.media_message is not None:
                item = await catalog.ingest(outcome.media_message)
                poster_fetcher = request.app.state.poster_fetcher
                poster_task = request.app.state.poster_task
                if (
                    config.poster_lookup
                    and poster_fetcher is not None
                    and poster_fetcher.available
                    and (poster_task is None or poster_task.done())
                ):
                    request.app.state.poster_task = asyncio.create_task(
                        poster_fetcher.enrich_missing(limit=8)
                    )
                return JSONResponse(
                    {
                        "type": "media",
                        "media": {
                            "id": int(item["id"]),
                            "title": str(item["title"]),
                            "filename": str(item["filename"]),
                            "mime_type": str(item["mime_type"]),
                        },
                        "watch_url": f"/media/{int(item['id'])}",
                    }
                )
            raise BotRequestError("The bot returned an unsupported response.")
        except (BotRequestError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            request.app.state.telegram_error = str(exc)[:300]
            return JSONResponse(
                {"error": "Telegram could not complete that action. Try again shortly."},
                status_code=502,
            )

    @app.get("/media/{media_id}", response_class=HTMLResponse)
    async def media_page(media_id: int, request: Request):
        if config is None:
            return RedirectResponse("/login", status_code=303)
        redirect = _auth_redirect(request, config)
        if redirect:
            return redirect
        item = database.get_media(media_id)
        if item is None:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"title": "Media not found", "message": "This item is no longer indexed."},
                status_code=404,
            )
        telegram = request.app.state.telegram
        catalog = request.app.state.catalog
        if telegram is not None and catalog is not None:
            source_message = await telegram.get_message(int(item["message_id"]))
            if source_message is None:
                await catalog.purge_media(media_id)
                return templates.TemplateResponse(
                    request,
                    "error.html",
                    {
                        "title": "Media removed",
                        "message": "This file was deleted from Telegram and has been removed from CineVault.",
                    },
                    status_code=410,
                )
            request.app.state.telegram_error = ""
            database.mark_source_checked([int(item["message_id"])])
        query = request.query_params.get("q", "")[:120]
        library_kind = request.query_params.get("kind", "all")
        sort = request.query_params.get("sort", "newest")
        library_kind = library_kind if library_kind in MEDIA_KINDS else "all"
        sort = sort if sort in LIBRARY_SORTS else "newest"
        seed = _random_seed(request.query_params.get("seed")) or 0
        context_query = _context_query(query, library_kind, sort, seed)

        playlist = database.playlist(
            query=query,
            kind=str(item["kind"]),
            sort=sort,
            seed=seed,
        )
        playlist_ids = [int(candidate["id"]) for candidate in playlist]
        if media_id not in playlist_ids and query:
            playlist = database.playlist(
                kind=str(item["kind"]),
                sort=sort,
                seed=seed,
            )
            playlist_ids = [int(candidate["id"]) for candidate in playlist]

        previous_item = None
        next_item = None
        related: list[dict[str, Any]]
        if media_id in playlist_ids:
            position = playlist_ids.index(media_id)
            if position > 0:
                previous_item = playlist[position - 1]
            if position + 1 < len(playlist):
                next_item = playlist[position + 1]
            related = (playlist[position + 1 :] + playlist[:position])[:10]
        else:
            related = database.related(media_id, str(item["kind"]))

        def watch_url(candidate: dict[str, Any] | None) -> str:
            if not candidate:
                return ""
            return f"/media/{int(candidate['id'])}?{context_query}"

        response = templates.TemplateResponse(
            request,
            "media.html",
            {
                "item": item,
                "related": related,
                "next_item": next_item,
                "previous_item": previous_item,
                "next_url": watch_url(next_item),
                "previous_url": watch_url(previous_item),
                "context_query": context_query,
                "back_url": f"/?{context_query}",
                "csrf": _csrf_token(request),
                "config": config,
                "browser_compat_warning": str(item["filename"]).casefold().endswith(
                    (".mkv", ".avi", ".ts", ".m2ts", ".flv")
                ),
            },
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.get("/thumbnail/{media_id}")
    async def thumbnail(media_id: int, request: Request):
        if config is None or not _access_allowed(request, config):
            return Response(status_code=401)
        item = database.get_media(media_id)
        if item is None or not item.get("thumbnail_filename"):
            return FileResponse(PACKAGE_DIR / "static" / "placeholder.svg", media_type="image/svg+xml")
        path = selected_data_dir / "thumbnails" / Path(item["thumbnail_filename"]).name
        if not path.is_file():
            return FileResponse(PACKAGE_DIR / "static" / "placeholder.svg", media_type="image/svg+xml")
        return FileResponse(
            path,
            media_type="image/webp",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.get("/poster/{media_id}")
    async def poster(media_id: int, request: Request):
        if config is None or not _access_allowed(request, config):
            return Response(status_code=401)
        item = database.get_media(media_id)
        if item is not None:
            for key in ("poster_filename", "thumbnail_filename"):
                if not item.get(key):
                    continue
                path = selected_data_dir / "thumbnails" / Path(item[key]).name
                if path.is_file():
                    return FileResponse(
                        path,
                        media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=86400"},
                    )
        return FileResponse(
            PACKAGE_DIR / "static" / "placeholder.svg", media_type="image/svg+xml"
        )

    async def serve_stream(media_id: int, request: Request) -> Response:
        item = database.get_media(media_id)
        telegram = request.app.state.telegram
        catalog = request.app.state.catalog
        if item is None or telegram is None:
            return Response("Media unavailable", status_code=404)
        filename = quote(str(item["filename"]), safe="")
        disposition = "attachment" if request.query_params.get("download") == "1" else "inline"
        base_headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
            "X-Accel-Buffering": "no",
        }
        message = await telegram.get_message(int(item["message_id"]))
        if message is None:
            if catalog is not None:
                await catalog.purge_media(media_id)
            return Response("Media removed from Telegram", status_code=410)
        request.app.state.telegram_error = ""
        database.mark_source_checked([int(item["message_id"])])
        if item["kind"] == "photo":
            if request.method == "HEAD":
                return Response(headers=base_headers, media_type=item["mime_type"])
            try:
                raw = await telegram.download_photo(int(item["message_id"]))
            except FileNotFoundError:
                if catalog is not None:
                    await catalog.purge_media(media_id)
                return Response("Media removed from Telegram", status_code=410)
            base_headers["Content-Length"] = str(len(raw))
            return Response(raw, media_type=item["mime_type"], headers=base_headers)

        size = int(item["size_bytes"] or 0)
        try:
            selected = parse_range_header(request.headers.get("range"), size)
        except RangeNotSatisfiable:
            return Response(
                status_code=416,
                headers={**base_headers, "Content-Range": f"bytes */{max(0, size)}"},
            )
        headers = {**base_headers, "Content-Length": str(selected.length)}
        status_code = 206 if selected.partial else 200
        if selected.partial:
            headers["Content-Range"] = selected.content_range
        if request.method == "HEAD":
            return Response(
                status_code=status_code,
                media_type=item["mime_type"],
                headers=headers,
            )
        return StreamingResponse(
            telegram.stream(
                int(item["message_id"]),
                selected.start,
                selected.end,
                selected.size,
            ),
            status_code=status_code,
            media_type=item["mime_type"],
            headers=headers,
        )

    @app.api_route("/stream/{media_id}", methods=["GET", "HEAD"])
    async def stream_media(media_id: int, request: Request):
        if config is None or not _access_allowed(request, config):
            return Response(status_code=401)
        return await serve_stream(media_id, request)

    @app.post("/api/media/{media_id}/link")
    async def create_public_link(media_id: int, request: Request):
        if config is None or not _access_allowed(request, config):
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        form = await request.form()
        if not _valid_csrf(request, str(form.get("csrf", ""))):
            return JSONResponse(
                {"error": "Your page expired. Refresh and try again."}, status_code=403
            )
        item = database.get_media(media_id)
        if item is None:
            return JSONResponse({"error": "Media not found"}, status_code=404)
        try:
            days = max(1, min(int(form.get("days", 7)), 30))
        except (TypeError, ValueError):
            days = 7
        expires = int(time.time()) + days * 86400
        signature = _stream_signature(config, media_id, expires)
        filename = quote(str(item.get("filename") or f"media-{media_id}"), safe="")
        url = str(
            request.url_for(
                "public_stream",
                media_id=media_id,
                expires=expires,
                signature=signature,
                filename=filename,
            )
        )
        return JSONResponse({"url": url, "expires": expires, "days": days})

    @app.api_route(
        "/public/stream/{media_id}/{expires}/{signature}/{filename:path}",
        methods=["GET", "HEAD"],
        name="public_stream",
    )
    async def public_stream(
        media_id: int, expires: int, signature: str, filename: str, request: Request
    ):
        if config is None or expires < int(time.time()):
            return Response("Link expired", status_code=410)
        expected = _stream_signature(config, media_id, expires)
        if not hmac.compare_digest(expected, signature):
            return Response("Invalid stream link", status_code=403)
        return await serve_stream(media_id, request)

    @app.post("/prune")
    async def start_prune(request: Request):
        if config is None or not _access_allowed(request, config):
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        form = await request.form()
        if not _valid_csrf(request, str(form.get("csrf", ""))):
            return JSONResponse({"error": "invalid_csrf"}, status_code=403)
        catalog = request.app.state.catalog
        if catalog is None:
            return JSONResponse(
                {"error": request.app.state.telegram_error or "Telegram is offline"},
                status_code=503,
            )
        job = request.app.state.prune_job
        if job and not job.done():
            return JSONResponse({"status": "already_running"}, status_code=202)

        async def run_prune() -> None:
            try:
                await catalog.prune(limit=500)
                request.app.state.telegram_error = ""
            except Exception as exc:
                request.app.state.telegram_error = str(exc)

        request.app.state.prune_job = asyncio.create_task(run_prune())
        return JSONResponse({"status": "started"}, status_code=202)

    @app.get("/api/status")
    async def service_status(request: Request):
        if config is None or not _access_allowed(request, config):
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        job = request.app.state.prune_job
        return JSONResponse(
            {
                "telegram_error": request.app.state.telegram_error,
                "prune_running": bool(job and not job.done()),
                "stats": database.stats(),
                "last_prune_at": database.get_meta("last_prune_at"),
            }
        )

    return app
