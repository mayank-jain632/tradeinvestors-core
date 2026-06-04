import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone


DB_PATH = os.getenv("OPS_DB_PATH", os.path.expanduser("~/home-lab/apps/solo_trader_vikrim/ops.db"))
_LOCK = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                symbol TEXT,
                event_type TEXT,
                detail TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                symbol TEXT,
                action TEXT,
                contracts REAL,
                fill_price REAL,
                realized_pnl REAL,
                commission REAL,
                net_pnl REAL,
                is_reversal INTEGER,
                exec_id TEXT,
                pushed INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_exec_id
            ON trades(exec_id)
            WHERE exec_id IS NOT NULL AND exec_id != ''
            """
        )
        conn.commit()


def log_event(event_type, detail, symbol=None) -> None:
    try:
        with _LOCK:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT INTO events (ts, symbol, event_type, detail) VALUES (?, ?, ?, ?)",
                    (_utc_now_iso(), symbol, event_type, detail),
                )
                conn.commit()
    except Exception as exc:
        print(f"log_event failed: {exc}", file=sys.stderr)


def log_trade(
    ts,
    symbol,
    action,
    contracts,
    fill_price,
    realized_pnl,
    commission,
    is_reversal,
    exec_id,
) -> int:
    try:
        net_pnl = None
        if realized_pnl is not None and commission is not None:
            net_pnl = realized_pnl - commission

        with _LOCK:
            with sqlite3.connect(DB_PATH) as conn:
                if exec_id:
                    existing = conn.execute(
                        "SELECT id FROM trades WHERE exec_id = ?",
                        (exec_id,),
                    ).fetchone()
                    if existing:
                        return -1

                cursor = conn.execute(
                    """
                    INSERT INTO trades (
                        ts, symbol, action, contracts, fill_price,
                        realized_pnl, commission, net_pnl, is_reversal,
                        exec_id, pushed
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        ts,
                        symbol,
                        action,
                        contracts,
                        fill_price,
                        realized_pnl,
                        commission,
                        net_pnl,
                        1 if is_reversal else 0,
                        exec_id,
                    ),
                )
                conn.commit()
                return cursor.lastrowid
    except sqlite3.IntegrityError:
        return -1
    except Exception as exc:
        print(f"log_trade failed: {exc}", file=sys.stderr)
        return -1


def get_unpushed_trades() -> list[dict]:
    try:
        with _LOCK:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM trades WHERE pushed = 0 ORDER BY id ASC"
                ).fetchall()
                return [dict(row) for row in rows]
    except Exception as exc:
        print(f"get_unpushed_trades failed: {exc}", file=sys.stderr)
        return []


def mark_trade_pushed(trade_id) -> None:
    try:
        with _LOCK:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE trades SET pushed = 1 WHERE id = ?", (trade_id,))
                conn.commit()
    except Exception as exc:
        print(f"mark_trade_pushed failed: {exc}", file=sys.stderr)


_init_db()
