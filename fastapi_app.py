import base64
import json
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from core import TABLE_HEADERS, build_rows, rows_to_lists


def _json_response(payload: Dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _state_positions_to_core(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_positions = state.get("positions", [])
    if not isinstance(raw_positions, list):
        return []

    positions: List[Dict[str, Any]] = []
    for item in raw_positions:
        if not isinstance(item, dict):
            continue
        exchange_name = item.get("exchange")
        side = item.get("side")
        display_symbol = item.get("symbol") or item.get("raw_symbol")
        raw_symbol = item.get("raw_symbol") or item.get("symbol")
        if not exchange_name or side not in ("long", "short") or not display_symbol:
            continue
        positions.append(
            {
                "exchange_name": exchange_name,
                "symbol": raw_symbol or display_symbol,
                "display_symbol": display_symbol,
                "side": side,
                "size": item.get("size"),
                "entry_price": item.get("entry_price"),
                "mark_price": item.get("mark_price"),
                "price": item.get("price") or item.get("mark_price"),
                "notional": item.get("notional"),
                "funding_rate": item.get("funding_rate"),
                "next_funding_time": item.get("next_funding_time"),
            }
        )
    return positions


def _is_forbidden_request_path(request_path: str) -> bool:
    path = unquote(urlsplit(request_path).path)
    parts = [p for p in path.split("/") if p]
    if any(p in ("..",) for p in parts):
        return True
    if any(p.startswith(".") for p in parts):
        return True
    if parts and parts[-1] in ("env.sh",):
        return True
    return False


def _is_authorized(header: str, auth_user: str, auth_pass: str) -> bool:
    if not auth_user:
        return True
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except Exception:
        return False
    if ":" not in decoded:
        return False
    user, password = decoded.split(":", 1)
    return secrets.compare_digest(user, auth_user) and secrets.compare_digest(password, auth_pass)


class SafeStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:  # type: ignore[override]
        if _is_forbidden_request_path("/" + path):
            return Response("Forbidden", status_code=403)
        return await super().get_response(path, scope)


def create_app(root_dir: Path, data_dir: Path, auth_user: str = "", auth_pass: str = "") -> FastAPI:
    app = FastAPI()
    app.state.root_dir = root_dir
    app.state.data_dir = data_dir
    app.state.auth_user = auth_user
    app.state.auth_pass = auth_pass
    app.state.sessions = set()  # type: Set[str]

    unauth_paths = {"/login.html", "/api/login", "/api/logout", "/favicon.ico"}

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if path in unauth_paths:
            return await call_next(request)

        authorized = False
        if request.app.state.auth_user:
            header = request.headers.get("Authorization", "")
            authorized = _is_authorized(header, request.app.state.auth_user, request.app.state.auth_pass)
        else:
            authorized = True

        if not authorized:
            token = request.cookies.get("shadow_session", "")
            if token and token in request.app.state.sessions:
                authorized = True

        if not authorized:
            if path.startswith("/api/"):
                return Response(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="shadow_arb_monitor"'},
                )
            return RedirectResponse("/login.html", status_code=303)
        return await call_next(request)

    @app.post("/api/login")
    async def api_login(request: Request):
        if not request.app.state.auth_user:
            return _json_response({"ok": True}, status_code=200)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if not secrets.compare_digest(username, request.app.state.auth_user) or not secrets.compare_digest(
            password, request.app.state.auth_pass
        ):
            return _json_response({"error": "Invalid credentials"}, status_code=401)
        token = secrets.token_urlsafe(32)
        request.app.state.sessions.add(token)
        response = _json_response({"ok": True}, status_code=200)
        response.set_cookie("shadow_session", token, max_age=86400, httponly=True, samesite="lax", path="/")
        return response

    @app.get("/api/logout")
    async def api_logout_get(request: Request):
        return _clear_session(request)

    @app.post("/api/logout")
    async def api_logout_post(request: Request):
        return _clear_session(request)

    @app.get("/api/state")
    async def api_state():
        return _read_data_file(data_dir, "state.json")

    @app.get("/api/events")
    async def api_events():
        return _read_data_file(data_dir, "events.json")

    @app.get("/api/positions")
    async def api_positions():
        return _read_data_file(data_dir, "positions.json")

    @app.get("/api/monitor")
    async def api_monitor():
        state_path = data_dir / "state.json"
        if not state_path.exists():
            return _json_response(
                {
                    "error": f"Missing {state_path}. Run: python3 monitor.py --interval 60",
                    "headers": TABLE_HEADERS,
                    "rows": [],
                    "message": "No data yet.",
                    "ts": int(data_dir.stat().st_mtime) if data_dir.exists() else 0,
                },
                status_code=404,
            )
        try:
            state = _load_json(state_path)
            positions = _state_positions_to_core(state)
            rows = rows_to_lists(build_rows(positions))
            payload = {
                "ts": state.get("ts"),
                "message": state.get("message") or "",
                "headers": TABLE_HEADERS,
                "rows": rows,
            }
        except Exception as exc:
            return _json_response({"error": f"Failed to build monitor payload: {exc}"}, status_code=500)
        return _json_response(payload, status_code=200)

    @app.get("/")
    async def index():
        return FileResponse(root_dir / "index.html")

    app.mount("/", SafeStaticFiles(directory=str(root_dir), html=True), name="static")
    return app


def _read_data_file(data_dir: Path, name: str) -> JSONResponse:
    if _is_forbidden_request_path("/" + name):
        return _json_response({"error": "Forbidden"}, status_code=403)
    path = data_dir / name
    if not path.exists():
        return _json_response({"error": f"Missing {path}. Run monitor.py first."}, status_code=404)
    try:
        payload = _load_json(path)
    except Exception as exc:
        return _json_response({"error": f"Failed to read {path}: {exc}"}, status_code=500)
    return _json_response(payload, status_code=200)


def _clear_session(request: Request) -> JSONResponse:
    token = request.cookies.get("shadow_session", "")
    if token:
        request.app.state.sessions.discard(token)
    response = _json_response({"ok": True}, status_code=200)
    response.delete_cookie("shadow_session", path="/")
    return response
