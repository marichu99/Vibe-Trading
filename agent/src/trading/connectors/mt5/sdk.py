"""MetaTrader 5 connector via the official ``MetaTrader5`` package (Windows-only).

Unlike the other ``broker_sdk`` connectors, MT5 holds no API key pair. The
``MetaTrader5`` package talks over local IPC to a MetaTrader 5 terminal that is
already installed and signed into a broker account on this machine — the same
"attach to what the user already has open" design as the IBKR local connector,
just without a TCP host/port (the package finds the running terminal itself, or
launches one at an explicit ``terminal_path``). No broker credentials pass
through Vibe-Trading: whichever account is signed into the terminal is the
account every call in this module reads or trades.

Paper-vs-live is NOT a separate host or key pair here — it is the terminal's own
signed-in account. ``check_status``/every read+write call re-verifies the
account's ``trade_mode`` against the configured profile (``paper`` requires a
demo account, ``live-readonly`` tolerates any) and fails closed on a mismatch,
mirroring the IBKR paper/live account guard. This is what stops a "demo"
profile from silently trading a funded live account after someone switches
accounts inside the terminal.

Order sizing is in lots (MT5's native unit): ``quantity`` maps straight to lot
volume; ``notional`` is converted to lots using the symbol's contract size and
current price, then rounded to the symbol's volume step and clamped to its
[min, max] range.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

from src.config.paths import get_runtime_root

CONFIG_FILENAME = "mt5.json"

PROFILE_ENVIRONMENTS = {
    "paper": "paper",
    "live-readonly": "live",
    "live-trade": "live",
}

#: MT5 ``ACCOUNT_TRADE_MODE_*`` values, mirrored so the guard works even if the
#: optional package attribute names ever drift (fallback to the documented ints).
_TRADE_MODE_DEMO = 0
_TRADE_MODE_REAL = 2

_PERIOD_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")


class MT5DependencyError(RuntimeError):
    """Raised when the optional ``MetaTrader5`` package is not installed."""


class MT5ConnectionError(RuntimeError):
    """Raised when Vibe-Trading cannot attach to a running MT5 terminal."""


class MT5ProfileMismatchError(RuntimeError):
    """Raised when the signed-in terminal account does not match the profile."""


@dataclass(frozen=True)
class MT5Config:
    """Local MT5 terminal connection settings.

    Args:
        terminal_path: Optional path to ``terminal64.exe`` for multi-terminal
            setups. Empty attaches to whichever terminal instance is running.
        login: Optional account number to select within a multi-account
            terminal. Empty uses the currently signed-in account.
        server: Optional broker server name, paired with ``login``.
        profile: ``paper`` (demo account required) or ``live-readonly``.
        timeout: Terminal IPC connection timeout, in seconds.
        magic: Magic number stamped on orders placed by Vibe-Trading.
        deviation: Allowed price slippage, in points, for market orders.
        readonly: Always true for this layer; the profile-level readonly flag
            is what actually gates order placement.
    """

    terminal_path: str = ""
    login: int | None = None
    server: str = ""
    profile: str = "paper"
    timeout: float = 10.0
    magic: int = 20260000
    deviation: int = 20
    readonly: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None = None) -> "MT5Config":
        """Build a config from a JSON-like mapping, normalizing the profile."""
        payload = dict(data or {})
        profile = str(payload.get("profile") or "paper").strip().lower()
        if profile not in PROFILE_ENVIRONMENTS:
            raise ValueError("profile must be 'paper', 'live-readonly', or 'live-trade'")
        login_raw = payload.get("login")
        return cls(
            terminal_path=str(payload.get("terminal_path") or "").strip(),
            login=int(login_raw) if login_raw not in (None, "") else None,
            server=str(payload.get("server") or "").strip(),
            profile=profile,
            timeout=float(payload.get("timeout") or 10.0),
            magic=int(payload.get("magic") or 20260000),
            deviation=int(payload.get("deviation") or 20),
            readonly=bool(payload.get("readonly", True)),
        )

    def with_overrides(
        self,
        *,
        terminal_path: str | None = None,
        login: int | None = None,
        server: str | None = None,
        profile: str | None = None,
    ) -> "MT5Config":
        """Return a copy with CLI/tool overrides applied."""
        payload = asdict(self)
        if terminal_path is not None:
            payload["terminal_path"] = terminal_path
        if login is not None:
            payload["login"] = login
        if server is not None:
            payload["server"] = server
        if profile is not None:
            payload["profile"] = profile
        return MT5Config.from_mapping(payload)

    @property
    def environment(self) -> str:
        """Return ``paper`` or ``live`` for this profile."""
        return PROFILE_ENVIRONMENTS.get(self.profile, "paper")

    @property
    def is_paper(self) -> bool:
        """Return whether this profile requires a demo account."""
        return self.environment == "paper"


_OVERRIDE_KEYS = ("terminal_path", "login", "server", "profile")


def build_config(profile_config: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None) -> "MT5Config":
    """Resolve config: saved file <- profile defaults <- CLI/tool overrides.

    The generic ``account`` override every trading tool exposes is accepted as
    an alias for ``login`` (selecting an account within a multi-account
    terminal), matching the IBKR local connector's use of the same parameter.
    """
    base = asdict(load_config())
    for key, value in dict(profile_config or {}).items():
        if value is not None:
            base[key] = value
    cfg = MT5Config.from_mapping(base)

    raw = dict(overrides or {})
    if raw.get("login") in (None, "") and raw.get("account") not in (None, ""):
        raw["login"] = raw["account"]
    clean = {k: v for k, v in raw.items() if k in _OVERRIDE_KEYS and v not in (None, "")}
    return cfg.with_overrides(**clean) if clean else cfg


def config_path() -> Path:
    """Return the user-level MT5 config path."""
    return get_runtime_root() / CONFIG_FILENAME


def load_config() -> MT5Config:
    """Load MT5 settings from ``~/.vibe-trading/mt5.json``."""
    path = config_path()
    if not path.exists():
        return MT5Config()
    try:
        return MT5Config.from_mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid MT5 config at {path}: {exc}") from exc


def save_config(config: MT5Config) -> Path:
    """Persist MT5 settings. No secrets are ever stored in this file."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def mt5_available() -> bool:
    """Return whether the optional ``MetaTrader5`` package can be imported."""
    try:
        _require_mt5()
        return True
    except MT5DependencyError:
        return False


def check_status(config: MT5Config | None = None) -> dict[str, Any]:
    """Check SDK readiness and terminal attachment without mutating state."""
    cfg = config or load_config()
    report: dict[str, Any] = {
        "status": "ok",
        "config": asdict(cfg),
        "sdk": {"package": "MetaTrader5", "installed": mt5_available()},
    }
    if not report["sdk"]["installed"]:
        report["status"] = "error"
        report["error"] = (
            "Optional dependency missing: install with `pip install MetaTrader5` "
            "(Windows only, requires a locally installed MT5 terminal)."
        )
        return report

    try:
        snapshot = get_account_snapshot(cfg)
    except Exception as exc:  # noqa: BLE001 - health endpoint should report cleanly
        report["status"] = "error"
        report["error"] = str(exc)
        return report

    report["account"] = {
        "profile": cfg.profile,
        "is_paper": cfg.is_paper,
        "login": snapshot.get("account", {}).get("login"),
        "server": snapshot.get("account", {}).get("server"),
        "trade_mode": snapshot.get("account", {}).get("trade_mode"),
    }
    return report


def get_account_snapshot(config: MT5Config | None = None) -> dict[str, Any]:
    """Fetch the signed-in terminal account's balance/equity summary."""
    cfg = config or load_config()
    module = _connect(cfg)
    account = _call(module, "account_info")
    if account is None:
        raise MT5ConnectionError(f"account_info() failed: {_last_error(module)}")
    _assert_profile(cfg, account, module)
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_paper": cfg.is_paper,
        "account": {
            "login": _obj_get(account, "login"),
            "server": _obj_get(account, "server"),
            "company": _obj_get(account, "company"),
            "currency": _obj_get(account, "currency"),
            "trade_mode": _obj_get(account, "trade_mode"),
            "balance": _obj_get(account, "balance"),
            "equity": _obj_get(account, "equity"),
            "margin": _obj_get(account, "margin"),
            "margin_free": _obj_get(account, "margin_free"),
            "margin_level": _obj_get(account, "margin_level"),
            "leverage": _obj_get(account, "leverage"),
            "trade_allowed": _obj_get(account, "trade_allowed"),
        },
    }


def get_positions(config: MT5Config | None = None) -> dict[str, Any]:
    """Fetch open positions on the signed-in terminal account."""
    cfg = config or load_config()
    module = _connect(cfg)
    account = _call(module, "account_info")
    _assert_profile(cfg, account, module)
    positions = _call(module, "positions_get") or ()
    rows = [_position_to_dict(item) for item in positions]
    return {"status": "ok", "profile": cfg.profile, "is_paper": cfg.is_paper, "positions": rows}


def get_open_orders(config: MT5Config | None = None, *, include_executions: bool = False) -> dict[str, Any]:
    """Fetch resting pending orders and, optionally, recent filled deals."""
    cfg = config or load_config()
    module = _connect(cfg)
    account = _call(module, "account_info")
    _assert_profile(cfg, account, module)
    orders = _call(module, "orders_get") or ()
    result: dict[str, Any] = {
        "status": "ok",
        "profile": cfg.profile,
        "is_paper": cfg.is_paper,
        "open_orders": [_order_to_dict(item) for item in orders],
    }
    if include_executions:
        result["executions"] = [_deal_to_dict(item) for item in _recent_deals(module)]
    return result


def contract_size(symbol: str, *, config: MT5Config | None = None, **_: Any) -> float | None:
    """Return units-per-1.0-lot for ``symbol``, or ``None`` if unreadable.

    MT5 order sizes are lots, not raw units — the live mandate gate's notional
    check (``src.live.sdk_order_gate._contract_multiplier``) calls this to
    convert a lot-based ``quantity`` into real USD notional. Treating quantity
    as raw units (as share/coin connectors do) would understate CFD exposure by
    exactly this factor — e.g. 100x for a standard 100oz gold lot, 100,000x for
    a standard FX lot — silently defeating the mandate's per-order notional cap.
    """
    cfg = config or load_config()
    try:
        module = _connect(cfg)
    except (MT5DependencyError, MT5ConnectionError):
        return None
    clean = symbol.strip()
    _ensure_symbol(module, clean)
    info = _safe_call(module, "symbol_info", clean)
    value = _obj_get(info, "trade_contract_size")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def point_size(symbol: str, *, config: MT5Config | None = None, **_: Any) -> float | None:
    """Return ``symbol``'s minimum price increment (MT5 ``point``), or ``None`` if unreadable.

    Used to size a small strictly-on-the-protective-side buffer for stop
    modifications — ``modify_position`` rejects an SL sitting exactly at
    entry (see its own validation), so callers that want a "breakeven" stop
    need a nonzero nudge past entry, and that nudge should be a handful of
    points, not a value guessed from the price scale (which differs wildly
    between e.g. XAUUSD and EURUSD).
    """
    cfg = config or load_config()
    try:
        module = _connect(cfg)
    except (MT5DependencyError, MT5ConnectionError):
        return None
    clean = symbol.strip()
    _ensure_symbol(module, clean)
    info = _safe_call(module, "symbol_info", clean)
    value = _obj_get(info, "point")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def get_quote(symbol: str, *, config: MT5Config | None = None, **_: Any) -> dict[str, Any]:
    """Fetch a top-of-book tick snapshot for ``symbol``."""
    cfg = config or load_config()
    module = _connect(cfg)
    clean = symbol.strip()
    _ensure_symbol(module, clean)
    tick = _safe_call(module, "symbol_info_tick", clean)
    if tick is None:
        raise MT5ConnectionError(f"symbol_info_tick({clean}) failed: {_last_error(module)}")
    return {
        "status": "ok",
        "symbol": clean,
        "quote": {
            "bid": _obj_get(tick, "bid"),
            "ask": _obj_get(tick, "ask"),
            "last": _obj_get(tick, "last"),
            "volume": _obj_get(tick, "volume"),
            "time": _epoch_to_iso(_obj_get(tick, "time")),
        },
    }


def get_historical_bars(
    symbol: str,
    *,
    config: MT5Config | None = None,
    period: str = "1d",
    limit: int = 90,
    **_: Any,
) -> dict[str, Any]:
    """Fetch historical bars for ``symbol`` (``period`` is a canonical token)."""
    cfg = config or load_config()
    module = _connect(cfg)
    clean = symbol.strip()
    _ensure_symbol(module, clean)
    timeframe = _timeframe(module, period)
    rates = _safe_call(module, "copy_rates_from_pos", clean, timeframe, 0, int(limit))
    if rates is None:
        raise MT5ConnectionError(f"copy_rates_from_pos({clean}) failed: {_last_error(module)}")
    return {
        "status": "ok",
        "symbol": clean,
        "period": period,
        "bars": [_bar_to_dict(row) for row in rates],
    }


def get_historical_bars_range(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    config: MT5Config | None = None,
    period: str = "15m",
    **_: Any,
) -> dict[str, Any]:
    """Fetch historical bars for ``symbol`` between ``start`` and ``end`` (UTC).

    Unlike ``get_historical_bars`` (most-recent-N-bars-from-now), this answers
    "what did price actually do during this specific past window" — used by
    committee_reporter.py's trade-outcome excursion analysis (was a loss a
    clean directional move, or did price move favorably before reversing).
    """
    cfg = config or load_config()
    module = _connect(cfg)
    clean = symbol.strip()
    _ensure_symbol(module, clean)
    timeframe = _timeframe(module, period)
    rates = _safe_call(module, "copy_rates_range", clean, timeframe, start, end)
    if rates is None:
        raise MT5ConnectionError(f"copy_rates_range({clean}) failed: {_last_error(module)}")
    return {
        "status": "ok",
        "symbol": clean,
        "period": period,
        "bars": [_bar_to_dict(row) for row in rates],
    }


def place_order(
    config: MT5Config | None = None,
    *,
    symbol: str,
    side: str,
    quantity: float | None = None,
    notional: float | None = None,
    order_type: str = "market",
    limit_price: float | None = None,
    time_in_force: str = "day",
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> dict[str, Any]:
    """Submit an order to the signed-in terminal account.

    ``quantity`` is lot volume; ``notional`` is converted to lots using the
    symbol's contract size and current price. ``stop_loss``/``take_profit``
    are optional absolute price levels attached to the same order (MT5's
    native ``sl``/``tp`` fields) — a buy's stop must be below and its
    take-profit above the entry price (sell is the reverse); a mismatched
    level is rejected here rather than left for MT5 to reject less legibly.
    When both are given, the take-profit distance must also be at least the
    stop-loss distance (reward:risk >= 1:1) — MT5 will happily fill an order
    whose target is closer than its stop, so that floor is enforced here
    rather than left to the calling agent's judgment.
    Fails closed (returns an error envelope) on any invalid input or a
    non-``DONE``/``PLACED`` MT5 retcode — never raises for caller-controlled
    mistakes.
    """
    cfg = config or load_config()

    clean_symbol = str(symbol or "").strip()
    if not clean_symbol:
        return {"status": "error", "error": "symbol is required"}

    side_token = str(side or "").strip().lower()
    if side_token not in ("buy", "sell"):
        return {"status": "error", "error": "side must be 'buy' or 'sell'"}

    type_token = str(order_type or "").strip().lower()
    if type_token not in ("market", "limit"):
        return {"status": "error", "error": "order_type must be 'market' or 'limit'"}

    tif_token = str(time_in_force or "").strip().lower()
    if tif_token not in ("day", "gtc"):
        return {"status": "error", "error": "time_in_force must be 'day' or 'gtc'"}

    has_qty = quantity is not None
    has_notional = notional is not None
    if has_qty == has_notional:
        return {"status": "error", "error": "provide exactly one of quantity or notional"}

    if type_token == "limit" and limit_price is None:
        return {"status": "error", "error": "limit order requires limit_price"}
    try:
        limit_value = float(limit_price) if limit_price is not None else None
    except (TypeError, ValueError):
        return {"status": "error", "error": "limit_price must be numeric"}
    if limit_value is not None and limit_value <= 0:
        return {"status": "error", "error": "limit_price must be positive"}

    try:
        sl_value = float(stop_loss) if stop_loss is not None else None
        tp_value = float(take_profit) if take_profit is not None else None
    except (TypeError, ValueError):
        return {"status": "error", "error": "stop_loss/take_profit must be numeric"}
    if sl_value is not None and sl_value <= 0:
        return {"status": "error", "error": "stop_loss must be positive"}
    if tp_value is not None and tp_value <= 0:
        return {"status": "error", "error": "take_profit must be positive"}

    try:
        module = _connect(cfg)
    except (MT5DependencyError, MT5ConnectionError) as exc:
        return {"status": "error", "error": str(exc)}

    account = _call(module, "account_info")
    try:
        _assert_profile(cfg, account, module)
    except MT5ProfileMismatchError as exc:
        return {"status": "error", "error": str(exc)}

    if not _ensure_symbol(module, clean_symbol):
        return {"status": "error", "error": f"symbol '{clean_symbol}' is not available on this account"}

    info = _safe_call(module, "symbol_info", clean_symbol)
    tick = _safe_call(module, "symbol_info_tick", clean_symbol)
    if info is None or tick is None:
        return {"status": "error", "error": f"could not read symbol info for '{clean_symbol}'"}

    price = _obj_get(tick, "ask") if side_token == "buy" else _obj_get(tick, "bid")
    if type_token == "limit":
        price = limit_value

    if price:
        is_buy = side_token == "buy"
        if sl_value is not None and ((is_buy and sl_value >= price) or (not is_buy and sl_value <= price)):
            return {
                "status": "error",
                "error": f"stop_loss {sl_value} must be {'below' if is_buy else 'above'} the entry price ~{price}",
            }
        if tp_value is not None and ((is_buy and tp_value <= price) or (not is_buy and tp_value >= price)):
            return {
                "status": "error",
                "error": f"take_profit {tp_value} must be {'above' if is_buy else 'below'} the entry price ~{price}",
            }
        if sl_value is not None and tp_value is not None:
            risk = abs(price - sl_value)
            reward = abs(tp_value - price)
            # 1e-9 relative tolerance: float rounding must not reject an
            # intended-exact 1:1 (e.g. 0.005000000000000116 vs 0.004999999999999893).
            if risk > 0 and reward < risk * (1 - 1e-9):
                return {
                    "status": "error",
                    "error": (
                        f"reward:risk {reward / risk:.2f} is below the 1:1 floor — take_profit is "
                        f"{reward:g} away from entry ~{price} but stop_loss is {risk:g} away; widen "
                        f"the target or tighten the stop before resubmitting"
                    ),
                }

    try:
        volume = _resolve_volume(info, quantity, notional, _obj_get(tick, "ask") or _obj_get(tick, "bid") or price)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    request = _build_request(
        module,
        config=cfg,
        symbol=clean_symbol,
        stop_loss=sl_value,
        take_profit=tp_value,
        side=side_token,
        order_type=type_token,
        volume=volume,
        price=price,
        time_in_force=tif_token,
    )

    result = _send_with_filling_fallback(module, request)
    if result is None:
        return {"status": "error", "error": f"order_send failed: {_last_error(module)}"}

    retcode = _obj_get(result, "retcode")
    done_codes = {
        getattr(module, "TRADE_RETCODE_DONE", 10009),
        getattr(module, "TRADE_RETCODE_DONE_PARTIAL", 10010),
        getattr(module, "TRADE_RETCODE_PLACED", 10008),
    }
    if retcode not in done_codes:
        return {
            "status": "error",
            "error": f"MT5 rejected the order (retcode={retcode}): {_obj_get(result, 'comment')}",
            "retcode": retcode,
        }

    return {
        "status": "ok",
        "order_id": str(_obj_get(result, "order") or _obj_get(result, "deal") or ""),
        "symbol": clean_symbol,
        "side": side_token,
        "profile": cfg.profile,
        "is_paper": cfg.is_paper,
        "order_type": type_token,
        "time_in_force": tif_token,
        "quantity": volume,
        "notional": notional,
        "limit_price": limit_value,
        "stop_loss": sl_value,
        "take_profit": tp_value,
        "order_status": str(retcode),
        "filled_qty": _obj_get(result, "volume"),
        "fill_price": _obj_get(result, "price"),
    }


def cancel_order(
    config: MT5Config | None = None,
    order_id: str = "",
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Cancel a resting pending order on the signed-in terminal account.

    MT5 separates filled positions from resting pending orders; this cancels a
    pending order by ticket. A ticket that has already filled into a position
    is not cancellable and returns an error explaining the position must be
    closed with a new opposite-side order instead.
    """
    cfg = config or load_config()

    clean_id = str(order_id or "").strip()
    if not clean_id:
        return {"status": "error", "error": "order_id is required"}
    try:
        ticket = int(clean_id)
    except ValueError:
        return {"status": "error", "error": "order_id must be a numeric MT5 ticket"}

    try:
        module = _connect(cfg)
    except (MT5DependencyError, MT5ConnectionError) as exc:
        return {"status": "error", "error": str(exc)}

    account = _call(module, "account_info")
    try:
        _assert_profile(cfg, account, module)
    except MT5ProfileMismatchError as exc:
        return {"status": "error", "error": str(exc)}

    orders = _safe_call(module, "orders_get", ticket=ticket) or ()
    if not orders:
        return {
            "status": "error",
            "error": (
                f"no resting pending order with ticket {ticket}. If this ticket "
                "already filled into a position, use close_position(ticket) "
                "instead of cancelling it — on a hedging-mode account (common "
                "on MT5), a plain opposite-side order opens a second hedged "
                "position rather than closing this one."
            ),
        }

    request = {
        "action": getattr(module, "TRADE_ACTION_REMOVE", 8),
        "order": ticket,
    }
    result = _order_send(module, request)
    if result is None:
        return {"status": "error", "error": f"order_send failed: {_last_error(module)}"}

    retcode = _obj_get(result, "retcode")
    if retcode != getattr(module, "TRADE_RETCODE_DONE", 10009):
        return {
            "status": "error",
            "error": f"MT5 rejected the cancel (retcode={retcode}): {_obj_get(result, 'comment')}",
            "retcode": retcode,
        }

    return {
        "status": "ok",
        "order_id": clean_id,
        "symbol": symbol,
        "side": None,
        "profile": cfg.profile,
        "is_paper": cfg.is_paper,
        "cancelled": True,
    }


def close_position(
    config: MT5Config | None = None,
    ticket: str | int = "",
    *,
    volume: float | None = None,
) -> dict[str, Any]:
    """Close an open position by ticket with an opposite-side market order.

    A plain opposite-side ``place_order`` is NOT sufficient on a hedging-mode
    account (MT5's ``ACCOUNT_MARGIN_MODE_RETAIL_HEDGING``, common on retail MT5
    brokers): it opens a second, separate hedged position instead of closing
    the existing one. Closing requires the request to carry the target
    position's own ticket in its ``position`` field, which this function does
    — safe on both hedging and netting accounts.

    Args:
        config: Connector config; falls back to the saved config when ``None``.
        ticket: The position's MT5 ticket (as returned by ``get_positions``).
        volume: Optional partial-close volume in lots; defaults to the full
            position volume.

    Returns:
        On success ``{"status": "ok", "order_id", "symbol", "side", "profile",
        "is_paper", "closed_volume", "fill_price"}``. On invalid input or a
        non-``DONE`` MT5 retcode, ``{"status": "error", "error": <message>}`` —
        fails closed, never raises.
    """
    cfg = config or load_config()

    clean_ticket = str(ticket or "").strip()
    if not clean_ticket:
        return {"status": "error", "error": "ticket is required"}
    try:
        ticket_id = int(clean_ticket)
    except ValueError:
        return {"status": "error", "error": "ticket must be a numeric MT5 ticket"}

    try:
        module = _connect(cfg)
    except (MT5DependencyError, MT5ConnectionError) as exc:
        return {"status": "error", "error": str(exc)}

    account = _call(module, "account_info")
    try:
        _assert_profile(cfg, account, module)
    except MT5ProfileMismatchError as exc:
        return {"status": "error", "error": str(exc)}

    positions = _safe_call(module, "positions_get", ticket=ticket_id) or ()
    if not positions:
        return {"status": "error", "error": f"no open position with ticket {ticket_id}"}
    position = positions[0]

    symbol = _obj_get(position, "symbol")
    position_volume = float(_obj_get(position, "volume") or 0.0)
    close_volume = float(volume) if volume is not None else position_volume
    if close_volume <= 0:
        return {"status": "error", "error": "volume must be positive"}
    if close_volume > position_volume:
        return {"status": "error", "error": f"volume {close_volume} exceeds open position volume {position_volume}"}

    is_buy_position = _obj_get(position, "type") == 0
    tick = _safe_call(module, "symbol_info_tick", symbol)
    if tick is None:
        return {"status": "error", "error": f"could not read a current price for '{symbol}'"}
    # Closing a long sells at bid; closing a short buys at ask — the opposite
    # of the side convention used to open the position.
    price = _obj_get(tick, "bid") if is_buy_position else _obj_get(tick, "ask")

    request = {
        "action": getattr(module, "TRADE_ACTION_DEAL", 1),
        "symbol": symbol,
        "volume": close_volume,
        "type": getattr(module, "ORDER_TYPE_SELL" if is_buy_position else "ORDER_TYPE_BUY", 1 if is_buy_position else 0),
        "position": ticket_id,
        "price": price,
        "deviation": cfg.deviation,
        "magic": cfg.magic,
        "comment": "vibe-trading-close",
        "type_time": getattr(module, "ORDER_TIME_DAY", 1),
    }

    result = _send_with_filling_fallback(module, request)
    if result is None:
        return {"status": "error", "error": f"order_send failed: {_last_error(module)}"}

    retcode = _obj_get(result, "retcode")
    done_codes = {
        getattr(module, "TRADE_RETCODE_DONE", 10009),
        getattr(module, "TRADE_RETCODE_DONE_PARTIAL", 10010),
    }
    if retcode not in done_codes:
        return {
            "status": "error",
            "error": f"MT5 rejected the close (retcode={retcode}): {_obj_get(result, 'comment')}",
            "retcode": retcode,
        }

    return {
        "status": "ok",
        "order_id": str(_obj_get(result, "order") or _obj_get(result, "deal") or ""),
        "symbol": symbol,
        "side": "sell" if is_buy_position else "buy",
        "profile": cfg.profile,
        "is_paper": cfg.is_paper,
        "closed_volume": close_volume,
        "fill_price": _obj_get(result, "price"),
    }


def modify_position(
    config: MT5Config | None = None,
    ticket: str | int = "",
    *,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> dict[str, Any]:
    """Set or change the stop-loss/take-profit on an already-open position.

    Uses MT5's ``TRADE_ACTION_SLTP`` — modifies the position's protective
    levels in place, no new order/fill involved. At least one of
    ``stop_loss``/``take_profit`` must be given; a level omitted here (as
    opposed to explicitly set to the position's current one) leaves that
    side unprotected on brokers that clear the field rather than preserving
    it, so callers should pass both if either is meant to survive.

    Returns:
        On success ``{"status": "ok", "symbol", "stop_loss", "take_profit"}``.
        On invalid input or a non-``DONE`` MT5 retcode, ``{"status": "error",
        "error": <message>}`` — fails closed, never raises.
    """
    cfg = config or load_config()

    clean_ticket = str(ticket or "").strip()
    if not clean_ticket:
        return {"status": "error", "error": "ticket is required"}
    try:
        ticket_id = int(clean_ticket)
    except ValueError:
        return {"status": "error", "error": "ticket must be a numeric MT5 ticket"}

    if stop_loss is None and take_profit is None:
        return {"status": "error", "error": "provide at least one of stop_loss or take_profit"}
    try:
        sl_value = float(stop_loss) if stop_loss is not None else None
        tp_value = float(take_profit) if take_profit is not None else None
    except (TypeError, ValueError):
        return {"status": "error", "error": "stop_loss/take_profit must be numeric"}
    if sl_value is not None and sl_value <= 0:
        return {"status": "error", "error": "stop_loss must be positive"}
    if tp_value is not None and tp_value <= 0:
        return {"status": "error", "error": "take_profit must be positive"}

    try:
        module = _connect(cfg)
    except (MT5DependencyError, MT5ConnectionError) as exc:
        return {"status": "error", "error": str(exc)}

    account = _call(module, "account_info")
    try:
        _assert_profile(cfg, account, module)
    except MT5ProfileMismatchError as exc:
        return {"status": "error", "error": str(exc)}

    positions = _safe_call(module, "positions_get", ticket=ticket_id) or ()
    if not positions:
        return {"status": "error", "error": f"no open position with ticket {ticket_id}"}
    position = positions[0]
    symbol = _obj_get(position, "symbol")
    is_buy_position = _obj_get(position, "type") == 0
    entry_price = _obj_get(position, "price_open")

    # Stop-loss is validated against the CURRENT price, not entry -- unlike
    # place_order (where entry and "current" are the same instant), a stop
    # on an already-open position is legitimately meant to move past entry
    # as a trade earns profit (a breakeven or trailing stop IS a stop past
    # entry by design). Pinning this to entry_price the same way place_order
    # does rejected every such modify with "must be below/above entry" even
    # when the stop was perfectly valid relative to where price actually is
    # now -- confirmed live 2026-09-03, see committee_reporter.py's
    # BREAKEVEN_BUFFER_POINTS comment. MT5 closes a buy at bid and a sell at
    # ask, so that's the side compared here, matching how the broker itself
    # evaluates stops-level distance on a modify.
    tick = _safe_call(module, "symbol_info_tick", symbol)
    current_price = _obj_get(tick, "bid") if is_buy_position else _obj_get(tick, "ask")

    if current_price:
        if sl_value is not None and ((is_buy_position and sl_value >= current_price) or (not is_buy_position and sl_value <= current_price)):
            return {
                "status": "error",
                "error": f"stop_loss {sl_value} must be {'below' if is_buy_position else 'above'} the current price ~{current_price}",
            }
    if entry_price:
        if tp_value is not None and ((is_buy_position and tp_value <= entry_price) or (not is_buy_position and tp_value >= entry_price)):
            return {
                "status": "error",
                "error": f"take_profit {tp_value} must be {'above' if is_buy_position else 'below'} entry {entry_price}",
            }

    request: dict[str, Any] = {
        "action": getattr(module, "TRADE_ACTION_SLTP", 6),
        "symbol": symbol,
        "position": ticket_id,
        "sl": sl_value if sl_value is not None else (_obj_get(position, "sl") or 0.0),
        "tp": tp_value if tp_value is not None else (_obj_get(position, "tp") or 0.0),
    }

    result = _order_send(module, request)
    if result is None:
        return {"status": "error", "error": f"order_send failed: {_last_error(module)}"}

    retcode = _obj_get(result, "retcode")
    if retcode != getattr(module, "TRADE_RETCODE_DONE", 10009):
        return {
            "status": "error",
            "error": f"MT5 rejected the modification (retcode={retcode}): {_obj_get(result, 'comment')}",
            "retcode": retcode,
        }

    return {
        "status": "ok",
        "symbol": symbol,
        "stop_loss": request["sl"] or None,
        "take_profit": request["tp"] or None,
    }


# --------------------------------------------------------------------------- #
# Order sizing / request building
# --------------------------------------------------------------------------- #


def _resolve_volume(info: Any, quantity: float | None, notional: float | None, price: float | None) -> float:
    """Resolve an order's lot volume from ``quantity`` or ``notional``, fail-closed."""
    volume_min = float(_obj_get(info, "volume_min") or 0.01)
    volume_max = float(_obj_get(info, "volume_max") or 0.0) or None
    volume_step = float(_obj_get(info, "volume_step") or 0.01)

    if quantity is not None:
        try:
            volume = float(quantity)
        except (TypeError, ValueError):
            raise ValueError("quantity must be numeric")
        if volume <= 0:
            raise ValueError("quantity must be positive")
    else:
        try:
            notional_value = float(notional)
        except (TypeError, ValueError):
            raise ValueError("notional must be numeric")
        if notional_value <= 0:
            raise ValueError("notional must be positive")
        contract_size = float(_obj_get(info, "trade_contract_size") or 1.0)
        if not price or price <= 0:
            raise ValueError("could not resolve a current price to size the order by notional")
        volume = notional_value / (price * contract_size)

    if volume_step > 0:
        volume = round(volume / volume_step) * volume_step
    volume = max(volume, volume_min)
    if volume_max is not None:
        volume = min(volume, volume_max)
    return round(volume, 8)


def _build_request(
    module: ModuleType,
    *,
    config: MT5Config,
    symbol: str,
    side: str,
    order_type: str,
    volume: float,
    price: float | None,
    time_in_force: str,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> dict[str, Any]:
    """Build an MT5 ``order_send`` request dict for a market or limit order."""
    is_buy = side == "buy"
    if order_type == "market":
        action = getattr(module, "TRADE_ACTION_DEAL", 1)
        mt5_type = getattr(module, "ORDER_TYPE_BUY" if is_buy else "ORDER_TYPE_SELL", 0 if is_buy else 1)
    else:
        action = getattr(module, "TRADE_ACTION_PENDING", 5)
        mt5_type = getattr(module, "ORDER_TYPE_BUY_LIMIT" if is_buy else "ORDER_TYPE_SELL_LIMIT", 2 if is_buy else 3)

    request: dict[str, Any] = {
        "action": action,
        "symbol": symbol,
        "volume": volume,
        "type": mt5_type,
        "price": price,
        "magic": config.magic,
        "comment": "vibe-trading",
        "type_time": getattr(module, "ORDER_TIME_GTC" if time_in_force == "gtc" else "ORDER_TIME_DAY", 0),
    }
    if order_type == "market":
        request["deviation"] = config.deviation
    if stop_loss is not None:
        request["sl"] = stop_loss
    if take_profit is not None:
        request["tp"] = take_profit
    return request


def _send_with_filling_fallback(module: ModuleType, request: dict[str, Any]) -> Any:
    """Try ``order_send`` across MT5's filling modes; brokers vary in which they accept."""
    filling_modes = [
        getattr(module, "ORDER_FILLING_IOC", 1),
        getattr(module, "ORDER_FILLING_FOK", 0),
        getattr(module, "ORDER_FILLING_RETURN", 2),
    ]
    invalid_fill = getattr(module, "TRADE_RETCODE_INVALID_FILL", 10030)
    last_result = None
    for filling in filling_modes:
        attempt = dict(request, type_filling=filling)
        result = _order_send(module, attempt)
        if result is None:
            continue
        last_result = result
        if _obj_get(result, "retcode") != invalid_fill:
            return result
    return last_result


def _order_send(module: ModuleType, request: dict[str, Any]) -> Any:
    """Call ``order_send`` directly with a named parameter, never ``*args``-forwarded.

    The ``MetaTrader5`` C extension mis-parses ``order_send`` when it is invoked
    through a generic ``fn(*args, **kwargs)`` forwarding wrapper (as every other
    helper in this module does via ``_safe_call``) — it silently returns
    ``None`` instead of an ``OrderSendResult``, even though the exact same
    request dict succeeds when passed with a named parameter. This helper
    exists solely to give ``request`` a real parameter name at the call site.
    """
    try:
        return module.order_send(request)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# SDK plumbing
# --------------------------------------------------------------------------- #


def _require_mt5() -> ModuleType:
    try:
        import MetaTrader5  # type: ignore
    except ModuleNotFoundError as exc:
        raise MT5DependencyError(
            "MetaTrader5 is not installed; run `pip install MetaTrader5` "
            "(Windows only, requires a locally installed MT5 terminal)."
        ) from exc
    return MetaTrader5


def _connect(config: MT5Config) -> ModuleType:
    module = _require_mt5()
    kwargs: dict[str, Any] = {}
    if config.terminal_path:
        kwargs["path"] = config.terminal_path
    if config.login:
        kwargs["login"] = config.login
    if config.server:
        kwargs["server"] = config.server
    if config.timeout:
        kwargs["timeout"] = int(config.timeout * 1000)

    ok = module.initialize(**kwargs) if kwargs else module.initialize()
    if not ok:
        raise MT5ConnectionError(
            f"Could not attach to a running MetaTrader 5 terminal ({_last_error(module)}). "
            "Open MetaTrader 5, sign into an account, and retry."
        )
    return module


def _assert_profile(config: MT5Config, account: Any, module: ModuleType) -> None:
    if account is None:
        raise MT5ConnectionError(f"account_info() failed: {_last_error(module)}")
    trade_mode = _obj_get(account, "trade_mode")
    demo_mode = getattr(module, "ACCOUNT_TRADE_MODE_DEMO", _TRADE_MODE_DEMO)
    if config.profile == "paper" and trade_mode != demo_mode:
        raise MT5ProfileMismatchError(
            "Configured profile is the MT5 demo profile, but the terminal is signed "
            "into a non-demo account. Sign into a demo account in MetaTrader 5, or "
            "select the mt5-live-readonly profile for intentional read-only live access."
        )
    if config.profile == "live-trade" and trade_mode == demo_mode:
        raise MT5ProfileMismatchError(
            "Configured profile is the MT5 live-trade profile, but the terminal is "
            "signed into a demo account. Sign into your live account, or select the "
            "mt5-demo-trade profile for demo trading."
        )


def _last_error(module: ModuleType) -> str:
    try:
        code, desc = module.last_error()
        return f"{code}: {desc}"
    except Exception:
        return "unknown error"


def _ensure_symbol(module: ModuleType, symbol: str) -> bool:
    """Make sure ``symbol`` is visible in Market Watch so ticks/rates resolve."""
    try:
        info = module.symbol_info(symbol)
        if info is not None and _obj_get(info, "visible"):
            return True
        return bool(module.symbol_select(symbol, True))
    except Exception:
        return False


def _timeframe(module: ModuleType, period: str) -> Any:
    """Map a canonical period token to an MT5 ``TIMEFRAME_*`` constant."""
    token = period.strip() if period in _PERIOD_TIMEFRAMES else "1d"
    names = {
        "1m": "TIMEFRAME_M1", "5m": "TIMEFRAME_M5", "15m": "TIMEFRAME_M15", "30m": "TIMEFRAME_M30",
        "1h": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4", "1d": "TIMEFRAME_D1", "1w": "TIMEFRAME_W1", "1M": "TIMEFRAME_MN1",
    }
    return getattr(module, names[token], getattr(module, "TIMEFRAME_D1", 16408))


def _recent_deals(module: ModuleType, days: int = 7) -> list[Any]:
    try:
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        deals = module.history_deals_get(now - timedelta(days=days), now)
        return list(deals) if deals else []
    except Exception:
        return []


def _call(obj: Any, name: str) -> Any:
    fn = getattr(obj, name, None)
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


def _safe_call(obj: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    fn = getattr(obj, name, None)
    if fn is None:
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _obj_get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _epoch_to_iso(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


# --------------------------------------------------------------------------- #
# Row conversion
# --------------------------------------------------------------------------- #


def _position_to_dict(item: Any) -> dict[str, Any]:
    side = "buy" if _obj_get(item, "type") == 0 else "sell"
    return {
        "ticket": _obj_get(item, "ticket"),
        "symbol": _obj_get(item, "symbol"),
        "side": side,
        "volume": _obj_get(item, "volume"),
        "price_open": _obj_get(item, "price_open"),
        "price_current": _obj_get(item, "price_current"),
        "stop_loss": _obj_get(item, "sl") or None,
        "take_profit": _obj_get(item, "tp") or None,
        "swap": _obj_get(item, "swap"),
        "profit": _obj_get(item, "profit"),
        "magic": _obj_get(item, "magic"),
        "comment": _obj_get(item, "comment"),
        "time": _epoch_to_iso(_obj_get(item, "time")),
    }


_ORDER_TYPE_NAMES = {
    0: "buy", 1: "sell", 2: "buy_limit", 3: "sell_limit", 4: "buy_stop", 5: "sell_stop",
}


def _order_to_dict(item: Any) -> dict[str, Any]:
    return {
        "order_id": str(_obj_get(item, "ticket") or ""),
        "symbol": _obj_get(item, "symbol"),
        "side": _ORDER_TYPE_NAMES.get(_obj_get(item, "type"), str(_obj_get(item, "type"))),
        "order_type": "limit" if _obj_get(item, "type") in (2, 3) else "stop" if _obj_get(item, "type") in (4, 5) else "market",
        "quantity": _obj_get(item, "volume_current"),
        "limit_price": _obj_get(item, "price_open"),
        "status": str(_obj_get(item, "state")),
        "magic": _obj_get(item, "magic"),
        "submitted_at": _epoch_to_iso(_obj_get(item, "time_setup")),
    }


def _deal_to_dict(item: Any) -> dict[str, Any]:
    return {
        "ticket": _obj_get(item, "ticket"),
        "order_id": str(_obj_get(item, "order") or ""),
        "symbol": _obj_get(item, "symbol"),
        "side": "buy" if _obj_get(item, "type") == 0 else "sell",
        "volume": _obj_get(item, "volume"),
        "price": _obj_get(item, "price"),
        "profit": _obj_get(item, "profit"),
        "time": _epoch_to_iso(_obj_get(item, "time")),
        # magic/comment let a caller tell its own fills apart from another EA's
        # or manual trades on the same account (see committee_reporter.py's
        # signal-service context feature).
        "magic": _obj_get(item, "magic"),
        "comment": _obj_get(item, "comment"),
        # The CLOSING deal of a position carries `order == 0` in practice (no
        # usable back-reference to the opening order via order_id) — the only
        # reliable link between a position's opening and closing deals is
        # `position_id`, which equals the opening order/position ticket for
        # both. `entry` (MT5's DEAL_ENTRY_* enum: 0=in/opening, 1=out/closing,
        # 2=in-out, 3=out-by) is what a caller needs to tell which one a given
        # deal actually is — an opening deal always reports profit=0.0, so
        # matching on order_id alone (as committee_reporter.py's trade journal
        # originally did) silently grabs the wrong deal and reports a real win
        # as "breakeven".
        "position_id": _obj_get(item, "position_id"),
        "entry": _obj_get(item, "entry"),
    }


def _bar_to_dict(row: Any) -> dict[str, Any]:
    get = row.__getitem__ if hasattr(row, "__getitem__") else lambda key: getattr(row, key, None)
    try:
        open_, high, low, close = get("open"), get("high"), get("low"), get("close")
        volume = get("tick_volume")
        time_value = get("time")
    except (KeyError, IndexError, ValueError):
        return {}
    return {
        "time": _epoch_to_iso(time_value),
        "open": float(open_) if open_ is not None else None,
        "high": float(high) if high is not None else None,
        "low": float(low) if low is not None else None,
        "close": float(close) if close is not None else None,
        "volume": int(volume) if volume is not None else None,
    }
