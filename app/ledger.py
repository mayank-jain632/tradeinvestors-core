import json
import logging
import os
from datetime import datetime, timezone

from config import cfg


log = logging.getLogger("solo_trader")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_ledger() -> dict:
    try:
        with open(cfg.LEDGER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_ledger(data: dict) -> None:
    directory = os.path.dirname(cfg.LEDGER_PATH) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{cfg.LEDGER_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp_path, cfg.LEDGER_PATH)


def update_expected(symbol: str, signed_qty: float) -> None:
    normalized_symbol = symbol.strip().upper()
    ledger = load_ledger()
    current = ledger.get(normalized_symbol, {})
    expected = float(current.get("expected", 0))
    ledger[normalized_symbol] = {
        "expected": expected + signed_qty,
        "last_updated": _utc_now_iso(),
    }
    save_ledger(ledger)


def get_expected_position(symbol: str) -> float:
    normalized_symbol = symbol.strip().upper()
    ledger = load_ledger()
    entry = ledger.get(normalized_symbol, {})
    return float(entry.get("expected", 0))


def reconcile(positions: dict[str, float]) -> None:
    ledger = load_ledger()
    normalized_positions = {
        symbol.strip().upper(): float(quantity)
        for symbol, quantity in positions.items()
    }
    changed = False

    for symbol, entry in ledger.items():
        expected = float(entry.get("expected", 0))
        actual = float(normalized_positions.get(symbol, 0))
        delta = actual - expected
        if abs(delta) > 0.01:
            log.warning(
                f"POSITION_DRIFT | symbol={symbol} expected={expected} actual={actual} delta={delta} "
                f"— auto-correcting ledger"
            )
            ledger[symbol] = {
                "expected": actual,
                "last_updated": _utc_now_iso(),
            }
            changed = True
        else:
            log.debug(f"POSITION_OK | symbol={symbol} position={expected}")

    for symbol, actual in normalized_positions.items():
        if symbol in ledger or abs(actual) <= 0.01:
            continue
        log.warning(f"UNTRACKED_POSITION | symbol={symbol} actual={actual} — adding to ledger")
        ledger[symbol] = {
            "expected": actual,
            "last_updated": _utc_now_iso(),
        }
        changed = True

    if changed:
        save_ledger(ledger)
