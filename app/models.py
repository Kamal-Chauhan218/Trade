from pydantic import BaseModel
from typing import Optional

class Funds(BaseModel):
    available: float

class Profit(BaseModel):
    day_profit: float

class Expenses(BaseModel):
    brokerage: float
    taxes: float
    other: float
    total: float

class PlaceOrderRequest(BaseModel):
    # Required
    symbol: str
    # Common defaults
    exchange: str = "NFO" # or "NSE"/"BSE" for equities
    product: str = "MIS" # MIS/NRML/CNC as per broker
    # Either provide transaction_type or side; either quantity or qty
    transaction_type: Optional[str] = None # "BUY"/"SELL"
    side: Optional[str] = None # alias for transaction_type
    quantity: Optional[int] = None
    qty: Optional[int] = None
    # Optional order params
    price: Optional[float] = None
    order_type: str = "MARKET" # MARKET/LIMIT/SL/SL-M
    validity: str = "DAY" # DAY/IOC
    disclosed_quantity: int = 0
    trigger_price: Optional[float] = None
    tag: Optional[str] = None

class OrderResponse(BaseModel):
    order_id: str
    status: str

class AutoSellConfig(BaseModel):
    y: float
    enabled: bool