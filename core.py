import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import ccxt  # type: ignore
except ImportError:
    ccxt = None


EXCHANGE_CONFIGS = [
    {
        "id": "binance",
        "name": "Binance",
        "env_prefix": "BINANCE",
        "options": {"defaultType": "future"},
        "needs_password": False,
    },
    {
        "id": "okx",
        "name": "OKX",
        "env_prefix": "OKX",
        "options": {"defaultType": "swap"},
        "needs_password": True,
    },
    {
        "id": "bybit",
        "name": "Bybit",
        "env_prefix": "BYBIT",
        "options": {"defaultType": "swap"},
        "needs_password": False,
    },
    {
        "id": "bitget",
        "name": "Bitget",
        "env_prefix": "BITGET",
        "options": {"defaultType": "swap"},
        "needs_password": True,
    },
    {
        "id": "gateio",
        "name": "Gate",
        "env_prefix": "GATE",
        "options": {"defaultType": "swap"},
        "needs_password": False,
    },
]


TABLE_COLUMNS = [
    ("symbol", "symbol"),
    ("long_ex", "long_ex"),
    ("short_ex", "short_ex"),
    ("long_entry", "long_entry"),
    ("long_size", "long_size"),
    ("long_notional", "long_notional"),
    ("short_entry", "short_entry"),
    ("short_size", "short_size"),
    ("short_notional", "short_notional"),
    ("price_long", "price_long"),
    ("price_short", "price_short"),
    ("spread_now", "spread_now"),
    ("spread_entry", "spread_entry"),
    ("long_rate", "long_rate"),
    ("long_next", "long_next"),
    ("short_rate", "short_rate"),
    ("short_next", "short_next"),
    ("funding_long", "funding_long"),
    ("funding_short", "funding_short"),
    ("net_funding", "net_funding"),
]

TABLE_HEADERS = [col[0] for col in TABLE_COLUMNS]
TABLE_KEYS = [col[1] for col in TABLE_COLUMNS]


def require_ccxt() -> None:
    if ccxt is None:
        print("Missing dependency: ccxt. Install with: pip install ccxt", file=sys.stderr)
        sys.exit(1)


def is_debug_enabled() -> bool:
    return os.getenv("MONITOR_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def sanitize_url(url: str) -> str:
    return url.split("?", 1)[0]


def sanitize_error_message(message: str) -> str:
    parts = []
    for chunk in message.split():
        if "://" in chunk and "?" in chunk:
            head, tail = chunk.split("?", 1)
            parts.append(head)
        else:
            parts.append(chunk)
    return " ".join(parts)


def truncate_text(text: str, limit: int = 300) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def format_error_details(exc: Exception) -> str:
    details = []
    status = getattr(exc, "status", None)
    if status is not None:
        details.append(f"status={status}")
    url = getattr(exc, "url", None)
    if url:
        details.append(f"url={sanitize_url(str(url))}")
    body = getattr(exc, "body", None)
    if body:
        details.append(f"body={truncate_text(str(body))}")
    return ", ".join(details)


def warn_exchange_error(prefix: str, exc: Exception) -> None:
    exc_type = exc.__class__.__name__
    message = sanitize_error_message(str(exc))
    print(f"[WARN] {prefix}: {exc_type} {message}", file=sys.stderr)
    if is_debug_enabled():
        details = format_error_details(exc)
        if details:
            print(f"[DEBUG] {prefix} details: {details}", file=sys.stderr)


def to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_symbol(exchange: Any, symbol: Optional[str]) -> Optional[str]:
    if not symbol:
        return None
    try:
        market = exchange.market(symbol)
        if market and market.get("base") and market.get("quote"):
            return f"{market['base']}/{market['quote']}"
    except Exception:
        pass
    if ":" in symbol:
        return symbol.split(":")[0]
    return symbol


def format_num(value: Optional[float], digits: int) -> str:
    if value is None:
        return "--"
    fmt = f"{{:.{digits}f}}"
    return fmt.format(value)


def format_pct(value: Optional[float], digits: int) -> str:
    if value is None:
        return "--"
    fmt = f"{{:.{digits}f}}%"
    return fmt.format(value * 100)


def format_countdown(next_ts_ms: Optional[float]) -> str:
    if not next_ts_ms:
        return "--"
    now = time.time()
    delta = int(max(0, (next_ts_ms / 1000.0) - now))
    hours = delta // 3600
    minutes = (delta % 3600) // 60
    seconds = delta % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def render_table(headers: List[str], rows: List[List[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, val in enumerate(row):
            widths[idx] = max(widths[idx], len(val))

    line_sep = "-+-".join("-" * w for w in widths)
    lines = [
        " | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))),
        line_sep,
    ]
    for row in rows:
        lines.append(" | ".join(row[i].ljust(widths[i]) for i in range(len(row))))
    return "\n".join(lines)


def build_exchanges_from_env() -> Dict[str, Any]:
    require_ccxt()
    creds: Dict[str, Dict[str, str]] = {}
    for cfg in EXCHANGE_CONFIGS:
        prefix = cfg["env_prefix"]
        api_key = os.getenv(f"{prefix}_API_KEY")
        secret = os.getenv(f"{prefix}_API_SECRET")
        passphrase = os.getenv(f"{prefix}_API_PASSPHRASE")
        if not api_key or not secret:
            continue
        creds[prefix] = {
            "api_key": api_key,
            "api_secret": secret,
            "passphrase": passphrase or "",
        }
    return build_exchanges_from_credentials(creds)


def build_exchanges_from_credentials(creds: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    require_ccxt()
    exchanges: Dict[str, Any] = {}
    for cfg in EXCHANGE_CONFIGS:
        prefix = cfg["env_prefix"]
        data = creds.get(prefix, {})
        api_key = data.get("api_key") or data.get("key")
        secret = data.get("api_secret") or data.get("secret")
        password = data.get("passphrase") or data.get("password")

        if not api_key or not secret:
            continue
        if cfg["needs_password"] and not password:
            continue

        klass = getattr(ccxt, cfg["id"])
        params: Dict[str, Any] = {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": cfg.get("options", {}),
        }
        if cfg["needs_password"]:
            params["password"] = password

        exchange = klass(params)
        try:
            exchange.load_markets()
        except Exception as exc:
            warn_exchange_error(f"{cfg['name']} load_markets failed", exc)
        exchanges[cfg["name"]] = exchange

    return exchanges


def extract_position(exchange: Any, exchange_name: str, pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    side = pos.get("side")
    contracts = pos.get("contracts")
    contract_size = to_float(pos.get("contractSize"))

    if side is None and contracts is not None:
        if contracts > 0:
            side = "long"
        elif contracts < 0:
            side = "short"

    if side not in ("long", "short"):
        return None

    size = None
    if contracts is not None:
        size = abs(float(contracts)) * (contract_size if contract_size else 1.0)
    else:
        raw_size = (
            pos.get("size")
            or pos.get("positionAmt")
            or pos.get("info", {}).get("pos")
            or pos.get("info", {}).get("size")
        )
        size = to_float(raw_size)
        if size is not None:
            size = abs(size)

    if not size:
        return None

    entry_price = to_float(
        pos.get("entryPrice") or pos.get("avgPrice") or pos.get("info", {}).get("entryPrice")
    )
    mark_price = to_float(pos.get("markPrice") or pos.get("info", {}).get("markPrice"))
    symbol = pos.get("symbol")
    display_symbol = normalize_symbol(exchange, symbol) or symbol

    return {
        "exchange_name": exchange_name,
        "exchange": exchange,
        "symbol": symbol,
        "display_symbol": display_symbol,
        "side": side,
        "size": size,
        "entry_price": entry_price,
        "mark_price": mark_price,
    }


def fetch_positions(exchange: Any, exchange_name: str) -> List[Dict[str, Any]]:
    if not exchange.has.get("fetchPositions"):
        print(f"[WARN] {exchange_name} does not support fetchPositions", file=sys.stderr)
        return []
    try:
        raw_positions = exchange.fetch_positions()
    except Exception as exc:
        warn_exchange_error(f"{exchange_name} fetch_positions failed", exc)
        return []

    positions = []
    for pos in raw_positions:
        extracted = extract_position(exchange, exchange_name, pos)
        if extracted:
            positions.append(extracted)
    return positions


def get_funding_info(
    exchange: Any,
    exchange_name: str,
    symbol: str,
    cache: Dict[Tuple[str, str], Dict[str, Any]],
) -> Dict[str, Any]:
    key = (exchange_name, symbol)
    if key in cache:
        return cache[key]

    info: Dict[str, Any] = {
        "funding_rate": None,
        "next_funding_time": None,
        "price": None,
    }

    if exchange.has.get("fetchFundingRate"):
        try:
            fr = exchange.fetch_funding_rate(symbol)
            info["funding_rate"] = to_float(fr.get("fundingRate"))
            info["next_funding_time"] = fr.get("nextFundingTime") or fr.get("nextFundingTimestamp")
            info["price"] = to_float(fr.get("markPrice") or fr.get("indexPrice"))
        except Exception as exc:
            warn_exchange_error(f"{exchange_name} fetch_funding_rate {symbol} failed", exc)

    if info["price"] is None:
        try:
            ticker = exchange.fetch_ticker(symbol)
            info["price"] = to_float(ticker.get("mark") or ticker.get("last") or ticker.get("close"))
        except Exception as exc:
            warn_exchange_error(f"{exchange_name} fetch_ticker {symbol} failed", exc)

    cache[key] = info
    return info


def calc_spread(price_a: Optional[float], price_b: Optional[float]) -> Optional[float]:
    if price_a is None or price_b is None:
        return None
    base = min(price_a, price_b)
    if base == 0:
        return None
    return (price_a - price_b) / base


def enrich_positions(positions: List[Dict[str, Any]]) -> None:
    funding_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for pos in positions:
        exchange = pos["exchange"]
        symbol = pos["symbol"]
        info = get_funding_info(exchange, pos["exchange_name"], symbol, funding_cache)
        price = info.get("price") or pos.get("mark_price")
        pos["price"] = price
        pos["funding_rate"] = info.get("funding_rate")
        pos["next_funding_time"] = info.get("next_funding_time")
        pos["notional"] = pos["size"] * price if price is not None else None


def collect_positions(exchanges: Dict[str, Any]) -> List[Dict[str, Any]]:
    positions: List[Dict[str, Any]] = []
    for name, exchange in exchanges.items():
        positions.extend(fetch_positions(exchange, name))
    if positions:
        enrich_positions(positions)
    return positions


def build_rows(positions: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for metrics in build_pair_metrics(positions):
        rows.append(
            {
                "symbol": metrics["symbol"],
                "long_ex": metrics["long_ex"],
                "short_ex": metrics["short_ex"],
                "long_entry": format_num(metrics.get("long_entry"), 6),
                "long_size": format_num(metrics.get("long_size"), 4),
                "long_notional": format_num(metrics.get("long_notional"), 2),
                "short_entry": format_num(metrics.get("short_entry"), 6),
                "short_size": format_num(metrics.get("short_size"), 4),
                "short_notional": format_num(metrics.get("short_notional"), 2),
                "price_long": format_num(metrics.get("price_long"), 6),
                "price_short": format_num(metrics.get("price_short"), 6),
                "spread_now": format_num(metrics.get("spread_now"), 6),
                "spread_entry": format_num(metrics.get("spread_entry"), 6),
                "long_rate": format_pct(metrics.get("long_rate"), 4),
                "long_next": format_countdown(metrics.get("long_next")),
                "short_rate": format_pct(metrics.get("short_rate"), 4),
                "short_next": format_countdown(metrics.get("short_next")),
                "funding_long": format_num(metrics.get("funding_long"), 4),
                "funding_short": format_num(metrics.get("funding_short"), 4),
                "net_funding": format_num(metrics.get("net_funding"), 4),
            }
        )
    return rows


def build_pair_metrics(positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_symbol: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for pos in positions:
        symbol = pos.get("display_symbol") or pos.get("symbol")
        if not symbol:
            continue
        by_symbol.setdefault(symbol, {"long": [], "short": []})[pos["side"]].append(pos)

    metrics: List[Dict[str, Any]] = []
    for symbol, sides in sorted(by_symbol.items()):
        for long_pos in sides["long"]:
            for short_pos in sides["short"]:
                spread_now = calc_spread(long_pos.get("price"), short_pos.get("price"))
                spread_entry = calc_spread(long_pos.get("entry_price"), short_pos.get("entry_price"))

                funding_long = None
                if long_pos.get("price") is not None and long_pos.get("funding_rate") is not None:
                    funding_long = long_pos["size"] * long_pos["price"] * long_pos["funding_rate"]

                funding_short = None
                if short_pos.get("price") is not None and short_pos.get("funding_rate") is not None:
                    funding_short = short_pos["size"] * short_pos["price"] * short_pos["funding_rate"]

                net_funding = None
                if funding_long is not None and funding_short is not None:
                    net_funding = funding_long - funding_short

                metrics.append(
                    {
                        "symbol": symbol,
                        "long_ex": long_pos["exchange_name"],
                        "short_ex": short_pos["exchange_name"],
                        "long_entry": long_pos.get("entry_price"),
                        "long_size": long_pos.get("size"),
                        "long_notional": long_pos.get("notional"),
                        "short_entry": short_pos.get("entry_price"),
                        "short_size": short_pos.get("size"),
                        "short_notional": short_pos.get("notional"),
                        "price_long": long_pos.get("price"),
                        "price_short": short_pos.get("price"),
                        "spread_now": spread_now,
                        "spread_entry": spread_entry,
                        "long_rate": long_pos.get("funding_rate"),
                        "long_next": long_pos.get("next_funding_time"),
                        "short_rate": short_pos.get("funding_rate"),
                        "short_next": short_pos.get("next_funding_time"),
                        "funding_long": funding_long,
                        "funding_short": funding_short,
                        "net_funding": net_funding,
                    }
                )
    return metrics


def rows_to_lists(rows: List[Dict[str, str]]) -> List[List[str]]:
    return [[row.get(key, "--") for key in TABLE_KEYS] for row in rows]


def close_exchanges(exchanges: Dict[str, Any]) -> None:
    for exchange in exchanges.values():
        close_fn = getattr(exchange, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass
