"""
solo_trader/app.py

Receives forwarded futures signals from the client's server,
runs risk checks, and executes via IB Gateway.

Signal vocabulary from Vikram's TradingView alerts:
  BUY    → buy  1 contract  (new long)
  SELL   → sell 1 contract  (new short)
  R-BUY  → reverse to long  (close short, open long)
  R-SELL → reverse to short (close long, open short)

All other asset_class values (options, etf, etc.) are silently ignored —
they still go to Discord on the client's server; this app never sees them
unless the forward logic changes.
"""

import logging
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, Request, HTTPException
from ib_insync import IB, Future, LimitOrder, MarketOrder

import asyncio
from functools import partial

from config import cfg
import ledger as ledger_module
from ledger import get_expected_position, reconcile, update_expected
from logger import get_unpushed_trades, log_event, log_trade, mark_trade_pushed
from state import state

import nest_asyncio
nest_asyncio.apply()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("solo_trader")

app = FastAPI(title="solo_trader")


open_limit_orders: dict[str, dict] = {}
open_limit_orders_lock = threading.Lock()
ib_execution_lock = threading.Lock()
_IB_EXECUTION_LOCK = asyncio.Lock()
APP_STARTED_AT: datetime | None = None
APP_EVENT_LOOP: asyncio.AbstractEventLoop | None = None
OPS_API_KEY = os.environ.get("OPS_API_KEY", "changeme")
OPS_DB_PATH = os.getenv("OPS_DB_PATH", os.path.expanduser("~/home-lab/apps/solo_trader_vikrim/ops.db"))
TRADES_RELAY_URL = "https://alerts.tradeinvestors.us/trades"
POSITIONS_RELAY_URL = "https://alerts.tradeinvestors.us/positions"
TRADES_RELAY_KEY = "relay_push_secret_9876543210"
TRADES_RELAY_TIMEOUT_SECONDS = 5


def _positions_by_symbol(ib: IB) -> dict[str, float]:
    positions = ib.positions(account=cfg.IB_ACCOUNT) if cfg.IB_ACCOUNT else ib.positions()
    by_symbol: dict[str, float] = {}
    for position in positions:
        symbol = position.contract.symbol.strip().upper()
        by_symbol[symbol] = by_symbol.get(symbol, 0.0) + float(position.position)
    return by_symbol


def _sync_ib_state(ib: IB) -> None:
    ib.reqPositions()
    ib.reqOpenOrders()
    ib.sleep(1.0)


def _post_trade_with_httpx(trade: dict) -> int:
    import httpx

    response = httpx.post(
        TRADES_RELAY_URL,
        headers={"X-Relay-Key": TRADES_RELAY_KEY},
        json=trade,
        timeout=TRADES_RELAY_TIMEOUT_SECONDS,
    )
    return response.status_code


def _post_trade_with_urllib(trade: dict) -> int:
    body = json.dumps(trade).encode("utf-8")
    request = urllib.request.Request(
        TRADES_RELAY_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Relay-Key": TRADES_RELAY_KEY,
        },
        method="POST",
    )
    with urllib.request.urlopen(
        request,
        timeout=TRADES_RELAY_TIMEOUT_SECONDS,
    ) as response:
        return response.getcode()


def _post_trade_to_relay(trade: dict) -> int:
    try:
        return _post_trade_with_httpx(trade)
    except ImportError:
        return _post_trade_with_urllib(trade)


def _post_positions_with_httpx(payload: dict) -> int:
    import httpx

    response = httpx.post(
        POSITIONS_RELAY_URL,
        headers={"X-Relay-Key": TRADES_RELAY_KEY},
        json=payload,
        timeout=TRADES_RELAY_TIMEOUT_SECONDS,
    )
    return response.status_code


def _post_positions_with_urllib(payload: dict) -> int:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        POSITIONS_RELAY_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Relay-Key": TRADES_RELAY_KEY,
        },
        method="POST",
    )
    with urllib.request.urlopen(
        request,
        timeout=TRADES_RELAY_TIMEOUT_SECONDS,
    ) as response:
        return response.getcode()


def _post_positions_to_relay(payload: dict) -> int:
    try:
        return _post_positions_with_httpx(payload)
    except ImportError:
        return _post_positions_with_urllib(payload)


def push_trades_to_relay() -> None:
    for trade in get_unpushed_trades():
        try:
            status_code = _post_trade_to_relay(trade)
            if status_code == 200:
                mark_trade_pushed(trade["id"])
            else:
                print(
                    f"push_trades_to_relay failed for trade_id={trade.get('id')}: "
                    f"status={status_code}",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(
                f"push_trades_to_relay failed for trade_id={trade.get('id')}: {exc}",
                file=sys.stderr,
            )


def push_positions_to_relay() -> None:
    try:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
        positions = []
        for symbol, size in _read_expected_positions().items():
            if size > 0:
                direction = "LONG"
            elif size < 0:
                direction = "SHORT"
            else:
                direction = "FLAT"
            positions.append(
                {
                    "symbol": symbol,
                    "size": size,
                    "direction": direction,
                    "entry_price": get_entry_price(symbol),
                    "last_updated": now,
                }
            )

        status_code = _post_positions_to_relay({"positions": positions})
        if status_code != 200:
            print(
                f"push_positions_to_relay failed: status={status_code}",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"push_positions_to_relay failed: {exc}", file=sys.stderr)


async def _push_trades_to_relay_async() -> None:
    await asyncio.to_thread(push_trades_to_relay)


async def push_positions_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(push_positions_to_relay)
        except Exception as exc:
            print(f"push_positions_loop failed: {exc}", file=sys.stderr)
        await asyncio.sleep(30)


def _schedule_push_trades_to_relay() -> None:
    if APP_EVENT_LOOP is None or APP_EVENT_LOOP.is_closed():
        print("push_trades_to_relay skipped: app event loop unavailable", file=sys.stderr)
        return
    APP_EVENT_LOOP.call_soon_threadsafe(
        lambda: asyncio.create_task(_push_trades_to_relay_async())
    )


def _exec_id_already_logged(exec_id: str) -> bool:
    if not exec_id:
        return False
    with sqlite3.connect(OPS_DB_PATH) as conn:
        row = conn.execute(
            "SELECT id FROM trades WHERE exec_id = ? LIMIT 1",
            (exec_id,),
        ).fetchone()
    return row is not None


def _execution_ts_iso(execution) -> str:
    execution_time = execution.time
    if isinstance(execution_time, datetime):
        if execution_time.tzinfo is None:
            execution_time = execution_time.replace(tzinfo=timezone.utc)
        return (
            execution_time.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return str(execution_time)


def handle_exec_details(trade, fill) -> None:
    try:
        order_type = getattr(getattr(trade, "order", None), "orderType", "")
        if order_type != "LMT":
            return

        contract = fill.contract
        execution = fill.execution
        exec_id = getattr(execution, "execId", "")
        if _exec_id_already_logged(exec_id):
            return

        symbol = normalize_symbol(getattr(contract, "symbol", ""))
        side = getattr(execution, "side", "")
        action = "SELL" if side == "SLD" else "BUY"
        contracts = getattr(execution, "shares", 0)
        fill_price = getattr(execution, "avgPrice", None)
        if fill_price is None:
            fill_price = getattr(execution, "price", None)
        ts = _execution_ts_iso(execution)

        commission_report = fill.commissionReport
        has_commission_report = bool(getattr(commission_report, "execId", ""))
        trade_id = log_trade(
            ts=ts,
            symbol=symbol,
            action=action,
            contracts=contracts,
            fill_price=fill_price,
            realized_pnl=commission_report.realizedPNL
            if has_commission_report
            else None,
            commission=commission_report.commission
            if has_commission_report
            else None,
            is_reversal=False,
            exec_id=exec_id,
        )
        if trade_id == -1:
            return

        signed_qty = contracts if action == "BUY" else -contracts
        update_expected(symbol, signed_qty)
        _schedule_push_trades_to_relay()
        log.info(
            f"TARGET_FILL | symbol={symbol} fill_price={fill_price} "
            f"contracts={contracts} action={action}"
        )
    except Exception as exc:
        log.exception(f"TARGET_FILL_HANDLER_FAILED | {repr(exc)}")


def _subscribe_ib_events(ib: IB) -> None:
    if handle_exec_details not in ib.execDetailsEvent:
        ib.execDetailsEvent += handle_exec_details


# ---------------------------------------------------------------------------
# Startup: verify Gateway is reachable
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def verify_gateway():
    global APP_EVENT_LOOP, APP_STARTED_AT
    APP_EVENT_LOOP = asyncio.get_running_loop()
    if APP_STARTED_AT is None:
        APP_STARTED_AT = datetime.now()
    if OPS_API_KEY == "changeme":
        log.warning("OPS_API_KEY is using default fallback value")
    asyncio.create_task(push_positions_loop())

    def _check():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ib = IB()
        try:
            ib.connect(cfg.IB_HOST, cfg.IB_PORT, clientId=99, timeout=5)
            _subscribe_ib_events(ib)
            log_event("RECONNECT", "connected")
            _sync_ib_state(ib)
            accounts = ib.managedAccounts()
            reconcile(_positions_by_symbol(ib))
            log.info(f"GATEWAY OK | accounts={accounts}")
        except Exception as e:
            log.error(f"GATEWAY UNREACHABLE at startup | {repr(e)}")
        finally:
            if ib.isConnected():
                log_event("RECONNECT", "disconnected")
                ib.disconnect()
            loop.close()

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _check)
    asyncio.create_task(background_reconcile())


async def background_reconcile():
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(900)
        try:
            log.info("BACKGROUND_RECONCILE | starting")
            await loop.run_in_executor(None, _background_reconcile_sync)
            log.info("BACKGROUND_RECONCILE | complete")
        except Exception as e:
            log.warning(f"BACKGROUND_RECONCILE_FAILED | {repr(e)}")


def _background_reconcile_sync():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ib = IB()
    try:
        ib.connect(cfg.IB_HOST, cfg.IB_PORT, clientId=97, timeout=5)
        _subscribe_ib_events(ib)
        _sync_ib_state(ib)
        _log_background_position_drift(_positions_by_symbol(ib))
    finally:
        if ib.isConnected():
            ib.disconnect()
        loop.close()


def _log_background_position_drift(positions: dict[str, float]) -> None:
    # Auto-correction disabled — ledger is source of truth.
    # IBKR position callbacks are async and may lag fills by several seconds.
    # Manual correction required if drift persists across multiple reconcile cycles.
    ledger = ledger_module.load_ledger()
    normalized_positions = {
        symbol.strip().upper(): float(quantity)
        for symbol, quantity in positions.items()
    }

    for symbol, entry in ledger.items():
        expected = float(entry.get("expected", 0))
        actual = float(normalized_positions.get(symbol, 0))
        delta = actual - expected
        if abs(delta) > 0.01:
            log.warning(
                f"POSITION_DRIFT | symbol={symbol} expected={expected} actual={actual} delta={delta}"
            )
        else:
            log.debug(f"POSITION_OK | symbol={symbol} position={expected}")

    for symbol, actual in normalized_positions.items():
        if symbol in ledger or abs(actual) <= 0.01:
            continue
        log.warning(
            f"POSITION_DRIFT | symbol={symbol} expected=0.0 actual={actual} delta={actual}"
        )


def _require_ops_api_key(request: Request) -> None:
    if request.headers.get("X-API-Key") != OPS_API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


def _get_last_event_ts() -> str | None:
    with sqlite3.connect(OPS_DB_PATH) as conn:
        row = conn.execute("SELECT ts FROM events ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def _get_last_reconnect_status() -> str:
    with sqlite3.connect(OPS_DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT detail
            FROM events
            WHERE event_type = 'RECONNECT'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    return row[0] if row and row[0] in ("connected", "disconnected") else "unknown"


def _read_expected_positions() -> dict[str, int]:
    if hasattr(ledger_module, "get_all_expected_positions"):
        data = ledger_module.get_all_expected_positions()
    else:
        try:
            with open(cfg.LEDGER_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}

    positions: dict[str, int] = {}
    for symbol, entry in data.items():
        expected = entry.get("expected", 0) if isinstance(entry, dict) else entry
        positions[str(symbol).strip().upper()] = int(round(float(expected)))
    return positions


def _read_position_last_updated() -> dict[str, str | None]:
    try:
        with open(cfg.LEDGER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    last_updated: dict[str, str | None] = {}
    for symbol, entry in data.items():
        value = entry.get("last_updated") if isinstance(entry, dict) else None
        last_updated[str(symbol).strip().upper()] = value
    return last_updated


def get_entry_price(symbol) -> float | None:
    try:
        with sqlite3.connect(OPS_DB_PATH) as conn:
            row = conn.execute(
                """
                SELECT detail FROM events
                WHERE symbol = ? AND event_type = 'FILL'
                ORDER BY id DESC LIMIT 1
                """,
                (symbol,),
            ).fetchone()
        if not row:
            return None
        detail = json.loads(row[0])
        fill_price = detail.get("fill_price")
        return float(fill_price) if fill_price is not None else None
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Vikram adapter
# ---------------------------------------------------------------------------

SIGNAL_MAP = {
    "BUY":    ("BUY",  1),
    "SELL":   ("SELL", 1),
    "R-BUY":  ("BUY",  1),
    "R-SELL": ("SELL", 1),
}

SYMBOL_MAP = {
    "MESM2026": "MES",
    "MNQM2026": "MNQ",
    "GC1!": "MGC",
    "MCLM2026": "MCL",
}

TICK_SIZES = {
    "MES": 0.25,
    "MNQ": 0.25,
    "MGC": 0.10,
    "MCL": 0.01,
}

MARKET_FILL_TIMEOUT_SECONDS = 120
FRONT_MONTH_MIN_DAYS_TO_EXPIRY = 10


def normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if normalized in SYMBOL_MAP:
        return SYMBOL_MAP[normalized]
    base = re.sub(r"[FGHJKMNQUVXZ]\d{4}$", "", normalized)
    return base if base else normalized


def round_to_tick(price: float, symbol: str) -> float:
    tick = TICK_SIZES.get(symbol, 0.25)
    return round(round(price / tick) * tick, 10)

def adapt_vikram_signal(data: dict) -> dict:
    raw_signal = str(data.get("signal", "")).upper()

    if raw_signal not in SIGNAL_MAP:
        raise ValueError(f"Unrecognized signal type: '{raw_signal}'. "
                         f"Expected one of: {list(SIGNAL_MAP.keys())}")

    action, contracts = SIGNAL_MAP[raw_signal]
    symbol = normalize_symbol(str(data.get("symbol", "")))

    if not symbol:
        raise ValueError("Missing symbol")

    try:
        target = float(data["target"])
    except KeyError as e:
        raise ValueError("Missing target") from e
    except (TypeError, ValueError) as e:
        raise ValueError(f"Invalid target: {data.get('target')}") from e

    return {
        "symbol":      symbol,
        "action":      action,
        "contracts":   contracts,
        "target":      target,
        "order_type":  "MKT",
        "is_reversal": raw_signal.startswith("R-"),
        "raw_signal":  raw_signal,
    }


# ---------------------------------------------------------------------------
# IBKR execution
# ---------------------------------------------------------------------------


def _tracked_limit(symbol: str) -> dict | None:
    with open_limit_orders_lock:
        entry = open_limit_orders.get(symbol)
        return dict(entry) if entry else None


def _set_tracked_limit(symbol: str, order_id: int, target: float, con_id: int) -> None:
    with open_limit_orders_lock:
        open_limit_orders[symbol] = {
            "order_id": order_id,
            "target": target,
            "con_id": con_id,
        }


def _clear_tracked_limit(symbol: str) -> None:
    with open_limit_orders_lock:
        open_limit_orders.pop(symbol, None)


def _contract_last_trade_date(value: str) -> date:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError("Missing lastTradeDateOrContractMonth")

    if len(normalized) == 6:
        year = int(normalized[:4])
        month = int(normalized[4:6])
        if month == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)
        return next_month - timedelta(days=1)

    return datetime.strptime(normalized[:8], "%Y%m%d").date()


def get_front_month_contract(ib: IB, symbol: str) -> Future:
    root_symbol = symbol.strip().upper()
    spec = cfg.CONTRACT_SPECS.get(root_symbol)
    if spec is None:
        supported = ", ".join(cfg.CONTRACT_SPECS.keys())
        raise ValueError(
            f"Unsupported futures symbol '{symbol}'. "
            f"Supported: {supported}"
        )

    contract = Future(
        symbol=root_symbol,
        exchange=spec["exchange"],
        currency=spec["currency"],
    )
    details = ib.reqContractDetails(contract)
    cutoff = date.today() + timedelta(days=FRONT_MONTH_MIN_DAYS_TO_EXPIRY)

    valid_contracts: list[Future] = []
    for detail in details:
        last_trade = detail.contract.lastTradeDateOrContractMonth
        if not last_trade:
            continue
        if _contract_last_trade_date(last_trade) > cutoff:
            valid_contracts.append(detail.contract)

    if not valid_contracts:
        raise RuntimeError(
            f"No active front-month contract found for {root_symbol} on {spec['exchange']}"
        )

    valid_contracts.sort(key=lambda c: c.lastTradeDateOrContractMonth)
    return valid_contracts[0]

async def place_order(normalized: dict) -> dict:
    if cfg.DRY_RUN:
        log.info(
            f"DRY RUN | would place {normalized['action']} "
            f"{normalized['contracts']}x {normalized['symbol']} MKT "
            f"with target={normalized['target']}"
        )
        return {
            "order_id":   -1,
            "status":     "DryRun",
            "fill_price": None,
            "contracts":  normalized["contracts"],
            "action":     normalized["action"],
            "limit_order_id": -1,
            "target": normalized["target"],
        }

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_place_order_sync, normalized))


def _place_order_sync(normalized: dict) -> dict:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    ib = IB()
    try:
        with ib_execution_lock:
            try:
                ib.connect(cfg.IB_HOST, cfg.IB_PORT, clientId=cfg.IB_CLIENT_ID, timeout=10)
                _subscribe_ib_events(ib)
                log_event("RECONNECT", "connected")
            except Exception as e:
                raise RuntimeError(
                    f"Cannot connect to IB Gateway at {cfg.IB_HOST}:{cfg.IB_PORT} — {e}"
                )

            _sync_ib_state(ib)
            reconcile(_positions_by_symbol(ib))
            contract = get_front_month_contract(ib, normalized["symbol"])
            ib.qualifyContracts(contract)

            _cancel_all_open_orders_for_symbol(
                ib=ib,
                contract=contract,
                symbol=normalized["symbol"],
            )

            market_contracts = normalized["contracts"]
            if normalized["is_reversal"]:
                positions_now = _positions_by_symbol(ib)
                live_position = int(positions_now.get(normalized["symbol"], 0))
                log.info(
                    f"REVERSAL_SIZING | symbol={normalized['symbol']} "
                    f"live_position={live_position} "
                    f"ledger_position={get_expected_position(normalized['symbol'])}"
                )
                market_contracts = _reversal_contracts(
                    action=normalized["action"],
                    current_position=live_position,
                )

            order = MarketOrder(
                action=normalized["action"],
                totalQuantity=market_contracts,
                account=cfg.IB_ACCOUNT or None,
                tif='DAY',
            )

            trade = ib.placeOrder(contract, order)
            log_event(
                "ORDER",
                json.dumps(
                    {
                        "side": normalized["action"],
                        "qty": market_contracts,
                        "order_type": "MKT",
                    }
                ),
                symbol=normalized["symbol"],
            )
            fill_price = _wait_for_fill(
                ib,
                trade,
                is_reversal=normalized["is_reversal"],
                timeout_seconds=MARKET_FILL_TIMEOUT_SECONDS,
            )
            signed_qty = market_contracts if normalized["action"] == "BUY" else -market_contracts
            update_expected(normalized["symbol"], signed_qty)
            exit_quantity = normalized["contracts"]

            limit_trade = _place_exit_limit(
                ib=ib,
                contract=contract,
                symbol=normalized["symbol"],
                entry_action=normalized["action"],
                target=normalized["target"],
                quantity=exit_quantity,
            )

            _set_tracked_limit(
                symbol=normalized["symbol"],
                order_id=limit_trade.order.orderId,
                target=normalized["target"],
                con_id=contract.conId,
            )

            return {
                "order_id":   trade.order.orderId,
                "status":     trade.orderStatus.status,
                "fill_price": fill_price,
                "contracts":  market_contracts,
                "action":     normalized["action"],
                "limit_order_id": limit_trade.order.orderId,
                "target": normalized["target"],
            }

    finally:
        if ib.isConnected():
            log_event("RECONNECT", "disconnected")
            ib.disconnect()
        loop.close()


async def update_target(symbol: str, new_target: float, position: float) -> dict:
    if cfg.DRY_RUN:
        closing_action = "SELL" if position > 0 else "BUY"
        log.info(
            f"DRY RUN | would update target for {symbol} "
            f"new_target={new_target} closing_action={closing_action}"
        )
        return {"ok": True, "action": "target_updated", "new_target": new_target}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, partial(_update_target_sync, symbol, new_target, position)
    )


def _update_target_sync(symbol: str, new_target: float, position: float) -> dict:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ib = IB()
    try:
        with ib_execution_lock:
            try:
                ib.connect(cfg.IB_HOST, cfg.IB_PORT, clientId=cfg.IB_CLIENT_ID, timeout=10)
                _subscribe_ib_events(ib)
                log_event("RECONNECT", "connected")
            except Exception as e:
                raise RuntimeError(
                    f"Cannot connect to IB Gateway at {cfg.IB_HOST}:{cfg.IB_PORT} — {e}"
                )

            _sync_ib_state(ib)
            contract = get_front_month_contract(ib, symbol)
            ib.qualifyContracts(contract)

            old_entry = _tracked_limit(symbol)
            old_target = old_entry["target"] if old_entry else None

            _cancel_all_open_orders_for_symbol(ib=ib, contract=contract, symbol=symbol)

            log.info(
                f"TARGET_UPDATE | symbol={symbol} old_target={old_target} "
                f"cancelled new_target={new_target}"
            )

            entry_action = "BUY" if position > 0 else "SELL"
            limit_trade = _place_exit_limit(
                ib=ib,
                contract=contract,
                symbol=symbol,
                entry_action=entry_action,
                target=new_target,
                quantity=1,
            )

            _set_tracked_limit(
                symbol=symbol,
                order_id=limit_trade.order.orderId,
                target=new_target,
                con_id=contract.conId,
            )

            return {"ok": True, "action": "target_updated", "new_target": new_target}
    finally:
        if ib.isConnected():
            log_event("RECONNECT", "disconnected")
            ib.disconnect()
        loop.close()


def _reversal_contracts(action: str, current_position: int) -> int:
    if action == "BUY" and current_position < 0:
        return abs(current_position) + 1
    if action == "SELL" and current_position > 0:
        return abs(current_position) + 1
    return 1


def _current_position_size(ib: IB, contract: Future) -> int:
    positions = ib.positions(account=cfg.IB_ACCOUNT) if cfg.IB_ACCOUNT else ib.positions()
    for position in positions:
        if position.contract.conId == contract.conId:
            return int(position.position)
    return 0


def _open_trades_for_symbol(ib: IB, contract: Future, symbol: str) -> list:
    ib.reqOpenOrders()
    ib.sleep(1.0)

    matches = []
    for trade in ib.openTrades():
        same_conid = (
            contract.conId
            and trade.contract.conId
            and trade.contract.conId == contract.conId
        )
        same_symbol = trade.contract.symbol == symbol
        if same_conid or same_symbol:
            matches.append(trade)
    return matches


def _cancel_all_open_orders_for_symbol(ib: IB, contract: Future, symbol: str) -> None:
    open_trades = _open_trades_for_symbol(ib, contract, symbol)
    if not open_trades:
        return

    for trade in open_trades:
        if trade.orderStatus.status in ("Cancelled", "Inactive", "Filled", "ApiCancelled"):
            continue
        ib.cancelOrder(trade.order)
        confirmed = False
        deadline = time.time() + 2
        while time.time() < deadline:
            ib.sleep(0.1)
            if trade.orderStatus.status in ("Cancelled", "ApiCancelled"):
                confirmed = True
                break
        if confirmed:
            log.info(f"CANCELLED_STALE | symbol={symbol} order_id={trade.order.orderId}")
        else:
            log.warning(
                f"CANCEL_TIMEOUT | symbol={symbol} order_id={trade.order.orderId} "
                f"status={trade.orderStatus.status}"
            )

    _clear_tracked_limit(symbol)


def _wait_for_fill(ib: IB, trade, is_reversal: bool, timeout_seconds: int) -> float:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        ib.sleep(0.25)
        status = trade.orderStatus.status
        if status == "Filled":
            exec_id = ""
            fill = trade.fills[-1] if trade.fills else None
            if trade.fills:
                exec_id = fill.execution.execId
            log_event(
                "FILL",
                json.dumps(
                    {
                        "fill_price": trade.orderStatus.avgFillPrice,
                        "qty": trade.order.totalQuantity,
                        "exec_id": exec_id,
                    }
                ),
                symbol=trade.contract.symbol,
            )
            if fill is not None:
                execution = fill.execution
                commission_report = fill.commissionReport
                has_commission_report = bool(getattr(commission_report, "execId", ""))
                execution_time = execution.time
                if isinstance(execution_time, datetime):
                    if execution_time.tzinfo is None:
                        execution_time = execution_time.replace(tzinfo=timezone.utc)
                    ts = execution_time.astimezone(timezone.utc).replace(
                        microsecond=0
                    ).isoformat().replace("+00:00", "Z")
                else:
                    ts = str(execution_time)

                trade_id = log_trade(
                    ts=ts,
                    symbol=trade.contract.symbol,
                    action="SELL" if execution.side == "SLD" else "BUY",
                    contracts=execution.shares,
                    fill_price=execution.avgPrice,
                    realized_pnl=commission_report.realizedPNL
                    if has_commission_report
                    else None,
                    commission=commission_report.commission
                    if has_commission_report
                    else None,
                    is_reversal=is_reversal,
                    exec_id=exec_id,
                )
                if trade_id != -1:
                    _schedule_push_trades_to_relay()
            return trade.orderStatus.avgFillPrice
        if status in ("Cancelled", "Inactive", "ApiCancelled"):
            raise RuntimeError(f"Market order did not fill: status={status}")

    raise RuntimeError(
        f"Timed out waiting for fill confirmation on order_id={trade.order.orderId}"
    )


def _place_exit_limit(
    ib: IB,
    contract: Future,
    symbol: str,
    entry_action: str,
    target: float,
    quantity: int,
):
    exit_action = "SELL" if entry_action == "BUY" else "BUY"
    rounded_target = round_to_tick(target, symbol)
    limit_order = LimitOrder(
        action=exit_action,
        totalQuantity=quantity,
        lmtPrice=rounded_target,
        account=cfg.IB_ACCOUNT or None,
        tif="DAY",
    )
    limit_trade = ib.placeOrder(contract, limit_order)
    log_event(
        "ORDER",
        json.dumps(
            {
                "side": exit_action,
                "qty": quantity,
                "order_type": "LMT",
            }
        ),
        symbol=symbol,
    )

    deadline = time.time() + 10
    while time.time() < deadline:
        ib.sleep(0.25)
        if limit_trade.orderStatus.status in (
            "PendingSubmit",
            "PreSubmitted",
            "Submitted",
        ):
            break
        if limit_trade.orderStatus.status in ("Cancelled", "Inactive", "ApiCancelled"):
            raise RuntimeError(
                f"Exit limit order rejected: status={limit_trade.orderStatus.status}"
            )

    log.info(f"LIMIT PLACED | symbol={symbol} target={rounded_target}")
    return limit_trade


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------

@app.post("/execute")
async def execute(request: Request):
    token = request.headers.get("X-Forward-Secret", "")
    if not cfg.SOLO_TRADER_SECRET or token != cfg.SOLO_TRADER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()
    symbol     = data.get("symbol", "UNKNOWN")
    raw_signal = data.get("signal", "UNKNOWN")
    asset_cls  = str(data.get("asset_class", "")).lower()
    normalized_symbol = normalize_symbol(str(data.get("symbol", ""))) if data.get("symbol") else None

    log_event(
        "WEBHOOK",
        json.dumps(data, sort_keys=True, default=str),
        symbol=normalized_symbol,
    )

    log.info(f"RECEIVED | symbol={symbol} signal={raw_signal} "
             f"asset_class={asset_cls}")

    if asset_cls != "futures":
        log.info(f"IGNORED  | asset_class='{asset_cls}' — not futures, skipping execution")
        return {"ok": True, "reason": "ignored_non_futures"}

    try:
        normalized = adapt_vikram_signal(data)
    except ValueError as e:
        log.warning(f"REJECTED | bad signal | {e}")
        raise HTTPException(status_code=422, detail=str(e))

    if state.is_duplicate(normalized["symbol"], normalized["raw_signal"]):
        log.warning(f"REJECTED | duplicate | symbol={symbol} signal={raw_signal}")
        return {"ok": False, "reason": "duplicate"}

    if not normalized["is_reversal"]:
        current_position = get_expected_position(normalized["symbol"])
        is_same_direction = (
            (normalized["action"] == "BUY" and current_position > 0) or
            (normalized["action"] == "SELL" and current_position < 0)
        )
        if is_same_direction:
            try:
                await asyncio.wait_for(_IB_EXECUTION_LOCK.acquire(), timeout=30)
            except asyncio.TimeoutError:
                log.warning(
                    f"TIMEOUT | symbol={normalized['symbol']} "
                    f"signal={normalized['raw_signal']} — skipped, execution slot busy"
                )
                return {"ok": False, "reason": "execution_slot_busy"}
            try:
                result = await update_target(
                    symbol=normalized["symbol"],
                    new_target=normalized["target"],
                    position=current_position,
                )
                return result
            except Exception as e:
                log.error(f"EXEC ERROR | {repr(e)}")
                log_event("ERROR", str(e), symbol=normalized.get("symbol"))
                raise HTTPException(status_code=500, detail="Order execution failed")
            finally:
                _IB_EXECUTION_LOCK.release()

    log.info(
        f"QUEUED | symbol={normalized['symbol']} "
        f"signal={normalized['raw_signal']} waiting for execution slot"
    )
    try:
        await asyncio.wait_for(_IB_EXECUTION_LOCK.acquire(), timeout=30)
    except asyncio.TimeoutError:
        log.warning(
            f"TIMEOUT | symbol={normalized['symbol']} "
            f"signal={normalized['raw_signal']} — skipped, execution slot busy"
        )
        return {"ok": False, "reason": "execution_slot_busy"}

    try:
        try:
            log.info(
                f"EXECUTING | symbol={normalized['symbol']} "
                f"signal={normalized['raw_signal']}"
            )
            result = await place_order(normalized)

            log.info(
                f"FILLED   | order_id={result['order_id']} fill_price={result['fill_price']} "
                f"contracts={result['contracts']} action={result['action']} "
                f"reversal={normalized['is_reversal']}"
            )
            return {"ok": True, "result": result}

        except Exception as e:
            log.error(f"EXEC ERROR | {repr(e)}")
            log_event("ERROR", str(e), symbol=normalized.get("symbol"))
            raise HTTPException(status_code=500, detail="Order execution failed")
    finally:
        _IB_EXECUTION_LOCK.release()


@app.get("/gateway-health")
async def gateway_health():
    def _check():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ib = IB()
        try:
            ib.connect(cfg.IB_HOST, cfg.IB_PORT, clientId=98, timeout=5)
            _subscribe_ib_events(ib)
            log_event("RECONNECT", "connected")
            _sync_ib_state(ib)
            accounts = ib.managedAccounts()
            reconcile(_positions_by_symbol(ib))
            ib.disconnect()
            log_event("HEALTH", json.dumps({"ok": True, "accounts": accounts}))
            return {"ok": True, "accounts": accounts}
        except Exception as e:
            log_event("HEALTH", json.dumps({"ok": False, "error": repr(e)}))
            return {"ok": False, "error": repr(e)}
        finally:
            if ib.isConnected():
                log_event("RECONNECT", "disconnected")
                ib.disconnect()
            loop.close()

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _check)

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ops/health")
async def ops_health(request: Request):
    _require_ops_api_key(request)
    ib_status = _get_last_reconnect_status()
    ib_connected = ib_status == "connected"
    uptime_seconds = 0
    if APP_STARTED_AT is not None:
        uptime_seconds = int((datetime.now() - APP_STARTED_AT).total_seconds())

    return {
        "status": "ok",
        "ib_connected": ib_connected,
        "ib_status": ib_status,
        "uptime_seconds": uptime_seconds,
        "last_event_ts": _get_last_event_ts(),
    }


@app.get("/ops/positions")
async def ops_positions(request: Request):
    _require_ops_api_key(request)
    last_updated = _read_position_last_updated()
    positions = []
    for symbol, size in _read_expected_positions().items():
        if size == 0:
            continue
        positions.append(
            {
                "symbol": symbol,
                "size": size,
                "direction": "LONG" if size > 0 else "SHORT",
                "entry_price": get_entry_price(symbol),
                "last_updated": last_updated.get(symbol),
            }
        )
    return {
        "positions": positions
    }


@app.get("/ops/events")
async def ops_events(request: Request, limit: int = 100, event_type: str | None = None):
    _require_ops_api_key(request)
    limit = max(1, min(limit, 200))

    with sqlite3.connect(OPS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if event_type:
            rows = conn.execute(
                """
                SELECT id, ts, symbol, event_type, detail
                FROM events
                WHERE event_type = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (event_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, ts, symbol, event_type, detail
                FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    return {
        "events": [
            {
                "id": row["id"],
                "ts": row["ts"],
                "symbol": row["symbol"],
                "event_type": row["event_type"],
                "detail": row["detail"],
            }
            for row in rows
        ]
    }


@app.get("/ops/trades")
async def ops_trades(request: Request, limit: int = 200, symbol: str | None = None):
    _require_ops_api_key(request)
    limit = max(1, min(limit, 500))
    with sqlite3.connect(OPS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if symbol:
            rows = conn.execute(
                """
                SELECT id, ts, symbol, action, contracts, fill_price,
                       realized_pnl, commission, net_pnl, is_reversal
                FROM trades
                WHERE symbol = ?
                ORDER BY id DESC LIMIT ?
                """,
                (symbol.strip().upper(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, ts, symbol, action, contracts, fill_price,
                       realized_pnl, commission, net_pnl, is_reversal
                FROM trades
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return {
        "trades": [
            {
                "id":           row["id"],
                "ts":           row["ts"],
                "symbol":       row["symbol"],
                "action":       row["action"],
                "contracts":    row["contracts"],
                "fill_price":   row["fill_price"],
                "realized_pnl": row["realized_pnl"],
                "commission":   row["commission"],
                "net_pnl":      row["net_pnl"],
                "is_reversal":  bool(row["is_reversal"]),
            }
            for row in rows
        ]
    }
