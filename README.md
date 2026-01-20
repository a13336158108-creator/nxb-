# Shadow Arbitrage Monitor (Read-Only)

This script prints a read-only arbitrage monitor table based on your own exchange
positions and funding data. It does **not** place orders or change any settings.

Supported exchanges (all required):
- Binance
- OKX
- Bybit
- Bitget
- Gate

## What it reads
- Positions (entry price, size, side)
- Funding rate and next funding time
- Current price (mark price when available, otherwise last price)

## What it does NOT do
- No order placement
- No parameter changes
- No withdrawals
- No trading web API (optional local viewer server only)

## Install
```
pip install -r requirements.txt
```

## Configure API keys (read-only)
Export these environment variables:
```
BINANCE_API_KEY=...
BINANCE_API_SECRET=...

OKX_API_KEY=...
OKX_API_SECRET=...
OKX_API_PASSPHRASE=...

BYBIT_API_KEY=...
BYBIT_API_SECRET=...

BITGET_API_KEY=...
BITGET_API_SECRET=...
BITGET_API_PASSPHRASE=...

GATE_API_KEY=...
GATE_API_SECRET=...
```

Make sure the API keys are read-only (no trading, no withdrawal).

## Run
```
python monitor.py --interval 60
```

Run once and exit:
```
python monitor.py --once
```

Write CSV output:
```
python monitor.py --once --csv /tmp/shadow_arb.csv
```

## JSON outputs (atomic overwrite)
By default, the monitor writes JSON files to `data/` (relative to this repo).
You can override the output directory with `--data-dir` or `MONITOR_DATA_DIR`.

Files:
- `data/state.json`      current positions + pair state
- `data/events.json`     expand/converge event stream (bounded)
- `data/positions.json`  shadow (virtual) positions

Environment knobs:
- `MONITOR_EXPAND_THRESHOLD` (default `0.0`) spread delta to trigger `EXPAND`
- `MONITOR_CONVERGE_THRESHOLD` (default `0.0`) spread delta to trigger `CONVERGE`
- `MONITOR_EVENTS_MAX` (default `200`) max events kept in `events.json`

## Offline mock mode (no network calls)
Provide a local JSON file and the monitor will skip all exchange calls.
You can edit the file between intervals to simulate market changes.

Run with:
```
MONITOR_MOCK_FILE=mock_positions.json python monitor.py --interval 60
```

File format (minimal):
```
{
  "positions": [
    {
      "exchange": "Binance",
      "symbol": "BTC/USDT",
      "side": "long",
      "size": 0.5,
      "entry_price": 60000.0,
      "price": 61000.0,
      "funding_rate": 0.0001,
      "next_funding_time": 1760000000000
    }
  ]
}
```

State + signal rules:
- `state=EXPAND` when `spread_delta > expand_threshold`
- `state=CONVERGE` when `spread_delta < -converge_threshold`
- Otherwise `state=NEUTRAL`
- `signal=OPEN` when `state=EXPAND` and no shadow position is open
- `signal=CLOSE` when `state=CONVERGE` and a shadow position is open

## Read-only web viewer (static)
Use `read_only_ui.html` with the `data/` folder on another machine.
The viewer fetches `data/state.json`, `data/events.json`, and `data/positions.json`
from the same directory (same origin).

Example (on the other machine):
```
python3 -m http.server 8080
```
Open `http://127.0.0.1:8080/read_only_ui.html`.

## Optional local web server (read-only)
FastAPI server that serves the static UI files via StaticFiles and exposes a read-only `/api/monitor`
endpoint that reads from `data/state.json`.

Run:
```
python3 web_server.py --port 8080
```

External access (bind all interfaces):
```
python3 web_server.py --host 0.0.0.0 --port 8080 --auth-user YOURUSER --auth-pass YOURPASS
```
Default basic-auth credentials (when not set via env/flags): `nxb` / `nxb`.

## Combined monitor + UI service (single port)
Runs the monitor loop and serves the UI/API (FastAPI + StaticFiles) from the same process and port.

Run:
```
python3 service.py --port 8080 --interval 60
```

Mock mode:
```
MONITOR_MOCK_FILE=mock_positions.json python3 service.py --port 8080 --interval 60
```

External access (bind all interfaces):
```
python3 service.py --host 0.0.0.0 --port 8080 --auth-user YOURUSER --auth-pass YOURPASS
```
Default basic-auth credentials (when not set via env/flags): `nxb` / `nxb`.

## Formulas used (explicit)
- Notional = |positionSize| * markPrice
- spread = (price_A - price_B) / min(price_A, price_B)
- funding_side = positionSize * markPrice * fundingRate
- net_funding = funding_long - funding_short
- spread_delta = spread_now - spread_entry

## Table columns
- symbol, long_ex, short_ex
- long_entry, long_size, long_notional
- short_entry, short_size, short_notional
- price_long, price_short
- spread_now, spread_entry
- long_rate, long_next
- short_rate, short_next
- funding_long, funding_short, net_funding

## Notes
- The script pairs longs and shorts by symbol across exchanges.
- Prices use mark price if available; otherwise last price.
- Funding rate is shown as a percent; times are HH:MM:SS countdowns.
