#!/usr/bin/env python3
import argparse
import base64
import json
import os
import secrets
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import unquote, urlsplit

from core import TABLE_HEADERS, build_rows, rows_to_lists


def _json_response(handler: SimpleHTTPRequestHandler, payload: Dict[str, Any], status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


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


class Handler(SimpleHTTPRequestHandler):
    server_version = "shadow_arb_monitor/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if not self._is_authorized():
            self._request_auth()
            return

        if self.path.startswith("/api/monitor"):
            self._handle_api_monitor()
            return
        if self.path.startswith("/api/state"):
            self._handle_api_file("state.json")
            return
        if self.path.startswith("/api/events"):
            self._handle_api_file("events.json")
            return
        if self.path.startswith("/api/positions"):
            self._handle_api_file("positions.json")
            return

        if _is_forbidden_request_path(self.path):
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return

        super().do_GET()

    def _is_authorized(self) -> bool:
        auth_user = getattr(self.server, "auth_user", "")  # type: ignore[attr-defined]
        auth_pass = getattr(self.server, "auth_pass", "")  # type: ignore[attr-defined]
        if not auth_user:
            return True

        header = self.headers.get("Authorization", "")
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

    def _request_auth(self) -> None:
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="shadow_arb_monitor"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _handle_api_file(self, name: str) -> None:
        if _is_forbidden_request_path(self.path):
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        data_dir = Path(self.server.data_dir)  # type: ignore[attr-defined]
        path = data_dir / name
        if not path.exists():
            _json_response(self, {"error": f"Missing {path}. Run monitor.py first."}, status=404)
            return
        try:
            payload = _load_json(path)
        except Exception as exc:
            _json_response(self, {"error": f"Failed to read {path}: {exc}"}, status=500)
            return
        _json_response(self, payload, status=200)

    def _handle_api_monitor(self) -> None:
        data_dir = Path(self.server.data_dir)  # type: ignore[attr-defined]
        state_path = data_dir / "state.json"
        if not state_path.exists():
            _json_response(
                self,
                {
                    "error": f"Missing {state_path}. Run: python3 monitor.py --interval 60",
                    "headers": TABLE_HEADERS,
                    "rows": [],
                    "message": "No data yet.",
                    "ts": int(data_dir.stat().st_mtime) if data_dir.exists() else 0,
                },
                status=404,
            )
            return
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
            _json_response(self, {"error": f"Failed to build monitor payload: {exc}"}, status=500)
            return

        _json_response(self, payload, status=200)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local read-only web viewer for shadow_arb_monitor")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="bind port (default: 8080)")
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(Path(__file__).with_name("data")),
        help="directory containing state.json/events.json/positions.json",
    )
    parser.add_argument(
        "--auth-user",
        type=str,
        default=os.getenv("MONITOR_AUTH_USER", ""),
        help="optional basic-auth username (or MONITOR_AUTH_USER)",
    )
    parser.add_argument(
        "--auth-pass",
        type=str,
        default=os.getenv("MONITOR_AUTH_PASS", ""),
        help="optional basic-auth password (or MONITOR_AUTH_PASS)",
    )
    args = parser.parse_args()
    if bool(args.auth_user) ^ bool(args.auth_pass):
        raise SystemExit("Provide both --auth-user and --auth-pass (or set MONITOR_AUTH_USER/MONITOR_AUTH_PASS).")

    root = Path(__file__).resolve().parent
    data_dir = Path(args.data_dir).resolve()

    def handler_factory(*h_args, **h_kwargs):
        return Handler(*h_args, directory=str(root), **h_kwargs)

    httpd = ThreadingHTTPServer((args.host, args.port), handler_factory)
    httpd.data_dir = str(data_dir)  # type: ignore[attr-defined]
    httpd.auth_user = args.auth_user  # type: ignore[attr-defined]
    httpd.auth_pass = args.auth_pass  # type: ignore[attr-defined]

    print(f"[INFO] Serving {root}")
    print(f"[INFO] Data dir: {data_dir}")
    print(f"[INFO] Open: http://{args.host}:{args.port}/read_only_ui.html")
    print(f"[INFO] Or:   http://{args.host}:{args.port}/index.html")
    if args.host not in ("127.0.0.1", "localhost") and not args.auth_user:
        print("[WARN] Binding to a non-local interface without auth; consider --auth-user/--auth-pass.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
