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

TRADE_SIZE_USD = 1          # size of trade in USD
CSV_PATH = "minimal.csv"    #csv for logging

# https://docs.polymarket.com/api-reference/introduction

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