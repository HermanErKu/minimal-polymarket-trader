# test4.py
#
# Installer:
#   pip install requests websockets python-dotenv
#
# Kjør:
#   python test4.py "btc-updown-5m-1773752700"
#
# Hva denne versjonen gjør:
# - Henter event via slug
# - Leser ut Up/Down token IDs
# - Kobler til Polymarket market websocket
# - Leser live best bid/ask
# - Vurderer signal hvert N sekund
# - Åpner bare handel hvis signalet er bekreftet over tid
# - Hopper over handler ved dårlig spread / gammel data / for sent i markedet
# - Kan lukke papirhandel tidlig ved bekreftet reversering
# - Logger både early exits og resolved trades til CSV
#
# Viktig:
# - Live trading støtter fortsatt bare BUY market order i denne filen
# - Early exit gjøres derfor kun i paper-trading logikken
# - Hvis POLY_LIVE_TRADING=1, så vil scriptet fortsatt kunne åpne live buy,
#   men early exit er kun simulert med mindre du bygger sell/close-støtte

import asyncio
import ast
import csv
import importlib
import json
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
import websockets
from dotenv import load_dotenv

GAMMA_EVENT_BY_SLUG = "https://gamma-api.polymarket.com/events/slug/{slug}"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# =========================
# Tunables / safeguards
# =========================
DECISION_EVERY_SECONDS = 10
TRADE_SIZE_USD = 1.5

EDGE_THRESHOLD_ENTER = 0.06
EDGE_THRESHOLD_EXIT = 0.08
EDGE_THRESHOLD_HOLD = 0.02

CONFIRMATIONS_REQUIRED = 3
REVERSAL_CONFIRMATIONS = 2
HISTORY_WINDOW_SECONDS = 60

NO_TRADE_LAST_SECONDS = 45
MIN_SECONDS_BEFORE_ENTRY = 30
COOLDOWN_AFTER_EXIT_SECONDS = 20

MAX_SPREAD = 0.04
MAX_DATA_AGE_SECONDS = 5

MIN_AVG_EDGE = 0.03
MIN_ENTRY_PRICE = 0.05
MAX_ENTRY_PRICE = 0.95

CSV_PATH = "paper_trades.csv"

load_dotenv()


class LiveTrader:
    def __init__(self):
        try:
            clob_client_module = importlib.import_module("py_clob_client.client")
            clob_types_module = importlib.import_module("py_clob_client.clob_types")
            clob_constants_module = importlib.import_module("py_clob_client.order_builder.constants")
            ClobClient = getattr(clob_client_module, "ClobClient")
            MarketOrderArgs = getattr(clob_types_module, "MarketOrderArgs")
            OrderType = getattr(clob_types_module, "OrderType")
            BUY = getattr(clob_constants_module, "BUY")
        except Exception as e:
            raise RuntimeError(
                "Mangler py-clob-client. Installer med: pip install py-clob-client"
            ) from e

        private_key = (os.getenv("POLY_PRIVATE_KEY") or "").strip()
        if not private_key:
            raise RuntimeError("POLY_PRIVATE_KEY mangler.")

        host = (os.getenv("POLY_CLOB_HOST") or "https://clob.polymarket.com").strip()
        chain_id = int((os.getenv("POLY_CHAIN_ID") or "137").strip())
        signature_type = int((os.getenv("POLY_SIGNATURE_TYPE") or "1").strip())
        funder = (os.getenv("POLY_FUNDER") or "").strip() or None

        if signature_type in {1, 2} and not funder:
            raise RuntimeError("POLY_FUNDER mangler for signature type 1/2.")

        self.OrderType = OrderType
        self.MarketOrderArgs = MarketOrderArgs
        self.BUY = BUY
        self.client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder,
        )
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def place_buy_market_order(self, token_id: str, amount_usd: float) -> dict:
        try:
            mo = self.MarketOrderArgs(
                token_id=str(token_id),
                amount=float(amount_usd),
                side=self.BUY,
                order_type=self.OrderType.FOK,
            )
            signed = self.client.create_market_order(mo)
            resp = self.client.post_order(signed, self.OrderType.FOK)
            return {"ok": True, "response": resp, "unknown": False}
        except Exception as e:
            msg = str(e)
            is_unknown = "Request exception!" in msg and "status_code=None" in msg
            return {
                "ok": False,
                "error": msg,
                "unknown": is_unknown,
            }


@dataclass
class BookState:
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    spread: float | None = None
    last_update_ts: float | None = None

    def update(self, bid, ask):
        try:
            bid = float(bid) if bid is not None else None
            ask = float(ask) if ask is not None else None
        except (TypeError, ValueError):
            return

        self.bid = bid
        self.ask = ask

        if bid is not None and ask is not None:
            self.mid = (bid + ask) / 2.0
            self.spread = ask - bid
        elif bid is not None:
            self.mid = bid
            self.spread = None
        elif ask is not None:
            self.mid = ask
            self.spread = None
        else:
            self.mid = None
            self.spread = None

        self.last_update_ts = time.time()


@dataclass
class Position:
    slug: str
    title: str
    opened_ts: float
    side: str              # "UP" or "DOWN"
    token_id: str
    entry_price: float
    stake_usd: float
    shares: float
    market_end_iso: str


class PaperSignalEngine:
    def __init__(
        self,
        slug: str,
        title: str,
        up_token_id: str,
        down_token_id: str,
        market_end_iso: str,
        live_trader: LiveTrader | None = None,
    ):
        self.slug = slug
        self.title = title
        self.up_token_id = str(up_token_id)
        self.down_token_id = str(down_token_id)
        self.market_end_iso = market_end_iso
        self.live_trader = live_trader

        self.up = BookState()
        self.down = BookState()

        self.position: Position | None = None
        self.closed_rows: list[dict] = []
        self.exit_history: list[dict] = []

        self.signal_history = deque()  # (ts, edge)
        self.last_exit_ts: float | None = None
        self.market_start_ts = time.time()

    def on_best_bid_ask(self, asset_id: str, bid, ask):
        asset_id = str(asset_id)
        if asset_id == self.up_token_id:
            self.up.update(bid, ask)
        elif asset_id == self.down_token_id:
            self.down.update(bid, ask)

    def seconds_to_market_end(self) -> float | None:
        try:
            dt = datetime.fromisoformat(self.market_end_iso.replace("Z", "+00:00"))
            return dt.timestamp() - time.time()
        except Exception:
            return None

    def is_data_fresh(self) -> bool:
        now = time.time()
        if self.up.last_update_ts is None or self.down.last_update_ts is None:
            return False
        return (
            (now - self.up.last_update_ts) <= MAX_DATA_AGE_SECONDS
            and (now - self.down.last_update_ts) <= MAX_DATA_AGE_SECONDS
        )

    def spreads_ok(self) -> bool:
        if self.up.spread is None or self.down.spread is None:
            return False
        return self.up.spread <= MAX_SPREAD and self.down.spread <= MAX_SPREAD

    def in_cooldown(self) -> bool:
        if self.last_exit_ts is None:
            return False
        return (time.time() - self.last_exit_ts) < COOLDOWN_AFTER_EXIT_SECONDS

    def update_signal_history(self, edge: float):
        now = time.time()
        self.signal_history.append((now, edge))

        while self.signal_history and (now - self.signal_history[0][0]) > HISTORY_WINDOW_SECONDS:
            self.signal_history.popleft()

    def history_stats(self) -> dict:
        if not self.signal_history:
            return {
                "avg_edge": None,
                "recent_signals": [],
                "up_votes": 0,
                "down_votes": 0,
                "hold_votes": 0,
            }

        edges = [edge for _, edge in self.signal_history]
        avg_edge = sum(edges) / len(edges)

        recent_signals = []
        for _, edge in self.signal_history:
            if edge > EDGE_THRESHOLD_HOLD:
                recent_signals.append("UP")
            elif edge < -EDGE_THRESHOLD_HOLD:
                recent_signals.append("DOWN")
            else:
                recent_signals.append("HOLD")

        up_votes = sum(1 for s in recent_signals if s == "UP")
        down_votes = sum(1 for s in recent_signals if s == "DOWN")
        hold_votes = sum(1 for s in recent_signals if s == "HOLD")

        return {
            "avg_edge": avg_edge,
            "recent_signals": recent_signals,
            "up_votes": up_votes,
            "down_votes": down_votes,
            "hold_votes": hold_votes,
        }

    def confirmed_signal(self, signal: str, n: int) -> bool:
        stats = self.history_stats()
        recent = stats["recent_signals"][-n:]
        return len(recent) == n and all(s == signal for s in recent)

    def decide(self) -> dict:
        if self.up.mid is None or self.down.mid is None:
            return {
                "signal": "HOLD",
                "reason": "Mangler nok live-data",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": None,
            }

        if not self.is_data_fresh():
            return {
                "signal": "HOLD",
                "reason": "Data er for gammel",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": None,
            }

        if not self.spreads_ok():
            return {
                "signal": "HOLD",
                "reason": "Spread er for høy",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": None,
                "up_spread": self.up.spread,
                "down_spread": self.down.spread,
            }

        secs_left = self.seconds_to_market_end()
        edge = self.up.mid - self.down.mid

        if secs_left is not None and secs_left <= NO_TRADE_LAST_SECONDS and self.position is None:
            return {
                "signal": "HOLD",
                "reason": f"Ingen nye handler siste {NO_TRADE_LAST_SECONDS}s",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": edge,
            }

        market_age = time.time() - self.market_start_ts
        if market_age < MIN_SECONDS_BEFORE_ENTRY and self.position is None:
            return {
                "signal": "HOLD",
                "reason": f"Venter første {MIN_SECONDS_BEFORE_ENTRY}s for å unngå tidlig fake move",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": edge,
            }

        if self.in_cooldown() and self.position is None:
            return {
                "signal": "HOLD",
                "reason": "Cooldown etter early exit",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": edge,
            }

        self.update_signal_history(edge)
        stats = self.history_stats()
        avg_edge = stats["avg_edge"]

        if avg_edge is None:
            return {
                "signal": "HOLD",
                "reason": "Bygger historikk",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": edge,
            }

        # Når posisjon er åpen, bruk sterkere terskel for reversering
        if self.position is not None:
            if (
                self.position.side == "UP"
                and edge < -EDGE_THRESHOLD_EXIT
                and avg_edge < -MIN_AVG_EDGE
                and self.confirmed_signal("DOWN", REVERSAL_CONFIRMATIONS)
            ):
                return {
                    "signal": "DOWN",
                    "reason": f"Bekreftet reversering mot DOWN: edge={edge:.3f}, avg_edge={avg_edge:.3f}",
                    "up_mid": self.up.mid,
                    "down_mid": self.down.mid,
                    "edge": edge,
                    "avg_edge": avg_edge,
                }

            if (
                self.position.side == "DOWN"
                and edge > EDGE_THRESHOLD_EXIT
                and avg_edge > MIN_AVG_EDGE
                and self.confirmed_signal("UP", REVERSAL_CONFIRMATIONS)
            ):
                return {
                    "signal": "UP",
                    "reason": f"Bekreftet reversering mot UP: edge={edge:.3f}, avg_edge={avg_edge:.3f}",
                    "up_mid": self.up.mid,
                    "down_mid": self.down.mid,
                    "edge": edge,
                    "avg_edge": avg_edge,
                }

            return {
                "signal": "HOLD",
                "reason": f"Holder posisjon. edge={edge:.3f}, avg={avg_edge:.3f}",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": edge,
                "avg_edge": avg_edge,
            }

        # Ingen posisjon åpen -> vanlig entry-logikk
        if (
            edge > EDGE_THRESHOLD_ENTER
            and avg_edge > MIN_AVG_EDGE
            and self.confirmed_signal("UP", CONFIRMATIONS_REQUIRED)
        ):
            return {
                "signal": "UP",
                "reason": f"Bekreftet UP: edge={edge:.3f}, avg_edge={avg_edge:.3f}",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": edge,
                "avg_edge": avg_edge,
            }

        if (
            edge < -EDGE_THRESHOLD_ENTER
            and avg_edge < -MIN_AVG_EDGE
            and self.confirmed_signal("DOWN", CONFIRMATIONS_REQUIRED)
        ):
            return {
                "signal": "DOWN",
                "reason": f"Bekreftet DOWN: edge={edge:.3f}, avg_edge={avg_edge:.3f}",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": edge,
                "avg_edge": avg_edge,
            }

        return {
            "signal": "HOLD",
            "reason": f"Ingen bekreftet fordel ennå (edge={edge:.3f}, avg={avg_edge:.3f})",
            "up_mid": self.up.mid,
            "down_mid": self.down.mid,
            "edge": edge,
            "avg_edge": avg_edge,
        }

    def maybe_open_position(self, decision: dict):
        if self.position is not None:
            return None

        signal = decision.get("signal")
        if signal not in {"UP", "DOWN"}:
            return None

        if signal == "UP":
            entry_price = self.up.ask
            token_id = self.up_token_id
        else:
            entry_price = self.down.ask
            token_id = self.down_token_id

        if entry_price is None or entry_price <= 0:
            return None

        if entry_price < MIN_ENTRY_PRICE or entry_price > MAX_ENTRY_PRICE:
            return {
                "action": "SKIP_OPEN",
                "slug": self.slug,
                "title": self.title,
                "ts": datetime.now(timezone.utc).isoformat(),
                "side": signal,
                "entry_price": round(entry_price, 6),
                "reason": f"Entry price {entry_price:.3f} er utenfor tillatt område [{MIN_ENTRY_PRICE}, {MAX_ENTRY_PRICE}]",
            }

        live_order = None
        if self.live_trader is not None:
            live_order = self.live_trader.place_buy_market_order(
                token_id=token_id,
                amount_usd=TRADE_SIZE_USD,
            )

            if not live_order.get("ok"):
                if live_order.get("unknown"):
                    return {
                        "action": "OPEN_UNKNOWN",
                        "slug": self.slug,
                        "title": self.title,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "side": signal,
                        "stake_usd": TRADE_SIZE_USD,
                        "reason": "Ordrestatus ukjent etter request exception. Ikke retry automatisk.",
                        "token_id": token_id,
                        "entry_price": round(entry_price, 6),
                        "live_order": live_order,
                    }

                return {
                    "action": "OPEN_FAILED",
                    "slug": self.slug,
                    "title": self.title,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "side": signal,
                    "stake_usd": TRADE_SIZE_USD,
                    "reason": decision.get("reason"),
                    "token_id": token_id,
                    "entry_price": round(entry_price, 6),
                    "live_order": live_order,
                }

        shares = TRADE_SIZE_USD / entry_price
        self.position = Position(
            slug=self.slug,
            title=self.title,
            opened_ts=time.time(),
            side=signal,
            token_id=token_id,
            entry_price=entry_price,
            stake_usd=TRADE_SIZE_USD,
            shares=shares,
            market_end_iso=self.market_end_iso,
        )

        return {
            "action": "OPEN",
            "slug": self.slug,
            "title": self.title,
            "ts": datetime.now(timezone.utc).isoformat(),
            "side": signal,
            "token_id": token_id,
            "entry_price": round(entry_price, 6),
            "stake_usd": TRADE_SIZE_USD,
            "shares": round(shares, 6),
            "reason": decision.get("reason"),
            "live_order": live_order,
        }

    def maybe_close_position_early(self, decision: dict):
        if self.position is None:
            return None

        # Early exit kun som paper-logikk.
        # Hvis du senere bygger sell/close live, kan dette utvides.
        signal = decision.get("signal")
        if signal not in {"UP", "DOWN"}:
            return None

        pos = self.position
        opposite = "DOWN" if pos.side == "UP" else "UP"

        if signal != opposite:
            return None

        if pos.side == "UP":
            exit_price = self.up.bid
        else:
            exit_price = self.down.bid

        if exit_price is None or exit_price <= 0:
            return None

        payout = pos.shares * exit_price
        pnl = payout - pos.stake_usd

        row = {
            "slug": pos.slug,
            "title": pos.title,
            "opened_at_utc": datetime.fromtimestamp(pos.opened_ts, tz=timezone.utc).isoformat(),
            "closed_at_utc": datetime.now(timezone.utc).isoformat(),
            "market_end_iso": pos.market_end_iso,
            "side": pos.side,
            "token_id": pos.token_id,
            "entry_price": round(pos.entry_price, 6),
            "exit_price": round(exit_price, 6),
            "stake_usd": round(pos.stake_usd, 2),
            "shares": round(pos.shares, 6),
            "exit_type": "early_exit",
            "result_side": f"EARLY_EXIT_{opposite}",
            "won": "",
            "payout_usd": round(payout, 2),
            "pnl_usd": round(pnl, 2),
        }

        self.exit_history.append(row)
        self.closed_rows.append(row)
        append_trade_csv(row, CSV_PATH)

        self.position = None
        self.last_exit_ts = time.time()

        return {
            "action": "EARLY_EXIT",
            "slug": self.slug,
            "title": self.title,
            "ts": datetime.now(timezone.utc).isoformat(),
            "side_opened": pos.side,
            "exit_reason": decision.get("reason"),
            "exit_price": round(exit_price, 6),
            "pnl_usd": round(pnl, 2),
        }

    def settle(self, winning_asset_id: str, winning_outcome: str):
        if self.position is None:
            return None

        pos = self.position
        won = str(winning_asset_id) == str(pos.token_id)
        payout = pos.shares * 1.0 if won else 0.0
        pnl = payout - pos.stake_usd

        result_side = "UP" if str(winning_outcome).strip().lower() in {"yes", "up"} else "DOWN"

        row = {
            "slug": pos.slug,
            "title": pos.title,
            "opened_at_utc": datetime.fromtimestamp(pos.opened_ts, tz=timezone.utc).isoformat(),
            "closed_at_utc": datetime.now(timezone.utc).isoformat(),
            "market_end_iso": pos.market_end_iso,
            "side": pos.side,
            "token_id": pos.token_id,
            "entry_price": round(pos.entry_price, 6),
            "exit_price": 1.0 if won else 0.0,
            "stake_usd": round(pos.stake_usd, 2),
            "shares": round(pos.shares, 6),
            "exit_type": "resolved",
            "result_side": result_side,
            "won": won,
            "payout_usd": round(payout, 2),
            "pnl_usd": round(pnl, 2),
        }

        self.closed_rows.append(row)
        self.position = None
        append_trade_csv(row, CSV_PATH)
        return row


def ensure_csv(path: str):
    try:
        with open(path, "x", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "slug",
                    "title",
                    "opened_at_utc",
                    "closed_at_utc",
                    "market_end_iso",
                    "side",
                    "token_id",
                    "entry_price",
                    "exit_price",
                    "stake_usd",
                    "shares",
                    "exit_type",
                    "result_side",
                    "won",
                    "payout_usd",
                    "pnl_usd",
                ],
            )
            writer.writeheader()
    except FileExistsError:
        pass


def append_trade_csv(row: dict, path: str):
    ensure_csv(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "slug",
                "title",
                "opened_at_utc",
                "closed_at_utc",
                "market_end_iso",
                "side",
                "token_id",
                "entry_price",
                "exit_price",
                "stake_usd",
                "shares",
                "exit_type",
                "result_side",
                "won",
                "payout_usd",
                "pnl_usd",
            ],
        )
        writer.writerow(row)


def parse_token_ids_from_event(event_json: dict) -> tuple[str, str]:
    markets = event_json.get("markets") or []
    if not markets:
        raise ValueError("Fant ingen markets i event-responsen.")

    market = markets[0]
    raw = market.get("clobTokenIds")
    if raw is None:
        raise ValueError("Fant ikke 'clobTokenIds' i market-responsen.")

    if isinstance(raw, str):
        try:
            token_ids = json.loads(raw)
        except json.JSONDecodeError:
            token_ids = ast.literal_eval(raw)
    elif isinstance(raw, list):
        token_ids = raw
    else:
        raise ValueError(f"Ukjent format på clobTokenIds: {type(raw)}")

    if len(token_ids) < 2:
        raise ValueError("Forventet minst to token IDs.")

    up_token_id = str(token_ids[0])
    down_token_id = str(token_ids[1])
    return up_token_id, down_token_id


def fetch_event_by_slug(slug: str) -> dict:
    url = GAMMA_EVENT_BY_SLUG.format(slug=slug)
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json()


async def send_heartbeats(ws):
    while True:
        await asyncio.sleep(10)
        try:
            await ws.send("PING")
        except Exception:
            return


async def decision_loop(engine: PaperSignalEngine):
    while True:
        await asyncio.sleep(DECISION_EVERY_SECONDS)

        decision = engine.decide()
        decision["ts"] = datetime.now(timezone.utc).isoformat()
        decision["slug"] = engine.slug
        decision["title"] = engine.title
        decision["position_open"] = engine.position is not None
        print(json.dumps(decision, ensure_ascii=False))

        closed = engine.maybe_close_position_early(decision)
        if closed:
            print(json.dumps(closed, ensure_ascii=False))
            continue

        opened = engine.maybe_open_position(decision)
        if opened:
            print(json.dumps(opened, ensure_ascii=False))


def iter_ws_messages(payload) -> list[dict]:
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


async def run(slug: str):
    live_enabled = (os.getenv("POLY_LIVE_TRADING") or "1").strip().lower() in {
        "1", "true", "yes", "on"
    }
    live_trader = LiveTrader() if live_enabled else None

    event = fetch_event_by_slug(slug)
    title = event.get("title", slug)
    markets = event.get("markets") or []
    if not markets:
        raise RuntimeError("Fant ingen market i eventet.")

    market = markets[0]
    market_end_iso = market.get("endDate") or event.get("endDate")
    up_token_id, down_token_id = parse_token_ids_from_event(event)

    print(json.dumps({
        "mode": "paper-trading-safer",
        "live_trading_enabled": live_enabled,
        "slug": slug,
        "title": title,
        "market_end_iso": market_end_iso,
        "up_token_id": up_token_id,
        "down_token_id": down_token_id,
        "initial_bestBid": market.get("bestBid"),
        "initial_bestAsk": market.get("bestAsk"),
        "initial_lastTradePrice": market.get("lastTradePrice"),
        "csv_path": CSV_PATH,
        "config": {
            "DECISION_EVERY_SECONDS": DECISION_EVERY_SECONDS,
            "TRADE_SIZE_USD": TRADE_SIZE_USD,
            "EDGE_THRESHOLD_ENTER": EDGE_THRESHOLD_ENTER,
            "EDGE_THRESHOLD_EXIT": EDGE_THRESHOLD_EXIT,
            "EDGE_THRESHOLD_HOLD": EDGE_THRESHOLD_HOLD,
            "CONFIRMATIONS_REQUIRED": CONFIRMATIONS_REQUIRED,
            "REVERSAL_CONFIRMATIONS": REVERSAL_CONFIRMATIONS,
            "HISTORY_WINDOW_SECONDS": HISTORY_WINDOW_SECONDS,
            "NO_TRADE_LAST_SECONDS": NO_TRADE_LAST_SECONDS,
            "MIN_SECONDS_BEFORE_ENTRY": MIN_SECONDS_BEFORE_ENTRY,
            "COOLDOWN_AFTER_EXIT_SECONDS": COOLDOWN_AFTER_EXIT_SECONDS,
            "MAX_SPREAD": MAX_SPREAD,
            "MAX_DATA_AGE_SECONDS": MAX_DATA_AGE_SECONDS,
            "MIN_AVG_EDGE": MIN_AVG_EDGE,
            "MIN_ENTRY_PRICE": MIN_ENTRY_PRICE,
            "MAX_ENTRY_PRICE": MAX_ENTRY_PRICE,
        }
    }, ensure_ascii=False, indent=2))

    engine = PaperSignalEngine(
        slug=slug,
        title=title,
        up_token_id=up_token_id,
        down_token_id=down_token_id,
        market_end_iso=market_end_iso,
        live_trader=live_trader,
    )

    async with websockets.connect(WS_URL, ping_interval=None) as ws:
        await ws.send(json.dumps({
            "assets_ids": [up_token_id, down_token_id],
            "type": "market",
            "custom_feature_enabled": True,
        }))

        heartbeat_task = asyncio.create_task(send_heartbeats(ws))
        decision_task = asyncio.create_task(decision_loop(engine))

        try:
            async for raw in ws:
                if raw == "PONG":
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                for item in iter_ws_messages(msg):
                    event_type = item.get("event_type")

                    if event_type == "best_bid_ask":
                        asset_id = item.get("asset_id") or item.get("assetID")
                        best_bid = item.get("best_bid")
                        best_ask = item.get("best_ask")
                        if asset_id is not None:
                            engine.on_best_bid_ask(asset_id, best_bid, best_ask)

                    elif event_type == "market_resolved":
                        winning_asset_id = item.get("winning_asset_id")
                        winning_outcome = item.get("winning_outcome")
                        settled = engine.settle(winning_asset_id, winning_outcome)

                        print(json.dumps({
                            "action": "RESOLVED",
                            "slug": slug,
                            "title": title,
                            "winning_asset_id": winning_asset_id,
                            "winning_outcome": winning_outcome,
                            "settled_trade": settled,
                        }, ensure_ascii=False, indent=2))
                        return

        finally:
            heartbeat_task.cancel()
            decision_task.cancel()


def main():
    if len(sys.argv) < 2:
        print('Bruk: python polymarket_slug_papertrade_safer.py "btc-updown-5m-1773752700"')
        sys.exit(1)

    slug = sys.argv[1].strip()
    asyncio.run(run(slug))


if __name__ == "__main__":
    main()