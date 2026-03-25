# test2.py
#
# Installer:
#   pip install requests websockets
#
# Kjør:
#   python test2.py "btc-updown-5m-1773752700"
#
# Hva scriptet gjør:
# - Henter event via slug
# - Leser ut Up/Down token IDs
# - Kobler til Polymarket market websocket
# - Leser live best bid/ask
# - Hvert 10. sekund vurderer signal: UP / DOWN / HOLD
# - Hvis ingen åpen posisjon finnes, åpner en teoretisk handel på $100
# - Når markedet resolves, regner ut gevinst/tap og lagrer til CSV
#
# Antakelse for paper trading:
# - Vi "kjøper" til best ask
# - Innsats = $100 hver gang
# - Antall andeler = 100 / entry_price
# - Ved korrekt utfall er andelen verdt $1, ellers $0
# - PnL = payout - 100

import asyncio
import ast
import csv
import importlib
import json
import os
from dotenv import load_dotenv
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import requests
import websockets

GAMMA_EVENT_BY_SLUG = "https://gamma-api.polymarket.com/events/slug/{slug}"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

DECISION_EVERY_SECONDS = 10
TRADE_SIZE_USD = 1.5
EDGE_THRESHOLD = 0.05        # hvor mye høyere én side må være enn den andre
NO_TRADE_LAST_SECONDS = 45   # ikke åpne nye papirhandler helt på slutten
CSV_PATH = "v1.csv"

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
                "Missing py-clob-client"
            ) from e

        private_key = (os.getenv("POLY_PRIVATE_KEY") or "").strip()
        if not private_key:
            raise RuntimeError("POLY_PRIVATE_KEY missing.")

        host = (os.getenv("POLY_CLOB_HOST") or "https://clob.polymarket.com").strip()
        chain_id = int((os.getenv("POLY_CHAIN_ID") or "137").strip())
        signature_type = int((os.getenv("POLY_SIGNATURE_TYPE") or "1").strip())
        funder = (os.getenv("POLY_FUNDER") or "").strip() or None

        if signature_type in {1, 2} and not funder:
            raise RuntimeError("POLY_FUNDER missing for signature type 1/2.")

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
            return {"ok": True, "response": resp}
        except Exception as e:
            return {"ok": False, "error": str(e)}


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
    side: str           # "UP" or "DOWN"
    token_id: str
    entry_price: float
    stake_usd: float
    shares: float
    market_end_iso: str

    def to_row(self, result_side: str, won: bool, payout: float, pnl: float):
        return {
            "slug": self.slug,
            "title": self.title,
            "opened_at_utc": datetime.fromtimestamp(self.opened_ts, tz=timezone.utc).isoformat(),
            "market_end_iso": self.market_end_iso,
            "side": self.side,
            "token_id": self.token_id,
            "entry_price": round(self.entry_price, 6),
            "stake_usd": round(self.stake_usd, 2),
            "shares": round(self.shares, 6),
            "result_side": result_side,
            "won": won,
            "payout_usd": round(payout, 2),
            "pnl_usd": round(pnl, 2),
        }


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

    def on_best_bid_ask(self, asset_id: str, bid, ask):
        asset_id = str(asset_id)
        if asset_id == self.up_token_id:
            self.up.update(bid, ask)
        elif asset_id == self.down_token_id:
            self.down.update(bid, ask)

    def seconds_to_market_end(self) -> float | None:
        try:
            dt = datetime.fromisoformat(self.market_end_iso.replace("Z", "+00:00"))
            end_ts = dt.timestamp()
            return end_ts - time.time()
        except Exception:
            return None

    def decide(self) -> dict:
        if self.up.mid is None or self.down.mid is None:
            return {
                "signal": "HOLD",
                "reason": "Mangler nok live-data",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": None,
            }

        secs_left = self.seconds_to_market_end()
        if secs_left is not None and secs_left <= NO_TRADE_LAST_SECONDS:
            return {
                "signal": "HOLD",
                "reason": f"Ingen nye handler siste {NO_TRADE_LAST_SECONDS}s",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": self.up.mid - self.down.mid,
            }

        edge = self.up.mid - self.down.mid

        if edge > EDGE_THRESHOLD:
            return {
                "signal": "UP",
                "reason": f"UP-mid {self.up.mid:.3f} > DOWN-mid {self.down.mid:.3f}",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": edge,
            }
        elif edge < -EDGE_THRESHOLD:
            return {
                "signal": "DOWN",
                "reason": f"DOWN-mid {self.down.mid:.3f} > UP-mid {self.up.mid:.3f}",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": edge,
            }
        else:
            return {
                "signal": "HOLD",
                "reason": "Forskjellen er for liten",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": edge,
            }

    def maybe_open_position(self, decision: dict):
        if self.position is not None:
            return None

        signal = decision["signal"]
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

        live_order = None
        if self.live_trader is not None:
            live_order = self.live_trader.place_buy_market_order(token_id=token_id, amount_usd=TRADE_SIZE_USD)
            if not live_order.get("ok"):
                return {
                    "action": "OPEN_FAILED",
                    "slug": self.slug,
                    "title": self.title,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "side": signal,
                    "stake_usd": TRADE_SIZE_USD,
                    "reason": decision["reason"],
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
            "reason": decision["reason"],
            "live_order": live_order,
        }

    def settle(self, winning_asset_id: str, winning_outcome: str):
        if self.position is None:
            return None

        pos = self.position
        won = str(winning_asset_id) == str(pos.token_id)
        payout = pos.shares * 1.0 if won else 0.0
        pnl = payout - pos.stake_usd

        # Normaliser outcome-navn litt
        result_side = "UP" if str(winning_outcome).strip().lower() in {"yes", "up"} else "DOWN"

        row = pos.to_row(result_side=result_side, won=won, payout=payout, pnl=pnl)
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
                    "market_end_iso",
                    "side",
                    "token_id",
                    "entry_price",
                    "stake_usd",
                    "shares",
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
                "market_end_iso",
                "side",
                "token_id",
                "entry_price",
                "stake_usd",
                "shares",
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

    # Polymarket docs: første er Yes, andre er No.
    up_token_id = str(token_ids[0])
    down_token_id = str(token_ids[1])
    return up_token_id, down_token_id


def fetch_event_by_slug(slug: str) -> dict:
    url = GAMMA_EVENT_BY_SLUG.format(slug=slug)
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json()


def poll_resolution(slug: str) -> dict | None:
    """Check REST API to see if the market has resolved.
    Returns {winning_asset_id, winning_outcome} or None."""
    try:
        event = fetch_event_by_slug(slug)
        markets = event.get("markets") or []
        if not markets:
            return None
        m = markets[0]
        # Check multiple fields that indicate resolution
        if m.get("resolved") or m.get("winner") or str(m.get("active", "true")).lower() == "false":
            winner = m.get("winner", "")
            winning_outcome = m.get("winningOutcome") or m.get("winner") or ""
            # Determine winning token ID
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
            # Winner mapping: Up/Yes = index 0, Down/No = index 1
            w_lower = str(winning_outcome).strip().lower()
            if w_lower in {"yes", "up", "true", "1"}:
                winning_asset_id = str(ids[0])
            elif w_lower in {"no", "down", "false", "0"}:
                winning_asset_id = str(ids[1])
            else:
                # Try from winner field directly
                winning_asset_id = str(ids[0]) if winner else str(ids[1])
            return {
                "winning_asset_id": winning_asset_id,
                "winning_outcome": winning_outcome,
            }
    except Exception as e:
        print(json.dumps({"action": "POLL_ERROR", "error": str(e)}, ensure_ascii=False))
    return None


async def send_heartbeats(ws):
    while True:
        await asyncio.sleep(10)
        try:
            await ws.send("PING")
        except Exception:
            return


async def decision_loop(engine: PaperSignalEngine, slug: str, title: str):
    while True:
        await asyncio.sleep(DECISION_EVERY_SECONDS)

        # Check if market has ended — poll REST API for resolution
        secs_left = engine.seconds_to_market_end()
        if secs_left is not None and secs_left < -10:
            # Market ended 10+ seconds ago, poll for result
            result = poll_resolution(slug)
            if result:
                settled = engine.settle(
                    result["winning_asset_id"],
                    result["winning_outcome"],
                )
                print(json.dumps({
                    "action": "RESOLVED_VIA_POLL",
                    "slug": slug,
                    "title": title,
                    "winning_asset_id": result["winning_asset_id"],
                    "winning_outcome": result["winning_outcome"],
                    "settled_trade": settled,
                }, ensure_ascii=False, indent=2))
                return  # done

        decision = engine.decide()
        decision["ts"] = datetime.now(timezone.utc).isoformat()
        decision["slug"] = engine.slug
        decision["title"] = engine.title
        decision["position_open"] = engine.position is not None
        print(json.dumps(decision, ensure_ascii=False))

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
    live_enabled = (os.getenv("POLY_LIVE_TRADING") or "1").strip().lower() in {"1", "true", "yes", "on"}
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
        "mode": "paper-trading",
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
    }, ensure_ascii=False, indent=2))

    engine = PaperSignalEngine(
        slug=slug,
        title=title,
        up_token_id=up_token_id,
        down_token_id=down_token_id,
        market_end_iso=market_end_iso,
        live_trader=live_trader,
    )

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
                    "assets_ids": [up_token_id, down_token_id],
                    "type": "market",
                    "custom_feature_enabled": True,
                }))

                heartbeat_task = asyncio.create_task(send_heartbeats(ws))
                decision_task = asyncio.create_task(decision_loop(engine, slug, title))

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

                                # Etter resolution er markedet ferdig
                                return
                finally:
                    heartbeat_task.cancel()
                    decision_task.cancel()

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
        print('Bruk: python polymarket_slug_papertrade.py "btc-updown-5m-1773752700"')
        sys.exit(1)

    slug = sys.argv[1].strip()
    asyncio.run(run(slug))


if __name__ == "__main__":
    main()