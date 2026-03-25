# test1.py
#
# Installer:
#   pip install requests websockets
#
# Kjør:
#   python test1.py "btc-updown-5m-1773752700"
#
# Dette scriptet:
# - henter event via /events/slug/{slug}
# - finner token IDs for Up og Down
# - kobler til Polymarket market websocket
# - følger best_bid_ask live
# - evaluerer hvert 10. sekund
# - skriver ut et teoretisk signal: UP / DOWN / HOLD
#

import asyncio
import ast
import json
import sys
import time
from dataclasses import dataclass, asdict

import requests
import websockets

GAMMA_EVENT_BY_SLUG = "https://gamma-api.polymarket.com/events/slug/{slug}"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

DECISION_EVERY_SECONDS = 10
EDGE_THRESHOLD = 0.02  # enkel buffer for små forskjeller


@dataclass
class BookState:
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
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
        elif bid is not None:
            self.mid = bid
        elif ask is not None:
            self.mid = ask
        else:
            self.mid = None

        self.last_update_ts = time.time()


class PaperSignalEngine:
    def __init__(self, up_token_id: str, down_token_id: str):
        self.up_token_id = str(up_token_id)
        self.down_token_id = str(down_token_id)
        self.up = BookState()
        self.down = BookState()

    def on_best_bid_ask(self, asset_id: str, bid, ask):
        asset_id = str(asset_id)
        if asset_id == self.up_token_id:
            self.up.update(bid, ask)
        elif asset_id == self.down_token_id:
            self.down.update(bid, ask)

    def decide(self) -> dict:
        if self.up.mid is None or self.down.mid is None:
            return {
                "signal": "HOLD",
                "reason": "Mangler nok live-data",
                "up_mid": self.up.mid,
                "down_mid": self.down.mid,
                "edge": None,
            }

        edge = self.up.mid - self.down.mid

        if edge > EDGE_THRESHOLD:
            signal = "UP"
            reason = f"UP-mid {self.up.mid:.3f} > DOWN-mid {self.down.mid:.3f}"
        elif edge < -EDGE_THRESHOLD:
            signal = "DOWN"
            reason = f"DOWN-mid {self.down.mid:.3f} > UP-mid {self.up.mid:.3f}"
        else:
            signal = "HOLD"
            reason = "Forskjellen er for liten"

        return {
            "signal": signal,
            "reason": reason,
            "up_bid": self.up.bid,
            "up_ask": self.up.ask,
            "up_mid": self.up.mid,
            "down_bid": self.down.bid,
            "down_ask": self.down.ask,
            "down_mid": self.down.mid,
            "edge": edge,
        }

    def snapshot(self) -> dict:
        return {"up": asdict(self.up), "down": asdict(self.down)}


def parse_token_ids_from_event(event_json: dict) -> tuple[str, str]:
    """
    Finner Up/Down token IDs fra første market i eventet.
    For disse markedene ligger de typisk i market['clobTokenIds'] som en JSON-liste i strengformat.
    Første token er Yes/Up, andre er No/Down.
    """
    markets = event_json.get("markets") or []
    if not markets:
        raise ValueError("Fant ingen markets i event-responsen.")

    market = markets[0]
    raw = market.get("clobTokenIds")
    if raw is None:
        raise ValueError("Fant ikke 'clobTokenIds' i market-responsen.")

    # Kan komme som string: '["id1","id2"]'
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
        raise ValueError("Forventet minst to token IDs (Up/Down).")

    up_token_id = str(token_ids[0])
    down_token_id = str(token_ids[1])
    return up_token_id, down_token_id


def fetch_event_by_slug(slug: str) -> dict:
    url = GAMMA_EVENT_BY_SLUG.format(slug=slug)
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("Forventet et enkelt event-objekt fra /events/slug/{slug}.")
    return data


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
        decision = engine.decide()
        decision["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        decision["slug"] = slug
        decision["title"] = title
        print(json.dumps(decision, ensure_ascii=False))


def iter_ws_messages(payload) -> list[dict]:
    if isinstance(payload, dict):
        return [payload]

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    return []


async def run(slug: str):
    event = fetch_event_by_slug(slug)
    up_token_id, down_token_id = parse_token_ids_from_event(event)

    title = event.get("title", "")
    market = (event.get("markets") or [{}])[0]

    print("Følger event:")
    print(json.dumps({
        "slug": slug,
        "title": title,
        "startTime": event.get("startTime"),
        "event_endDate": event.get("endDate"),
        "market_endDate": market.get("endDate"),
        "up_token_id": up_token_id,
        "down_token_id": down_token_id,
        "initial_bestBid": market.get("bestBid"),
        "initial_bestAsk": market.get("bestAsk"),
        "initial_lastTradePrice": market.get("lastTradePrice"),
    }, ensure_ascii=False, indent=2))

    engine = PaperSignalEngine(up_token_id=up_token_id, down_token_id=down_token_id)

    async with websockets.connect(WS_URL, ping_interval=None) as ws:
        sub_msg = {
            "assets_ids": [up_token_id, down_token_id],
            "type": "market",
            "custom_feature_enabled": True
        }
        await ws.send(json.dumps(sub_msg))

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
                    # best_bid_ask krever custom_feature_enabled=true
                    if item.get("event_type") != "best_bid_ask":
                        continue

                    asset_id = item.get("asset_id") or item.get("assetID")
                    best_bid = item.get("best_bid")
                    best_ask = item.get("best_ask")

                    if asset_id is None:
                        continue

                    engine.on_best_bid_ask(asset_id, best_bid, best_ask)

        finally:
            heartbeat_task.cancel()
            decision_task.cancel()


def main():
    if len(sys.argv) < 2:
        print('Bruk: python slug_follow_polymarket.py "btc-updown-5m-1773752700"')
        sys.exit(1)

    slug = sys.argv[1].strip()
    asyncio.run(run(slug))


if __name__ == "__main__":
    main()