import asyncio
from typing import Any, Dict, List

class AutoSellWorker:
    """
    Simple background worker that, when enabled, places SELL orders at (buy_price + y)
    for open positions. This is a demo scaffold; adapt to your real positions shape.
    """
    def __init__(self, kotak_client, y_default: float = 2.0, enabled_default: bool = False, interval_sec: int = 5):
        self.k = kotak_client
        self.y = y_default
        self.enabled = enabled_default
        self.interval_sec = interval_sec

    def update_config(self, y: float, enabled: bool):
        self.y = float(y)
        self.enabled = bool(enabled)

    async def run(self):
        while True:
            try:
                if self.enabled:
                    positions = await self.k.get_positions()
                    # Expect either a list or { "data": [...] }
                    if isinstance(positions, dict) and "data" in positions:
                        positions = positions["data"]
                    if not isinstance(positions, list):
                        positions = []

                    for p in positions:
                        # Example expected keys; adjust to your schema
                        symbol = p.get("symbol") or p.get("tradingsymbol")
                        qty = p.get("qty") or p.get("quantity") or 0
                        buy_price = p.get("buy_price") or p.get("avgPrice") or p.get("avg_buy_price")

                        if not symbol or not qty or not buy_price:
                            continue

                        target_sell = float(buy_price) + float(self.y)
                        await self.k.place_order(
                            symbol=symbol,
                            side="SELL",
                            qty=int(qty),
                            order_type="LIMIT",
                            price=round(target_sell, 2),
                        )
                await asyncio.sleep(self.interval_sec)
            except Exception:
                # Keep worker resilient; swallow and continue
                await asyncio.sleep(self.interval_sec)

