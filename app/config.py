import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Auth
    SOLO_TRADER_SECRET: str = os.getenv("SOLO_TRADER_SECRET", "")

    # IB Gateway
    IB_HOST: str      = os.getenv("IB_HOST", "127.0.0.1")
    IB_PORT: int      = int(os.getenv("IB_PORT", "4002"))       # 4002 = paper, 4001 = live
    IB_CLIENT_ID: int = int(os.getenv("IB_CLIENT_ID", "1"))
    IB_ACCOUNT: str   = os.getenv("IB_ACCOUNT", "")             # e.g. DU1234567

    # Dry run — set to true to skip IB Gateway connection entirely
    DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() == "true"

    # Dedup
    DEDUP_WINDOW_SECONDS: int = int(os.getenv("DEDUP_WINDOW_SECONDS", "300"))
    LEDGER_PATH: str = os.getenv("LEDGER_PATH", "")

    # Stable IB futures metadata. Front month is resolved dynamically at runtime.
    CONTRACT_SPECS: dict[str, dict[str, str]] = {
        "MES": {
            "exchange": "CME",
            "currency": "USD",
        },
        "MNQ": {
            "exchange": "CME",
            "currency": "USD",
        },
        "MGC": {
            "exchange": "COMEX",
            "currency": "USD",
        },
        "MCL": {
            "exchange": "NYMEX",
            "currency": "USD",
        },
    }

cfg = Config()
