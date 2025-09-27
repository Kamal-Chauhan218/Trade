import os
from typing import Optional, Dict, Any, Union, List, Tuple
from pydantic import BaseModel, Field
from fastapi import HTTPException
import httpx
from dotenv import load_dotenv

load_dotenv()

"""
ENV (put in .env, values from your Dev Portal)

KOTAK_BASE_URL=https://gw-napi.kotaksecurities.com/Orders/2.0
KOTAK_AUTH_MODE=apikey # apikey | bearer | internal
KOTAK_API_KEY=... # if AUTH_MODE=apikey
KOTAK_ACCESS_TOKEN=... # if AUTH_MODE=bearer
KOTAK_INTERNAL_KEY=... # if AUTH_MODE=internal
KOTAK_CLIENT_CODE=Y1P0E

KOTAK_PATH_FUNDS=/quick/user/limits
KOTAK_FUNDS_METHOD=POST
KOTAK_PATH_POSITIONS=/portfolio/positions # adjust to your tenant path
KOTAK_PATH_ORDERS=/orders # adjust to your tenant path
KOTAK_PATH_ORDERS_OPEN= # optional dedicated open-orders endpoint
KOTAK_PATH_PLACE_ORDER=/quick/order/place # adjust to your tenant path
KOTAK_PATH_PNL=
KOTAK_PATH_EXPENSES=
KOTAK_PATH_CLOSE_ALL=
"""

# ============ DTOs ============
class OrderRequest(BaseModel):
    symbol: str
    exchange: str = "NFO"
    product: str = "MIS" # MIS/NRML/CNC per spec
    # Your models may send either "transaction_type" or "side"
    transaction_type: Optional[str] = None
    side: Optional[str] = None # "BUY" or "SELL"
    # quantity may come as "quantity" or "qty"
    quantity: Optional[int] = None
    qty: Optional[int] = None

    price: Optional[float] = None
    order_type: str = "MARKET" # MARKET/LIMIT/SL/SL-M
    validity: str = "DAY" # DAY/IOC
    disclosed_quantity: int = 0
    trigger_price: Optional[float] = None
    tag: Optional[str] = Field(default=None, description="Optional client tag")

# ============ ENV ============
KOTAK_BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
KOTAK_AUTH_MODE = os.getenv("KOTAK_AUTH_MODE", "bearer").lower()

KOTAK_API_KEY = os.getenv("KOTAK_API_KEY", "")
KOTAK_ACCESS_TOKEN = os.getenv("KOTAK_ACCESS_TOKEN", "")
KOTAK_INTERNAL_KEY = os.getenv("KOTAK_INTERNAL_KEY", "")
KOTAK_CLIENT_CODE = os.getenv("KOTAK_CLIENT_CODE", "")

PATH_FUNDS = os.getenv("KOTAK_PATH_FUNDS", "/quick/user/limits")
FUNDS_METHOD = os.getenv("KOTAK_FUNDS_METHOD", "GET").upper()

PATH_POSITIONS = os.getenv("KOTAK_PATH_POSITIONS", "/portfolio/positions")
PATH_ORDERS = os.getenv("KOTAK_PATH_ORDERS", "/orders")
PATH_ORDERS_OPEN = os.getenv("KOTAK_PATH_ORDERS_OPEN", "")
PATH_PLACE_ORDER = os.getenv("KOTAK_PATH_PLACE_ORDER", "/quick/order/place")
PATH_PNL = os.getenv("KOTAK_PATH_PNL", "")
PATH_EXPENSES = os.getenv("KOTAK_PATH_EXPENSES", "")
PATH_CLOSE_ALL = os.getenv("KOTAK_PATH_CLOSE_ALL", "")

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=15.0, read=15.0, write=15.0)

class KotakClient:
    """
    Thin async client around Kotak APIs.
    Base URL should include context+version (e.g., /Orders/2.0)
    """

    def __init__(self):
        if not KOTAK_BASE_URL:
            raise RuntimeError("KOTAK_BASE_URL is not set.")
        self._client = httpx.AsyncClient(base_url=KOTAK_BASE_URL, timeout=DEFAULT_TIMEOUT)

    def _auth_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "accept": "application/json",
            "content-type": "application/json",
        }

        mode = KOTAK_AUTH_MODE
        if mode == "apikey":
            if not KOTAK_API_KEY:
                raise HTTPException(status_code=500, detail="KOTAK_API_KEY not set but KOTAK_AUTH_MODE=apikey")
            headers["apikey"] = KOTAK_API_KEY # exact header name per gateway hint
        elif mode == "bearer":
            if not KOTAK_ACCESS_TOKEN:
                raise HTTPException(status_code=500, detail="KOTAK_ACCESS_TOKEN not set but KOTAK_AUTH_MODE=bearer")
            headers["authorization"] = f"Bearer {KOTAK_ACCESS_TOKEN}"
        elif mode == "internal":
            if not KOTAK_INTERNAL_KEY:
                raise HTTPException(status_code=500, detail="KOTAK_INTERNAL_KEY not set but KOTAK_AUTH_MODE=internal")
            headers["Internal-Key"] = KOTAK_INTERNAL_KEY
        else:
            raise HTTPException(status_code=500, detail=f"Unknown KOTAK_AUTH_MODE={mode}")

        if KOTAK_CLIENT_CODE:
            headers["x-client-code"] = KOTAK_CLIENT_CODE

        return headers

    async def _handle_response(self, resp: httpx.Response) -> Any:
        try:
            data = resp.json()
        except Exception:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        if resp.is_success:
            return data
        detail = data.get("message") or data.get("error") or data.get("errors") or data
        raise HTTPException(status_code=resp.status_code, detail=detail)

    @staticmethod
    def _extract_funds_value(data: Any) -> float:
        try:
            if isinstance(data, (int, float)):
                return float(data)
            if isinstance(data, dict):
                # direct keys
                for k in ("available", "availableCash", "cash", "netAvailable", "netCash", "limit"):
                    if k in data and isinstance(data[k], (int, float, str)):
                        return float(data[k])
                # nested under data
                d = data.get("data")
                if isinstance(d, dict):
                    for k in ("available", "availableCash", "cash", "netAvailable", "netCash", "limit"):
                        if k in d and isinstance(d[k], (int, float, str)):
                            return float(d[k])
                # nested under limits
                limits = data.get("limits")
                if isinstance(limits, dict):
                    for k in ("available", "availableCash", "cash", "netAvailable", "netCash", "limit"):
                        if k in limits and isinstance(limits[k], (int, float, str)):
                            return float(limits[k])
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Unexpected funds payload: {data}")

    # -------- public methods --------
    async def get_funds(self) -> float:
        headers = self._auth_headers()
        if FUNDS_METHOD == "POST":
            resp = await self._client.post(PATH_FUNDS, headers=headers, json={})
        else:
            resp = await self._client.get(PATH_FUNDS, headers=headers)
        data = await self._handle_response(resp)
        return self._extract_funds_value(data)

    async def get_positions(self) -> Any:
        headers = self._auth_headers()
        resp = await self._client.get(PATH_POSITIONS, headers=headers)
        return await self._handle_response(resp)

    async def get_orders(self, status: Optional[str] = None) -> Any:
        headers = self._auth_headers()
        params = {}
        if status:
            params["status"] = status
        resp = await self._client.get(PATH_ORDERS, headers=headers, params=params or None)
        return await self._handle_response(resp)

    async def get_open_orders(self) -> Any:
        headers = self._auth_headers()
        if PATH_ORDERS_OPEN:
            resp = await self._client.get(PATH_ORDERS_OPEN, headers=headers)
            return await self._handle_response(resp)
        # fallback via generic orders endpoint (if supported by tenant)
        resp = await self._client.get(PATH_ORDERS, headers=headers, params={"status": "OPEN"})
        return await self._handle_response(resp)

    async def place_order(
        self,
        order: Union[OrderRequest, Dict[str, Any], None] = None,
        **order_kwargs: Any
    ) -> Any:
        headers = self._auth_headers()

        if order is None and order_kwargs:
            payload_src = order_kwargs
        elif isinstance(order, OrderRequest):
            try:
                payload_src = order.model_dump() # pydantic v2
            except AttributeError:
                payload_src = order.dict() # pydantic v1
        elif isinstance(order, dict):
            payload_src = order
        else:
            raise HTTPException(status_code=400, detail="Missing order payload")

        # normalize synonyms
        txn = payload_src.get("transaction_type") or payload_src.get("side")
        qty = payload_src.get("quantity") or payload_src.get("qty")

        payload = {
            "symbol": payload_src.get("symbol"),
            "exchange": payload_src.get("exchange"),
            "productType": payload_src.get("product"),
            "transactionType": txn,
            "quantity": qty,
            "price": payload_src.get("price"),
            "orderType": payload_src.get("order_type"),
            "validity": payload_src.get("validity"),
            "disclosedQuantity": payload_src.get("disclosed_quantity", 0),
            "triggerPrice": payload_src.get("trigger_price"),
            "tag": payload_src.get("tag"),
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        resp = await self._client.post(PATH_PLACE_ORDER, headers=headers, json=payload)
        return await self._handle_response(resp)

    async def get_day_profit(self) -> float:
        if PATH_PNL:
            headers = self._auth_headers()
            resp = await self._client.get(PATH_PNL, headers=headers)
            data = await self._handle_response(resp)
            # TODO: extract real field here
            # return float(data["..."])
            pass
        return 0.0

    async def get_day_expenses(self) -> Dict[str, float]:
        if PATH_EXPENSES:
            headers = self._auth_headers()
            resp = await self._client.get(PATH_EXPENSES, headers=headers)
            data = await self._handle_response(resp)
            # TODO: map to keys below
            # return {"brokerage": ..., "taxes": ..., "other": ...}
            pass
        return {"brokerage": 0.0, "taxes": 0.0, "other": 0.0}

    async def close_all_positions(self) -> bool:
        if PATH_CLOSE_ALL:
            headers = self._auth_headers()
            resp = await self._client.post(PATH_CLOSE_ALL, headers=headers)
            _ = await self._handle_response(resp)
            return True
        return False

    # ---- attempts helper for probe ----
    def _attempts(self) -> List[Tuple[str, Dict[str, str]]]:
        attempts: List[Tuple[str, Dict[str, str]]] = []
        # bearer
        if KOTAK_ACCESS_TOKEN:
            attempts.append(("bearer", {
                "accept": "application/json",
                "content-type": "application/json",
                "authorization": f"Bearer {KOTAK_ACCESS_TOKEN}",
            }))
        # apikey
        if KOTAK_API_KEY:
            attempts.append(("apikey", {
                "accept": "application/json",
                "content-type": "application/json",
                "apikey": KOTAK_API_KEY,
            }))
            attempts.append(("x-api-key", {
                "accept": "application/json",
                "content-type": "application/json",
                "x-api-key": KOTAK_API_KEY,
            }))
        # internal key
        if KOTAK_INTERNAL_KEY:
            attempts.append(("Internal-Key", {
                "accept": "application/json",
                "content-type": "application/json",
                "Internal-Key": KOTAK_INTERNAL_KEY,
            }))
        # client code on all
        for _, h in attempts:
            if KOTAK_CLIENT_CODE:
                h["x-client-code"] = KOTAK_CLIENT_CODE
        return attempts

    async def close(self):
        await self._client.aclose()

# ---- export aliases for probes ----
BASE_URL = KOTAK_BASE_URL
AUTH_STYLE = KOTAK_AUTH_MODE
CONSUMER_KEY = os.getenv("KOTAK_CONSUMER_KEY", "")
CONSUMER_SECRET = os.getenv("KOTAK_CONSUMER_SECRET", "")
API_KEY = KOTAK_API_KEY
ACCESS_TOKEN_INITIAL = KOTAK_ACCESS_TOKEN
CLIENT_CODE = KOTAK_CLIENT_CODE

# Dependency helper (optional singleton)
_kotak_singleton: Optional[KotakClient] = None
async def get_kotak_client() -> KotakClient:
    global _kotak_singleton
    if _kotak_singleton is None:
        _kotak_singleton = KotakClient()
    return _kotak_singleton



class LoginRequest(BaseModel):
    mobileNumber: str = None
    pan: str = None
    password: str

class GenerateOtpRequest(BaseModel):
    userId: str
    sendEmail: bool = True
    isWhitelisted: bool = True

class LoginValidateRequest(BaseModel):
    otp: str

class FundsLimitsRequest(BaseModel):
    hsServerId: str = ""  # optional
    seg: str = "CASH"
    exch: str = "NSE"
    prod: str = "ALL"

class MarginRequest(BaseModel):
    brkName: str
    brnchId: str
    exSeg: str
    prc: str
    prcTp: str
    prod: str
    qty: str
    tok: str
    trnsTp: str
    slAbsOrTks: str | None = None
    slVal: str | None = None
    sqrOffAbsOrTks: str | None = None
    sqrOffVal: str | None = None
    trailSL: str | None = None
    trgPrc: str | None = None
    tSLTks: str | None = None