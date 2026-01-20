#!/usr/bin/env python3
import argparse
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import monitor
from core import build_exchanges_from_env
from web_server import Handler


def _monitor_loop(
    exchanges: dict,
    csv_path: Optional[str],
    output: monitor.MonitorOutput,
    mock_file: Optional[Path],
    interval: int,
    stop_event: threading.Event,
) -> None:
    while not stop_event.is_set():
        try:
            monitor.run_once(exchanges, csv_path, output, mock_file)
        except Exception as exc:
            print(f"[WARN] Monitor loop error: {exc}", file=sys.stderr)
        if stop_event.wait(interval):
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Combined monitor + UI service for shadow_arb_monitor")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="bind port (default: 8080)")
    parser.add_argument("--interval", type=int, default=60, help="monitor refresh interval in seconds")
    parser.add_argument("--csv", type=str, default=None, help="write CSV output to path (overwritten per interval)")
    parser.add_argument(
        "--mock-file",
        type=str,
        default=os.getenv("MONITOR_MOCK_FILE"),
        help="use local JSON file as positions input (offline mode)",
    )
    default_data_dir = os.getenv("MONITOR_DATA_DIR")
    parser.add_argument(
        "--data-dir",
        type=str,
        default=default_data_dir or str(Path(__file__).with_name("data")),
        help="directory containing state.json/events.json/positions.json",
    )
    parser.add_argument(
        "--expand-threshold",
        type=float,
        default=monitor._safe_float(os.getenv("MONITOR_EXPAND_THRESHOLD"), 0.0),
        help="spread delta threshold for EXPAND state",
    )
    parser.add_argument(
        "--converge-threshold",
        type=float,
        default=monitor._safe_float(os.getenv("MONITOR_CONVERGE_THRESHOLD"), 0.0),
        help="spread delta threshold for CONVERGE state",
    )
    parser.add_argument(
        "--events-max",
        type=int,
        default=int(os.getenv("MONITOR_EVENTS_MAX", "200")),
        help="max events kept in events.json",
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

    interval = max(1, args.interval)
    mock_file = Path(args.mock_file) if args.mock_file else None
    if mock_file:
        print(f"[INFO] Mock mode enabled: {mock_file}")
        exchanges: dict = {}
    else:
        exchanges = build_exchanges_from_env()
        if not exchanges:
            print("No exchanges configured. Set API keys in environment variables.", file=sys.stderr)
            sys.exit(1)

    data_dir = Path(args.data_dir).resolve()
    output = monitor.MonitorOutput(
        data_dir,
        args.expand_threshold,
        args.converge_threshold,
        args.events_max,
    )

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_monitor_loop,
        args=(exchanges, args.csv, output, mock_file, interval, stop_event),
        daemon=True,
    )
    thread.start()

    root = Path(__file__).resolve().parent

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
    finally:
        stop_event.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
