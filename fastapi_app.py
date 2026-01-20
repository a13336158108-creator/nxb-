import json
import secrets
import threading
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


def _find_position(
    state: Dict[str, Any],
    exchange_name: str,
    symbol: str,
    side: str,
) -> Optional[Dict[str, Any]]:
    positions = state.get("positions", [])
    if not isinstance(positions, list):
        return None
    for item in positions:
        if not isinstance(item, dict):
            continue
        if item.get("exchange") != exchange_name:
            continue
        if item.get("symbol") != symbol:
            continue
        if side and item.get("side") != side:
            continue
        return item
    return None


def _serialize_trade(trade: Dict[str, Any]) -> Dict[str, Any]:
    fee = trade.get("fee") or {}
    return {
        "id": trade.get("id"),
        "order": trade.get("order"),
        "side": trade.get("side"),
        "price": trade.get("price"),
        "amount": trade.get("amount"),
        "cost": trade.get("cost"),
        "timestamp": trade.get("timestamp"),
        "datetime": trade.get("datetime"),
        "fee_cost": fee.get("cost"),
        "fee_currency": fee.get("currency"),
    }


def _summarize_trades(trades: List[Dict[str, Any]], position_side: Optional[str]) -> Dict[str, Any]:
    if position_side not in ("long", "short"):
        return {"trade_side": None, "trade_vwap": None, "trade_amount": None}
    trade_side = "buy" if position_side == "long" else "sell"
    total_qty = 0.0
    total_cost = 0.0
    for trade in trades:
        if trade.get("side") != trade_side:
            continue
        price = trade.get("price")
        amount = trade.get("amount")
        if price is None or amount is None:
            continue
        total_qty += float(amount)
        total_cost += float(price) * float(amount)
    trade_vwap = total_cost / total_qty if total_qty > 0 else None
    return {"trade_side": trade_side, "trade_vwap": trade_vwap, "trade_amount": total_qty or None}


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


def _forwarded_prefix(request: Request) -> str:
    prefix = (request.headers.get("x-forwarded-prefix") or "").strip()
    if not prefix:
        return ""
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    prefix = prefix.rstrip("/")
    return "" if prefix == "/" else prefix


def _cookie_path_for_prefix(prefix: str) -> str:
    return prefix if prefix else "/"


class SafeStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:  # type: ignore[override]
        if _is_forbidden_request_path("/" + path):
            return Response("Forbidden", status_code=403)
        return await super().get_response(path, scope)


def create_app(
    root_dir: Path,
    data_dir: Path,
    auth_user: str = "",
    auth_pass: str = "",
    exchanges: Optional[Dict[str, Any]] = None,
) -> FastAPI:
    app = FastAPI()
    app.state.root_dir = root_dir
    app.state.data_dir = data_dir
    app.state.auth_user = auth_user
    app.state.auth_pass = auth_pass
    app.state.sessions = set()  # type: Set[str]
    app.state.exchanges = exchanges or {}
    app.state.exchange_locks = {name: threading.Lock() for name in app.state.exchanges}

    unauth_paths = {"/login.html", "/api/login", "/api/logout", "/favicon.ico"}

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if path in unauth_paths:
            return await call_next(request)

        if not request.app.state.auth_user:
            return await call_next(request)

        authorized = False
        if not authorized:
            token = request.cookies.get("shadow_session", "")
            if token and token in request.app.state.sessions:
                authorized = True

        if not authorized:
            if path.startswith("/api/"):
                return _json_response({"error": "Unauthorized"}, status_code=401)
            prefix = _forwarded_prefix(request)
            return RedirectResponse(f"{prefix}/login.html" if prefix else "/login.html", status_code=303)
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
        prefix = _forwarded_prefix(request)
        response.set_cookie(
            "shadow_session",
            token,
            max_age=86400,
            httponly=True,
            samesite="lax",
            path=_cookie_path_for_prefix(prefix),
        )
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
    prefix = _forwarded_prefix(request)
    response.delete_cookie("shadow_session", path=_cookie_path_for_prefix(prefix))
    return response
