# test3.py — Polymarket BTC 5-min UP/DOWN Live Trader
#
# Install:
#   pip install requests websockets python-dotenv py-clob-client
#
# Usage:
#   python test3.py "btc-updown-5m-1773752700"
#
# Strategy: "Patient conviction — hold to resolution"
# ----------------------------------------------------
# Key insight: in a 5-min binary market, stop-loss selling destroys
# performance because normal price noise triggers exits, each sell
# costs the spread, and re-entries compound losses.
#
# This strategy:
# 1. Waits 60+ seconds to gather clear market data
# 2. Requires a strong, consistent, GROWING edge before entering
# 3. Only buys at good prices (0.30-0.58) for favorable risk/reward
# 4. ONE trade per window — never re-enters after buying
# 5. HOLDS to resolution — no early selling
# 6. If market has no clear signal, SKIPS the window entirely
#
# The math: buying at 0.45 means win=$3.06 vs lose=$2.50.
# Even 50% accuracy is profitable at that price.

import asyncio
import ast
import csv
import importlib
import json
import math
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
import websockets
from dotenv import load_dotenv

load_dotenv()

# ── API endpoints ──────────────────────────────────────────────────────
GAMMA_EVENT_BY_SLUG = "https://gamma-api.polymarket.com/events/slug/{slug}"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ── Strategy tunables ──────────────────────────────────────────────────
DECISION_INTERVAL = 5          # seconds between decision ticks
TRADE_SIZE_USD = 1.0          # max dollars per 5-min window
WINDOW_SECONDS = 300           # 5-minute window

# Entry requirements — be very selective
EDGE_ENTER = 0.08             # min absolute edge to consider entry
EDGE_VOTE = 0.04              # min edge to count as a directional vote
MIN_AVG_EDGE = 0.05           # avg edge over recent history must exceed this
CONFIRMATIONS_ENTER = 4       # consecutive directional votes needed (4 × 5s = 20s)
MIN_EDGE_TREND = 0.02         # edge must be GROWING: recent avg > older avg by this

# Reversal detection — the real edge
# Pattern: UP rushes to 60-70% early, then DOWN takes over mid-window and wins
EARLY_PHASE_SEC = 60           # first 60s = "early phase" (noisy, don't trade)
REVERSAL_EDGE = 0.06          # if early avg was this far one way and now flipped → reversal
REVERSAL_CONFIRMATIONS = 3    # consecutive signals in new direction to confirm reversal

# NO stop-loss selling — hold every position to resolution
# The data proves stop-losses destroy performance in 5-min binary markets

HISTORY_WINDOW = 120           # seconds of signal history to keep
WAIT_BEFORE_ENTRY = 90         # wait 90s — skip the volatile opening
NO_TRADE_LAST_SEC = 120        # need 120s left — don't buy when prices are extreme
# Sweet spot: enter between 90s and 180s into the window (mid-window)

MAX_SPREAD = 0.05              # skip if spread on either side > this
MAX_DATA_AGE = 8               # seconds before data is considered stale
MIN_ENTRY_PRICE = 0.10         # only buy if ask >= 0.30 (not too uncertain)
MAX_ENTRY_PRICE = 0.90         # only buy if ask <= 0.70 (still decent risk/reward)

CSV_PATH = "v3_trades.csv"

# ── CSV fields ─────────────────────────────────────────────────────────
CSV_FIELDS = [
    "slug", "title",
    "opened_at_utc", "closed_at_utc", "market_end_iso",
    "side", "token_id",
    "entry_price", "exit_price",
    "stake_usd", "shares",
    "exit_type",           # "sell" | "resolved"
    "result_side", "won",
    "payout_usd", "pnl_usd",
]


# ═══════════════════════════════════════════════════════════════════════
#  Live Trader — handles BUY and SELL market orders
# ═══════════════════════════════════════════════════════════════════════
class LiveTrader:
    def __init__(self):
        try:
            clob_client_mod = importlib.import_module("py_clob_client.client")
            clob_types_mod = importlib.import_module("py_clob_client.clob_types")
            clob_const_mod = importlib.import_module("py_clob_client.order_builder.constants")
            ClobClient = getattr(clob_client_mod, "ClobClient")
            self.MarketOrderArgs = getattr(clob_types_mod, "MarketOrderArgs")
            self.OrderType = getattr(clob_types_mod, "OrderType")
            self.BUY = getattr(clob_const_mod, "BUY")
            self.SELL = getattr(clob_const_mod, "SELL")
        except Exception as e:
            raise RuntimeError("Missing py-clob-client: pip install py-clob-client") from e

        pk = (os.getenv("POLY_PRIVATE_KEY") or "").strip()
        if not pk:
            raise RuntimeError("POLY_PRIVATE_KEY not set in .env")

        host = (os.getenv("POLY_CLOB_HOST") or "https://clob.polymarket.com").strip()
        chain_id = int((os.getenv("POLY_CHAIN_ID") or "137").strip())
        sig_type = int((os.getenv("POLY_SIGNATURE_TYPE") or "1").strip())
        funder = (os.getenv("POLY_FUNDER") or "").strip() or None

        if sig_type in {1, 2} and not funder:
            raise RuntimeError("POLY_FUNDER required for signature type 1/2")

        self.client = ClobClient(
            host, key=pk, chain_id=chain_id,
            signature_type=sig_type, funder=funder,
        )
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def buy(self, token_id: str, amount_usd: float) -> dict:
        """Place a BUY market order. amount_usd = dollars to spend."""
        try:
            args = self.MarketOrderArgs(
                token_id=str(token_id),
                amount=float(amount_usd),
                side=self.BUY,
                order_type=self.OrderType.FOK,
            )
            signed = self.client.create_market_order(args)
            resp = self.client.post_order(signed, self.OrderType.FOK)
            return {"ok": True, "response": resp}
        except Exception as e:
            msg = str(e)
            unknown = "Request exception!" in msg and "status_code=None" in msg
            return {"ok": False, "error": msg, "unknown": unknown}

    def sell(self, token_id: str, shares: float) -> dict:
        """Place a SELL market order with automatic retry at reduced size.
        Floors shares to 2 decimal places and retries at 95%/90%/80% if
        balance error occurs (actual fill from buy is often less than theoretical).
        """
        # Try progressively smaller amounts to handle fee/rounding mismatch
        attempts = [1.0, 0.95, 0.90, 0.80]
        last_err = ""
        for factor in attempts:
            amt = math.floor(shares * factor * 100) / 100  # floor to 2 decimals
            if amt <= 0:
                continue
            try:
                args = self.MarketOrderArgs(
                    token_id=str(token_id),
                    amount=float(amt),
                    side=self.SELL,
                    order_type=self.OrderType.FOK,
                )
                signed = self.client.create_market_order(args)
                resp = self.client.post_order(signed, self.OrderType.FOK)
                return {"ok": True, "response": resp, "sold_shares": amt, "factor": factor}
            except Exception as e:
                last_err = str(e)
                # Only retry on balance errors
                if "not enough balance" not in last_err.lower():
                    break
        unknown = "Request exception!" in last_err and "status_code=None" in last_err
        return {"ok": False, "error": last_err, "unknown": unknown}


# ═══════════════════════════════════════════════════════════════════════
#  Book state — tracks best bid/ask for one side
# ═══════════════════════════════════════════════════════════════════════
@dataclass
class BookState:
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    spread: float | None = None
    updated: float | None = None

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
        self.updated = time.time()


# ═══════════════════════════════════════════════════════════════════════
#  Open position record
# ═══════════════════════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════════════════════
#  Trading engine
# ═══════════════════════════════════════════════════════════════════════
class TradingEngine:
    def __init__(
        self,
        slug: str,
        title: str,
        up_token_id: str,
        down_token_id: str,
        market_end_iso: str,
        trader: LiveTrader | None,
    ):
        self.slug = slug
        self.title = title
        self.up_token_id = str(up_token_id)
        self.down_token_id = str(down_token_id)
        self.market_end_iso = market_end_iso
        self.trader = trader

        self.up = BookState()
        self.down = BookState()

        self.position: Position | None = None
        self.closed_rows: list[dict] = []

        self.signal_history: deque = deque()
        self.early_edges: list[float] = []   # edges from first EARLY_PHASE_SEC
        self.early_avg: float | None = None  # avg edge during early phase
        self.start_ts = time.time()
        self.traded_this_window = False  # ONE trade per window, no re-entry

    # ── websocket data handler ─────────────────────────────────────────
    def on_book_update(self, asset_id: str, bid, ask):
        aid = str(asset_id)
        if aid == self.up_token_id:
            self.up.update(bid, ask)
        elif aid == self.down_token_id:
            self.down.update(bid, ask)

    # ── helpers ────────────────────────────────────────────────────────
    def secs_to_end(self) -> float | None:
        try:
            dt = datetime.fromisoformat(self.market_end_iso.replace("Z", "+00:00"))
            return dt.timestamp() - time.time()
        except Exception:
            return None

    def data_fresh(self) -> bool:
        now = time.time()
        if self.up.updated is None or self.down.updated is None:
            return False
        return (
            (now - self.up.updated) <= MAX_DATA_AGE
            and (now - self.down.updated) <= MAX_DATA_AGE
        )

    def spreads_ok(self) -> bool:
        if self.up.spread is None or self.down.spread is None:
            return False
        return self.up.spread <= MAX_SPREAD and self.down.spread <= MAX_SPREAD

    # ── signal history & trend analysis ────────────────────────────────
    def _push_signal(self, edge: float):
        now = time.time()
        age = now - self.start_ts
        self.signal_history.append((now, edge))
        while self.signal_history and (now - self.signal_history[0][0]) > HISTORY_WINDOW:
            self.signal_history.popleft()

        # Track early-phase edges (first EARLY_PHASE_SEC seconds)
        if age <= EARLY_PHASE_SEC:
            self.early_edges.append(edge)
            self.early_avg = sum(self.early_edges) / len(self.early_edges)

    def _recent_votes(self) -> list[str]:
        votes = []
        for _, e in self.signal_history:
            if e > EDGE_VOTE:
                votes.append("UP")
            elif e < -EDGE_VOTE:
                votes.append("DOWN")
            else:
                votes.append("HOLD")
        return votes

    def _confirmed(self, direction: str, n: int) -> bool:
        votes = self._recent_votes()
        tail = votes[-n:]
        return len(tail) == n and all(v == direction for v in tail)

    def _recent_avg_edge(self) -> float | None:
        """Average edge over the recent 30 seconds only."""
        if not self.signal_history:
            return None
        now = time.time()
        recent = [e for t, e in self.signal_history if (now - t) <= 30]
        if not recent:
            return None
        return sum(recent) / len(recent)

    def _edge_is_growing(self) -> bool:
        """Check that edge trend is GROWING (not just high).
        Compare avg of recent half vs older half of history."""
        if len(self.signal_history) < 6:
            return False
        edges = [e for _, e in self.signal_history]
        mid = len(edges) // 2
        older_avg = sum(edges[:mid]) / mid
        recent_avg = sum(edges[mid:]) / (len(edges) - mid)
        # Edge should be moving in the same direction and getting stronger
        if recent_avg > 0 and older_avg >= 0:
            return (recent_avg - older_avg) >= MIN_EDGE_TREND
        if recent_avg < 0 and older_avg <= 0:
            return (older_avg - recent_avg) >= MIN_EDGE_TREND
        return False

    def _detect_reversal(self) -> str | None:
        """Detect a mid-window reversal.
        If early phase was strongly UP but now consistently DOWN (or vice versa),
        that's a reversal signal — often the winning direction."""
        if self.early_avg is None:
            return None
        recent_avg = self._recent_avg_edge()
        if recent_avg is None:
            return None

        # Early was strongly UP, now flipped to DOWN
        if (self.early_avg > REVERSAL_EDGE
                and recent_avg < -EDGE_VOTE
                and self._confirmed("DOWN", REVERSAL_CONFIRMATIONS)):
            return "DOWN"

        # Early was strongly DOWN, now flipped to UP
        if (self.early_avg < -REVERSAL_EDGE
                and recent_avg > EDGE_VOTE
                and self._confirmed("UP", REVERSAL_CONFIRMATIONS)):
            return "UP"

        return None

    # ── core decision logic ────────────────────────────────────────────
    def decide(self) -> dict:
        base = {
            "up_mid": self.up.mid,
            "down_mid": self.down.mid,
            "up_bid": self.up.bid,
            "up_ask": self.up.ask,
            "down_bid": self.down.bid,
            "down_ask": self.down.ask,
        }

        if self.up.mid is None or self.down.mid is None:
            return {**base, "signal": "HOLD", "reason": "Waiting for data", "edge": None}

        if not self.data_fresh():
            return {**base, "signal": "HOLD", "reason": "Data stale", "edge": None}

        if not self.spreads_ok():
            return {**base, "signal": "HOLD", "reason": "Spread too wide", "edge": None}

        edge = self.up.mid - self.down.mid
        self._push_signal(edge)
        recent_avg = self._recent_avg_edge()
        growing = self._edge_is_growing()
        reversal = self._detect_reversal()

        base["edge"] = round(edge, 4)
        base["recent_avg"] = round(recent_avg, 4) if recent_avg else None
        base["early_avg"] = round(self.early_avg, 4) if self.early_avg else None
        base["trend_growing"] = growing
        base["reversal"] = reversal

        secs_left = self.secs_to_end()
        age = time.time() - self.start_ts

        # ── If we have a position: HOLD to resolution ──────────────
        if self.position is not None:
            return {**base, "signal": "HOLD",
                    "reason": f"Holding to resolution, edge={edge:.3f}"}

        # ── Already traded this window? Done. ──────────────────────
        if self.traded_this_window:
            return {**base, "signal": "HOLD",
                    "reason": "Already traded this window"}

        # ── Skip the volatile opening ──────────────────────────────
        if age < WAIT_BEFORE_ENTRY:
            return {**base, "signal": "HOLD",
                    "reason": f"Skipping opening noise ({age:.0f}/{WAIT_BEFORE_ENTRY}s)"}

        # ── Don't enter when prices are extreme near end ───────────
        if secs_left is not None and secs_left <= NO_TRADE_LAST_SEC:
            return {**base, "signal": "HOLD",
                    "reason": f"Too late, prices too extreme ({secs_left:.0f}s left)"}

        # ── Need some history ──────────────────────────────────────
        if recent_avg is None:
            return {**base, "signal": "HOLD", "reason": "Building history"}

        # ══════════════════════════════════════════════════════════════
        # SIGNAL 1: REVERSAL (highest priority — this is where the edge is)
        # Early phase was strongly one way, but now the market has flipped.
        # This is the "UP goes to 60-70% then DOWN takes over" pattern.
        # ══════════════════════════════════════════════════════════════
        if reversal is not None:
            if reversal == "UP":
                return {**base, "signal": "BUY_UP",
                        "reason": f"REVERSAL: early_avg={self.early_avg:.3f}(DOWN) → now UP edge={edge:.3f}"}
            else:
                return {**base, "signal": "BUY_DOWN",
                        "reason": f"REVERSAL: early_avg={self.early_avg:.3f}(UP) → now DOWN edge={edge:.3f}"}

        # ══════════════════════════════════════════════════════════════
        # SIGNAL 2: MOMENTUM (consistent direction that's building)
        # Only if edge is strong, recent average confirms, and trend growing.
        # ══════════════════════════════════════════════════════════════
        if (
            edge > EDGE_ENTER
            and recent_avg > MIN_AVG_EDGE
            and self._confirmed("UP", CONFIRMATIONS_ENTER)
            and growing
        ):
            return {**base, "signal": "BUY_UP",
                    "reason": f"Momentum UP: edge={edge:.3f} avg={recent_avg:.3f} trend=growing"}

        if (
            edge < -EDGE_ENTER
            and recent_avg < -MIN_AVG_EDGE
            and self._confirmed("DOWN", CONFIRMATIONS_ENTER)
            and growing
        ):
            return {**base, "signal": "BUY_DOWN",
                    "reason": f"Momentum DOWN: edge={edge:.3f} avg={recent_avg:.3f} trend=growing"}

        return {**base, "signal": "HOLD",
                "reason": f"Waiting for signal (edge={edge:.3f}, trend={'growing' if growing else 'flat'})"}

    # ── execute BUY ────────────────────────────────────────────────────
    def execute_buy(self, decision: dict) -> dict | None:
        if self.position is not None:
            return None

        signal = decision.get("signal")
        if signal == "BUY_UP":
            side, ask, token_id = "UP", self.up.ask, self.up_token_id
        elif signal == "BUY_DOWN":
            side, ask, token_id = "DOWN", self.down.ask, self.down_token_id
        else:
            return None

        if ask is None or ask <= 0:
            return None
        if ask < MIN_ENTRY_PRICE or ask > MAX_ENTRY_PRICE:
            return {"action": "SKIP", "reason": f"Ask {ask:.3f} outside [{MIN_ENTRY_PRICE}, {MAX_ENTRY_PRICE}]"}

        # ── place live order ───────────────────────────────────────
        live_result = None
        if self.trader:
            live_result = self.trader.buy(token_id, TRADE_SIZE_USD)
            if not live_result.get("ok"):
                return {
                    "action": "BUY_FAILED",
                    "side": side, "token_id": token_id,
                    "error": live_result.get("error"),
                }

        shares = TRADE_SIZE_USD / ask
        self.traded_this_window = True  # ONE trade per window — no re-entry
        self.position = Position(
            slug=self.slug, title=self.title,
            opened_ts=time.time(), side=side,
            token_id=token_id, entry_price=ask,
            stake_usd=TRADE_SIZE_USD, shares=shares,
            market_end_iso=self.market_end_iso,
        )

        log = {
            "action": "BUY",
            "ts": datetime.now(timezone.utc).isoformat(),
            "side": side, "token_id": token_id,
            "entry_price": round(ask, 6),
            "stake_usd": TRADE_SIZE_USD,
            "shares": round(shares, 6),
            "reason": decision.get("reason"),
        }
        if live_result:
            log["live"] = live_result
        return log

    # ── execute SELL (early exit) ──────────────────────────────────────
    def execute_sell(self, decision: dict) -> dict | None:
        if self.position is None:
            return None
        if decision.get("signal") != "SELL":
            return None

        pos = self.position
        bid = self.up.bid if pos.side == "UP" else self.down.bid

        if bid is None or bid <= 0:
            return {"action": "SELL_SKIP", "reason": "No bid available"}

        # ── place live sell order ──────────────────────────────────
        live_result = None
        sell_failed = False
        if self.trader:
            live_result = self.trader.sell(pos.token_id, pos.shares)
            if not live_result.get("ok"):
                sell_failed = True
                print(json.dumps({
                    "action": "SELL_FAILED",
                    "side": pos.side, "token_id": pos.token_id,
                    "shares": round(pos.shares, 6),
                    "error": live_result.get("error"),
                    "note": "Clearing position to avoid infinite retry loop. Check balance manually.",
                }, ensure_ascii=False))
                # Still clear the position so we don't spam failed sells forever

        payout = pos.shares * bid
        pnl = payout - pos.stake_usd

        row = self._make_row(pos, exit_price=bid, exit_type="sell", payout=payout, pnl=pnl)
        self.closed_rows.append(row)
        _append_csv(row)

        self.position = None
        self.last_sell_ts = time.time()

        log = {
            "action": "SELL" if not sell_failed else "SELL_CLEARED",
            "ts": datetime.now(timezone.utc).isoformat(),
            "side": pos.side, "token_id": pos.token_id,
            "entry_price": round(pos.entry_price, 6),
            "exit_price": round(bid, 6),
            "shares": round(pos.shares, 6),
            "payout_usd": round(payout, 2),
            "pnl_usd": round(pnl, 2),
            "reason": decision.get("reason"),
            "sell_failed": sell_failed,
        }
        if live_result:
            log["live"] = live_result
        return log

    # ── settle on market resolution ────────────────────────────────────
    def settle(self, winning_asset_id: str, winning_outcome: str) -> dict | None:
        if self.position is None:
            return None

        pos = self.position
        won = str(winning_asset_id) == str(pos.token_id)
        payout = pos.shares * 1.0 if won else 0.0
        pnl = payout - pos.stake_usd

        result_side = "UP" if str(winning_outcome).strip().lower() in {"yes", "up"} else "DOWN"

        row = self._make_row(
            pos, exit_price=(1.0 if won else 0.0),
            exit_type="resolved", payout=payout, pnl=pnl,
            result_side=result_side, won=won,
        )
        self.closed_rows.append(row)
        _append_csv(row)
        self.position = None
        return row

    # ── CSV row builder ────────────────────────────────────────────────
    @staticmethod
    def _make_row(
        pos: Position, *, exit_price: float, exit_type: str,
        payout: float, pnl: float,
        result_side: str = "", won = "",
    ) -> dict:
        return {
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
            "exit_type": exit_type,
            "result_side": result_side,
            "won": won,
            "payout_usd": round(payout, 2),
            "pnl_usd": round(pnl, 2),
        }


# ═══════════════════════════════════════════════════════════════════════
#  CSV helpers
# ═══════════════════════════════════════════════════════════════════════
def _ensure_csv():
    try:
        with open(CSV_PATH, "x", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()
    except FileExistsError:
        pass


def _append_csv(row: dict):
    _ensure_csv()
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow(row)


# ═══════════════════════════════════════════════════════════════════════
#  Event / slug helpers
# ═══════════════════════════════════════════════════════════════════════
def fetch_event(slug: str) -> dict:
    url = GAMMA_EVENT_BY_SLUG.format(slug=slug)
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json()


def parse_token_ids(event: dict) -> tuple[str, str]:
    markets = event.get("markets") or []
    if not markets:
        raise ValueError("No markets in event")

    raw = markets[0].get("clobTokenIds")
    if raw is None:
        raise ValueError("No clobTokenIds in market")

    if isinstance(raw, str):
        try:
            ids = json.loads(raw)
        except json.JSONDecodeError:
            ids = ast.literal_eval(raw)
    elif isinstance(raw, list):
        ids = raw
    else:
        raise ValueError(f"Unknown clobTokenIds format: {type(raw)}")

    if len(ids) < 2:
        raise ValueError("Expected at least 2 token IDs")

    return str(ids[0]), str(ids[1])  # UP, DOWN


# ═══════════════════════════════════════════════════════════════════════
#  WebSocket message parser
# ═══════════════════════════════════════════════════════════════════════
def iter_messages(payload) -> list[dict]:
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def poll_resolution(slug: str) -> dict | None:
    """Check REST API to see if the market has resolved."""
    try:
        event = fetch_event(slug)
        markets = event.get("markets") or []
        if not markets:
            return None
        m = markets[0]
        if m.get("resolved") or m.get("winner") or str(m.get("active", "true")).lower() == "false":
            winning_outcome = m.get("winningOutcome") or m.get("winner") or ""
            raw = m.get("clobTokenIds")
            if raw is None:
                return None
            if isinstance(raw, str):
                try:
                    ids = json.loads(raw)
                except json.JSONDecodeError:
                    ids = ast.literal_eval(raw)
            elif isinstance(raw, list):
                ids = raw
            else:
                return None
            if len(ids) < 2:
                return None
            w_lower = str(winning_outcome).strip().lower()
            if w_lower in {"yes", "up", "true", "1"}:
                winning_asset_id = str(ids[0])
            elif w_lower in {"no", "down", "false", "0"}:
                winning_asset_id = str(ids[1])
            else:
                winner = m.get("winner")
                winning_asset_id = str(ids[0]) if winner else str(ids[1])
            return {"winning_asset_id": winning_asset_id, "winning_outcome": winning_outcome}
    except Exception as e:
        print(json.dumps({"action": "POLL_ERROR", "error": str(e)}, ensure_ascii=False))
    return None


# ═══════════════════════════════════════════════════════════════════════
#  Async tasks
# ═══════════════════════════════════════════════════════════════════════
async def heartbeat(ws):
    while True:
        await asyncio.sleep(10)
        try:
            await ws.send("PING")
        except Exception:
            return


async def decision_loop(engine: TradingEngine):
    while True:
        await asyncio.sleep(DECISION_INTERVAL)

        # Check if market has ended — poll REST API for resolution
        secs_left = engine.secs_to_end()
        if secs_left is not None and secs_left < -10:
            result = poll_resolution(engine.slug)
            if result:
                settled = engine.settle(
                    result["winning_asset_id"],
                    result["winning_outcome"],
                )
                print(json.dumps({
                    "action": "RESOLVED_VIA_POLL",
                    "slug": engine.slug,
                    "title": engine.title,
                    "winning_asset_id": result["winning_asset_id"],
                    "winning_outcome": result["winning_outcome"],
                    "settled": settled,
                }, ensure_ascii=False, indent=2))
                return

        decision = engine.decide()
        decision["ts"] = datetime.now(timezone.utc).isoformat()
        decision["slug"] = engine.slug
        decision["has_position"] = engine.position is not None
        decision["traded"] = engine.traded_this_window
        if engine.position:
            decision["pos_side"] = engine.position.side
            decision["pos_entry"] = round(engine.position.entry_price, 4)
        print(json.dumps(decision, ensure_ascii=False))

        # Try buy (entry) — no selling, we hold to resolution
        bought = engine.execute_buy(decision)
        if bought:
            print(json.dumps(bought, ensure_ascii=False))


# ═══════════════════════════════════════════════════════════════════════
#  Main run loop
# ═══════════════════════════════════════════════════════════════════════
async def run(slug: str):
    live_on = (os.getenv("POLY_LIVE_TRADING") or "1").strip().lower() in {"1", "true", "yes", "on"}
    trader = LiveTrader() if live_on else None

    event = fetch_event(slug)
    title = event.get("title", slug)
    markets = event.get("markets") or []
    if not markets:
        raise RuntimeError("No markets found in event")

    market = markets[0]
    end_iso = market.get("endDate") or event.get("endDate")
    up_id, down_id = parse_token_ids(event)

    print(json.dumps({
        "mode": "v3-live-buy-sell",
        "live": live_on,
        "slug": slug,
        "title": title,
        "market_end": end_iso,
        "up_token": up_id,
        "down_token": down_id,
        "bestBid": market.get("bestBid"),
        "bestAsk": market.get("bestAsk"),
        "lastPrice": market.get("lastTradePrice"),
        "csv": CSV_PATH,
        "config": {
            "strategy": "mid-window-reversal-hold-to-resolution",
            "TRADE_SIZE_USD": TRADE_SIZE_USD,
            "DECISION_INTERVAL": DECISION_INTERVAL,
            "EDGE_ENTER": EDGE_ENTER,
            "REVERSAL_EDGE": REVERSAL_EDGE,
            "EARLY_PHASE_SEC": EARLY_PHASE_SEC,
            "MIN_AVG_EDGE": MIN_AVG_EDGE,
            "MIN_EDGE_TREND": MIN_EDGE_TREND,
            "CONFIRMATIONS_ENTER": CONFIRMATIONS_ENTER,
            "REVERSAL_CONFIRMATIONS": REVERSAL_CONFIRMATIONS,
            "WAIT_BEFORE_ENTRY": WAIT_BEFORE_ENTRY,
            "NO_TRADE_LAST_SEC": NO_TRADE_LAST_SEC,
            "entry_window": f"{WAIT_BEFORE_ENTRY}s to {WINDOW_SECONDS - NO_TRADE_LAST_SEC}s",
            "MIN_ENTRY_PRICE": MIN_ENTRY_PRICE,
            "MAX_ENTRY_PRICE": MAX_ENTRY_PRICE,
            "early_sell": "DISABLED",
            "max_trades_per_window": 1,
        },
    }, ensure_ascii=False, indent=2))

    engine = TradingEngine(
        slug=slug, title=title,
        up_token_id=up_id, down_token_id=down_id,
        market_end_iso=end_iso, trader=trader,
    )

    # Reconnect loop — websocket drops are normal, just reconnect
    MAX_RECONNECTS = 20
    reconnect_count = 0

    while reconnect_count < MAX_RECONNECTS:
        try:
            async with websockets.connect(WS_URL, ping_interval=None) as ws:
                if reconnect_count > 0:
                    print(json.dumps({
                        "action": "RECONNECTED",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "attempt": reconnect_count,
                    }, ensure_ascii=False))

                await ws.send(json.dumps({
                    "assets_ids": [up_id, down_id],
                    "type": "market",
                    "custom_feature_enabled": True,
                }))

                hb_task = asyncio.create_task(heartbeat(ws))
                dec_task = asyncio.create_task(decision_loop(engine))

                try:
                    async for raw in ws:
                        if raw == "PONG":
                            continue

                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        for item in iter_messages(msg):
                            evt = item.get("event_type")

                            if evt == "best_bid_ask":
                                aid = item.get("asset_id") or item.get("assetID")
                                if aid is not None:
                                    engine.on_book_update(
                                        aid,
                                        item.get("best_bid"),
                                        item.get("best_ask"),
                                    )

                            elif evt == "market_resolved":
                                w_id = item.get("winning_asset_id")
                                w_out = item.get("winning_outcome")
                                settled = engine.settle(w_id, w_out)

                                print(json.dumps({
                                    "action": "RESOLVED",
                                    "slug": slug,
                                    "title": title,
                                    "winning_asset_id": w_id,
                                    "winning_outcome": w_out,
                                    "settled": settled,
                                }, ensure_ascii=False, indent=2))
                                return  # market is done, exit cleanly
                finally:
                    hb_task.cancel()
                    dec_task.cancel()

        except (websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.ConnectionClosedOK,
                ConnectionError,
                OSError) as e:
            reconnect_count += 1
            wait = min(2 * reconnect_count, 10)
            print(json.dumps({
                "action": "WS_DISCONNECTED",
                "ts": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
                "reconnect_in": wait,
                "attempt": reconnect_count,
            }, ensure_ascii=False))
            await asyncio.sleep(wait)

    print("Max reconnects reached, exiting.")


def main():
    if len(sys.argv) < 2:
        print('Usage: python v3.py "btc-updown-5m-1773752700"')
        sys.exit(1)

    slug = sys.argv[1].strip()
    asyncio.run(run(slug))


if __name__ == "__main__":
    main()
