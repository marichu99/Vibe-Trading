"""Built-in MetaTrader 5 connector profiles.

MT5 is reached by attaching to a MetaTrader 5 terminal already installed and
signed into a broker account on this machine (the official ``MetaTrader5``
Python package talks to the terminal over local IPC — the same "attach to
what's already open" design as the IBKR local connector, just without a
TCP host/port). No broker credentials pass through Vibe-Trading: whichever
account is signed into the terminal is the account these profiles read/trade.
Every call re-verifies the signed-in account's own ``trade_mode`` against the
selected profile and refuses to proceed on a mismatch.

The demo profile places real orders against the terminal's demo account —
useful for evaluating futures/CFD/forex strategies without risking capital.

``mt5-live-trade`` places real orders against whatever account is signed into
the terminal when it is NOT a demo account. Every live order still routes
through Vibe-Trading's mandate gate (``src.live.sdk_order_gate``) exactly like
the app's other live brokers — it is denied unless a committed mandate exists
for broker key ``"mt5"`` (see ``scripts/commit_mt5_mandate.py``) authorizing
the ``CFD`` instrument type and the order's asset class (``forex``/
``commodity``/``us_index`` — ``src.trading.service._mt5_asset_class``).
"""

from __future__ import annotations

from src.trading.types import READ_CAPABILITIES, TradingProfile

MT5_PROFILES: tuple[TradingProfile, ...] = (
    TradingProfile(
        id="mt5-demo-trade",
        connector="mt5",
        label="MetaTrader 5 Demo · Local Terminal Trade",
        environment="paper",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES + ("orders.place",),
        readonly=False,
        config={"profile": "paper"},
        notes=(
            "Reads and places orders against whatever account is signed into your "
            "local MT5 terminal. Refuses to trade if that account is not a demo "
            "account. No credentials are stored by Vibe-Trading."
        ),
    ),
    TradingProfile(
        id="mt5-live-readonly",
        connector="mt5",
        label="MetaTrader 5 Live · Local Terminal Read-Only",
        environment="live",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "live-readonly"},
        notes="Reads a local live MT5 terminal session only. Order placement is not exposed in this profile.",
    ),
    TradingProfile(
        id="mt5-live-trade",
        connector="mt5",
        label="MetaTrader 5 Live · Local Terminal Trade",
        environment="live",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES + ("orders.place",),
        readonly=False,
        config={"profile": "live-trade"},
        notes=(
            "Places REAL orders against whatever account is signed into your local "
            "MT5 terminal. Refuses to trade if that account is a demo account. "
            "Every order is gated by the committed mt5 mandate (hard caps, kill "
            "switch, audit log) before it reaches the broker — see "
            "scripts/commit_mt5_mandate.py."
        ),
    ),
)
