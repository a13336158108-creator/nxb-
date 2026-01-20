#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import (
    TABLE_HEADERS,
    build_exchanges_from_env,
    build_pair_metrics,
    collect_positions,
    build_rows,
    render_table,
    rows_to_lists,
)


def _safe_float(value: Optional[str], default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pair_key(symbol: str, long_ex: str, short_ex: str) -> str:
    return f"{symbol}|{long_ex}|{short_ex}"


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def _load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except Exception as exc:
        print(f"[WARN] Failed to read {path}: {exc}", file=sys.stderr)
        return default


def _serialize_positions(raw_positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    serialized: List[Dict[str, Any]] = []
    for pos in raw_positions:
        serialized.append(
            {
                "exchange": pos.get("exchange_name"),
                "symbol": pos.get("display_symbol") or pos.get("symbol"),
                "raw_symbol": pos.get("symbol"),
                "side": pos.get("side"),
                "size": pos.get("size"),
                "entry_price": pos.get("entry_price"),
                "mark_price": pos.get("mark_price"),
                "price": pos.get("price"),
                "notional": pos.get("notional"),
                "funding_rate": pos.get("funding_rate"),
                "next_funding_time": pos.get("next_funding_time"),
            }
        )
    return serialized


def _coerce_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_mock_positions(path: Path) -> List[Dict[str, Any]]:
    payload = _load_json(path, {})
    if isinstance(payload, list):
        raw_positions = payload
    elif isinstance(payload, dict):
        raw_positions = payload.get("positions", [])
    else:
        raw_positions = []

    if not isinstance(raw_positions, list):
        raw_positions = []

    positions: List[Dict[str, Any]] = []
    for item in raw_positions:
        if not isinstance(item, dict):
            continue
        exchange_name = item.get("exchange") or item.get("exchange_name")
        symbol = item.get("symbol")
        side = item.get("side")
        if not exchange_name or not symbol or side not in ("long", "short"):
            continue

        size = _coerce_float(item.get("size"))
        entry_price = _coerce_float(item.get("entry_price"))
        price = _coerce_float(item.get("price") or item.get("mark_price"))
        mark_price = _coerce_float(item.get("mark_price")) or price
        funding_rate = _coerce_float(item.get("funding_rate"))
        next_funding_time = item.get("next_funding_time")
        notional = _coerce_float(item.get("notional"))
        if notional is None and size is not None and price is not None:
            notional = size * price

        positions.append(
            {
                "exchange_name": exchange_name,
                "symbol": symbol,
                "display_symbol": item.get("display_symbol") or symbol,
                "side": side,
                "size": size,
                "entry_price": entry_price,
                "mark_price": mark_price,
                "price": price,
                "notional": notional,
                "funding_rate": funding_rate,
                "next_funding_time": next_funding_time,
            }
        )

    if not positions:
        print(f"[WARN] No valid mock positions found in {path}", file=sys.stderr)
    return positions


class MonitorOutput:
    def __init__(self, data_dir: Path, expand_threshold: float, converge_threshold: float, events_max: int) -> None:
        self.data_dir = data_dir
        self.expand_threshold = max(0.0, expand_threshold)
        self.converge_threshold = max(0.0, converge_threshold)
        self.events_max = max(1, events_max)
        self.shadow_positions: Dict[str, Dict[str, Any]] = {}
        self.events: List[Dict[str, Any]] = []
        self.last_states: Dict[str, str] = {}
        self._load_state()

    def _load_state(self) -> None:
        positions_payload = _load_json(self.data_dir / "positions.json", {})
        for item in positions_payload.get("positions", []) or []:
            key = item.get("key")
            if key:
                self.shadow_positions[key] = dict(item)

        events_payload = _load_json(self.data_dir / "events.json", {})
        raw_events = events_payload.get("events", [])
        if isinstance(raw_events, list):
            self.events = [e for e in raw_events if isinstance(e, dict)]

    def _emit_event(self, event: Dict[str, Any]) -> None:
        self.events.append(event)
        if len(self.events) > self.events_max:
            self.events = self.events[-self.events_max :]

    def _classify_state(self, spread_delta: Optional[float]) -> str:
        if spread_delta is None:
            return "UNKNOWN"
        if spread_delta > self.expand_threshold:
            return "EXPAND"
        if spread_delta < -self.converge_threshold:
            return "CONVERGE"
        return "NEUTRAL"

    def update(
        self,
        ts: int,
        positions: List[Dict[str, Any]],
        pair_metrics: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        message = ""
        if not positions:
            message = "No positions found."
        elif not pair_metrics:
            message = "No long/short pairs found across exchanges."

        pairs_payload: List[Dict[str, Any]] = []
        for pair in pair_metrics:
            spread_now = pair.get("spread_now")
            spread_entry = pair.get("spread_entry")
            spread_delta = None
            if spread_now is not None and spread_entry is not None:
                spread_delta = spread_now - spread_entry

            state = self._classify_state(spread_delta)
            key = _pair_key(pair["symbol"], pair["long_ex"], pair["short_ex"])
            shadow = self.shadow_positions.get(key)
            signal = "HOLD"
            if state == "EXPAND" and shadow is None:
                signal = "OPEN"
                shadow = {
                    "key": key,
                    "symbol": pair["symbol"],
                    "long_ex": pair["long_ex"],
                    "short_ex": pair["short_ex"],
                    "opened_at": ts,
                    "open_spread": spread_now,
                    "open_spread_entry": spread_entry,
                    "last_spread": spread_now,
                    "last_state": state,
                    "last_update": ts,
                }
                self.shadow_positions[key] = shadow
            elif state == "CONVERGE" and shadow is not None:
                signal = "CLOSE"
                opened_at = shadow.get("opened_at")
                shadow.update(
                    {
                        "last_spread": spread_now,
                        "last_state": state,
                        "last_update": ts,
                    }
                )
                self.shadow_positions.pop(key, None)
                self._emit_event(
                    {
                        "ts": ts,
                        "event": "CONVERGE",
                        "signal": signal,
                        "symbol": pair["symbol"],
                        "long_ex": pair["long_ex"],
                        "short_ex": pair["short_ex"],
                        "spread_now": spread_now,
                        "spread_entry": spread_entry,
                        "spread_delta": spread_delta,
                        "opened_at": opened_at,
                    }
                )

            event_emitted = False
            if signal == "OPEN":
                self._emit_event(
                    {
                        "ts": ts,
                        "event": "EXPAND",
                        "signal": signal,
                        "symbol": pair["symbol"],
                        "long_ex": pair["long_ex"],
                        "short_ex": pair["short_ex"],
                        "spread_now": spread_now,
                        "spread_entry": spread_entry,
                        "spread_delta": spread_delta,
                    }
                )
                event_emitted = True
            elif signal == "CLOSE":
                event_emitted = True

            if not event_emitted:
                last_state = self.last_states.get(key)
                if state in ("EXPAND", "CONVERGE") and state != last_state:
                    self._emit_event(
                        {
                            "ts": ts,
                            "event": state,
                            "signal": signal,
                            "symbol": pair["symbol"],
                            "long_ex": pair["long_ex"],
                            "short_ex": pair["short_ex"],
                            "spread_now": spread_now,
                            "spread_entry": spread_entry,
                            "spread_delta": spread_delta,
                        }
                    )

            self.last_states[key] = state

            pairs_payload.append(
                {
                    **pair,
                    "spread_delta": spread_delta,
                    "state": state,
                    "signal": signal,
                }
            )

            if shadow is not None:
                shadow.update(
                    {
                        "last_spread": spread_now,
                        "last_state": state,
                        "last_update": ts,
                    }
                )

        state_payload = {
            "ts": ts,
            "message": message,
            "config": {
                "expand_threshold": self.expand_threshold,
                "converge_threshold": self.converge_threshold,
            },
            "positions": _serialize_positions(positions),
            "pairs": pairs_payload,
        }

        events_payload = {"ts": ts, "events": self.events}
        positions_payload = {"ts": ts, "positions": list(self.shadow_positions.values())}

        return {
            "state": state_payload,
            "events": events_payload,
            "positions": positions_payload,
            "message": message,
        }

    def write_outputs(self, payloads: Dict[str, Any]) -> None:
        _atomic_write_json(self.data_dir / "state.json", payloads["state"])
        _atomic_write_json(self.data_dir / "events.json", payloads["events"])
        _atomic_write_json(self.data_dir / "positions.json", payloads["positions"])


def run_once(exchanges, csv_path, output: MonitorOutput, mock_file: Optional[Path]):
    if mock_file:
        if not mock_file.exists():
            print(f"[WARN] Mock file not found: {mock_file}", file=sys.stderr)
            positions = []
        else:
            positions = _load_mock_positions(mock_file)
    else:
        positions = collect_positions(exchanges)
    pairs = build_pair_metrics(positions)
    rows = build_rows(positions)

    if not positions:
        print("No positions found.")
    elif not rows:
        print("No long/short pairs found across exchanges.")
    else:
        row_lists = rows_to_lists(rows)
        table = render_table(TABLE_HEADERS, row_lists)
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"[{ts}]")
        print(table)

        if csv_path:
            import csv

            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(TABLE_HEADERS)
                writer.writerows(row_lists)

    ts = int(time.time())
    payloads = output.update(ts, positions, pairs)
    output.write_outputs(payloads)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only shadow arbitrage monitor")
    parser.add_argument("--interval", type=int, default=60, help="refresh interval in seconds")
    parser.add_argument("--once", action="store_true", help="run once and exit")
    parser.add_argument("--csv", type=str, default=None, help="write CSV output to path")
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
        help="output directory for JSON data files",
    )
    parser.add_argument(
        "--expand-threshold",
        type=float,
        default=_safe_float(os.getenv("MONITOR_EXPAND_THRESHOLD"), 0.0),
        help="spread delta threshold for EXPAND state",
    )
    parser.add_argument(
        "--converge-threshold",
        type=float,
        default=_safe_float(os.getenv("MONITOR_CONVERGE_THRESHOLD"), 0.0),
        help="spread delta threshold for CONVERGE state",
    )
    parser.add_argument(
        "--events-max",
        type=int,
        default=int(os.getenv("MONITOR_EVENTS_MAX", "200")),
        help="max events kept in events.json",
    )
    args = parser.parse_args()

    mock_file = Path(args.mock_file) if args.mock_file else None
    exchanges = {}
    if mock_file:
        print(f"[INFO] Mock mode enabled: {mock_file}")
    else:
        exchanges = build_exchanges_from_env()
        if not exchanges:
            print("No exchanges configured. Set API keys in environment variables.", file=sys.stderr)
            sys.exit(1)

    output = MonitorOutput(
        Path(args.data_dir),
        args.expand_threshold,
        args.converge_threshold,
        args.events_max,
    )

    while True:
        run_once(exchanges, args.csv, output, mock_file)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
