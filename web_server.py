#!/usr/bin/env python3
import argparse
import os
from pathlib import Path

import uvicorn

from fastapi_app import create_app


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
    env_auth_user = os.getenv("MONITOR_AUTH_USER")
    env_auth_pass = os.getenv("MONITOR_AUTH_PASS")
    parser.add_argument(
        "--auth-user",
        type=str,
        default=env_auth_user if env_auth_user is not None else "nxb",
        help="basic-auth username (or MONITOR_AUTH_USER, default: nxb)",
    )
    parser.add_argument(
        "--auth-pass",
        type=str,
        default=env_auth_pass if env_auth_pass is not None else "nxb",
        help="basic-auth password (or MONITOR_AUTH_PASS, default: nxb)",
    )
    args = parser.parse_args()
    if bool(args.auth_user) ^ bool(args.auth_pass):
        raise SystemExit("Provide both --auth-user and --auth-pass (or set MONITOR_AUTH_USER/MONITOR_AUTH_PASS).")

    root = Path(__file__).resolve().parent
    data_dir = Path(args.data_dir).resolve()
    app = create_app(root, data_dir, args.auth_user, args.auth_pass)

    print(f"[INFO] Serving {root}")
    print(f"[INFO] Data dir: {data_dir}")
    print(f"[INFO] Open: http://{args.host}:{args.port}/read_only_ui.html")
    print(f"[INFO] Or:   http://{args.host}:{args.port}/index.html")
    if args.host not in ("127.0.0.1", "localhost") and not args.auth_user:
        print("[WARN] Binding to a non-local interface without auth; consider --auth-user/--auth-pass.")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
