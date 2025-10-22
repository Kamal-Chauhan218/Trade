from fastapi import FastAPI, HTTPException,Query,Request
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import os
import httpx
import json
import jwt
import pandas as pd
import numpy as np
import boto3
from datetime import datetime
import uuid

from app.models import (
    Funds,
    Profit,
    Expenses,
    PlaceOrderRequest,
    OrderResponse,
    AutoSellConfig,
)
from app.services.kotak import (
    KotakClient,
    LoginRequest,
    GenerateOtpRequest,
    LoginValidateRequest,
    FundsLimitsRequest,
    MarginRequest,
    AUTH_STYLE,
    BASE_URL,
    PATH_FUNDS,
    CONSUMER_KEY,
    CONSUMER_SECRET,
    API_KEY,
    ACCESS_TOKEN_INITIAL,
    CLIENT_CODE,
)
from app.workers.auto_sell import AutoSellWorker
from app.token_store import TokenStore
from fastapi import Body
from boto3.dynamodb.conditions import Key

app = FastAPI(title="OQE Trading Backend", version="1.0.0")

# CORS (relax during dev; tighten in prod)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # set your frontend origin in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Single shared client + worker
k = KotakClient()
worker = AutoSellWorker(k)
token_store = TokenStore()

# -------- Lifecycle --------
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(worker.run())

# -------- Health --------
@app.get("/")
async def root():
    return {"message": "Trading backend running ✅"}

@app.get("/health")
async def health():
    return {"status": "ok"}

# -------- Funds / P&L / Expenses --------
@app.get("/funds", response_model=Funds)
async def get_funds():
    try:
        available = await k.get_funds()
        return Funds(available=available)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Funds fetch failed: {e}")

@app.get("/profit", response_model=Profit)
async def get_profit():
    try:
        day_pnl = await k.get_day_profit()
        return Profit(day_profit=day_pnl)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Profit fetch failed: {e}")

@app.get("/expenses", response_model=Expenses)
async def get_expenses():
    try:
        e = await k.get_day_expenses() # expects keys: brokerage, taxes, other
        total = float(e["brokerage"]) + float(e["taxes"]) + float(e["other"])
        return Expenses(**e, total=total)
    except KeyError as ke:
        raise HTTPException(status_code=500, detail=f"Missing expenses key: {ke}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Expenses fetch failed: {e}")

# -------- Orders --------
@app.get("/orders/open")
async def get_open_orders():
    try:
        return await k.get_open_orders()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Open orders fetch failed: {e}")

@app.post("/orders/place", response_model=OrderResponse)
async def place_order(req: PlaceOrderRequest):
    try:
        # Pydantic v1/v2 compatibility
        try:
            payload = req.model_dump()
        except AttributeError:
            payload = req.dict()
        res = await k.place_order(**payload)
        return OrderResponse(order_id=str(res.get("order_id") or res.get("orderId") or res.get("id")),
                             status=str(res.get("status", "UNKNOWN")))
    except HTTPException:
        raise
    except KeyError as ke:
        raise HTTPException(status_code=500, detail=f"Order response missing field: {ke}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Order failed: {e}")

@app.post("/close_positions")
async def close_positions():
    try:
        closed = await k.close_all_positions()
        return {"closed": closed}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Close positions failed: {e}")

# -------- Auto-sell config --------
@app.post("/auto_sell/config")
async def set_auto_sell(cfg: AutoSellConfig):
    try:
        worker.update_config(y=cfg.y, enabled=cfg.enabled)
        return {"ok": True, "enabled": cfg.enabled, "y": cfg.y}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Auto-sell config failed: {e}")

# -------- Probes (diagnostics) --------
@app.get("/__probe/env-clean")
async def probe_env_clean():
    def info(val: str):
        if val is None:
            return {"len": 0, "starts": "", "ends": "", "trailing_space": False}
        tspace = (len(val) > 0 and val[-1].isspace())
        return {
            "len": len(val),
            "starts": val[:4] if len(val) >= 4 else val,
            "ends": val[-4:] if len(val) >= 4 else val,
            "trailing_space": tspace,
        }

    return {
        "auth_style": AUTH_STYLE,
        "consumer_key": info(CONSUMER_KEY),
        "consumer_secret": info(CONSUMER_SECRET),
        "api_key": info(API_KEY),
        "access_token": info(ACCESS_TOKEN_INITIAL),
        "client_code": info(CLIENT_CODE),
        "session": {"has_session": False, "sid_starts": "", "sid_ends": ""},
    }

@app.get("/__probe/funds")
async def probe_funds():
    base = os.getenv("KOTAK_BASE_URL", "")
    path = os.getenv("KOTAK_PATH_FUNDS", "/quick/user/limits")
    try:
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as c:
            r_root = await c.get("/")
            r_post = await c.post(path, json={})
        return {
            "base": base,
            "base_status": r_root.status_code,
            "funds_path": path,
            "funds_status_POST": r_post.status_code,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/__probe/funds-auth")
async def probe_funds_auth():
    base = os.getenv("KOTAK_BASE_URL", "")
    path = os.getenv("KOTAK_PATH_FUNDS", "/quick/user/limits")
    results = []
    try:
        async with httpx.AsyncClient(base_url=base, timeout=8.0) as c:
            for mode, headers in k._attempts():
                r = await c.post(path, json={}, headers=headers)
                # Try to show a compact sample body
                try:
                    body = r.json()
                    if isinstance(body, dict):
                        body = {k: body[k] for k in list(body.keys())[:2]}
                except Exception:
                    body = r.text[:120]
                results.append({"mode": mode, "status": r.status_code, "sample_body": body})
        return {"url": f"{base}{path}", "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/login/validate")
async def login_validate(data: LoginValidateRequest):
    base_url = "https://gw-napi.kotaksecurities.com"
    path = "/login/1.0/login/v2/validate"
    # Headers you got from step 1
    sid = await token_store.get_token("sid")
    access_token = await token_store.get_token("access_token")
    sub = await token_store.get_token("sub")
    headers = {
        "accept": "*/*",
        "sid": sid,         # generated in step 1
        "Auth": access_token, # token from step 1
        "Authorization": f"Bearer {os.getenv('KOTAK_ACCESS_TOKEN')}",
        "Content-Type": "application/json"
    }
    body = {"userId": sub, "otp": data.otp}

    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=8.0) as client:
            r = await client.post(path, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
            trade_token = data["data"]["token"]
            await token_store.set_token("trade_token",trade_token)
            return r.json()  # contains session token, sid, ucc, etc.
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/login/view-token")
async def generate_view_token(data: LoginRequest):
    """
    Step 1: Generate Kotak View Token (used to generate final session token)
    """
    base_url = "https://gw-napi.kotaksecurities.com"
    path = "/login/1.0/login/v2/validate"

    headers = {
        "accept": "*/*",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.getenv('KOTAK_ACCESS_TOKEN')}"  # access token from Kotak
    }

    body = {}
    if data.mobileNumber:
        body = {"mobileNumber": data.mobileNumber, "password": data.password}
    elif data.pan:
        body = {"pan": data.pan, "password": data.password}
    else:
        raise HTTPException(status_code=400, detail="Either mobileNumber or pan is required")

    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=8.0) as client:
            response = await client.post(path, json=body, headers=headers)
            response.raise_for_status()
            data = response.json()
            token = data['data']['token']
            sid = data['data']['sid']
            decoded = jwt.decode(token, options={"verify_signature": False})
            sub = decoded.get("sub") 
            await token_store.set_token("access_token", token)
            await token_store.set_token("sid", sid)
            await token_store.set_token("sub",sub)
            return response.json()  # contains view token and sid
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/login/otp/generate")
async def generate_otp():
    base_url = "https://gw-napi.kotaksecurities.com"
    path = "/login/1.0/login/otp/generate"

    # Headers for the request
    headers = {
        "accept": "*/*",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.getenv('KOTAK_ACCESS_TOKEN')}"  # Set your access token in env
    }
    sub = await token_store.get_token("sub")
    body = {
        "userId":sub,
        "sendEmail": True,
        "isWhitelisted": True,
    }
    print(body)
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=8.0) as client:
            response = await client.post(path, json=body, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
 
@app.post("/user/limits")   
async def get_user_limits(
    neo_fin_key: str = "neotradeapi",
    seg: str = "CASH",
    exch: str = "NSE",
    prod: str = "ALL"
):
    url = "https://gw-napi.kotaksecurities.com/Orders/2.0/quick/user/limits"

    headers = {
        "accept": "application/json",
        "Sid": os.getenv("SID"),
        "Auth": os.getenv("TRADE_ACCESS_TOKEN"),
        "Authorization": f"Bearer {os.getenv('KOTAK_ACCESS_TOKEN')}",
        "neo-fin-key": neo_fin_key,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    # jData must be stringified JSON
    form_data = {
        "jData": json.dumps({"seg": seg, "exch": exch, "prod": prod})
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, data=form_data, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/check-funds")
async def check_margin(sId: str = "server1"):
    """
    Wrapper for Kotak Check Margin API.
    """
    sid = await token_store.get_token("sid")
    trade_token = await token_store.get_token("trade_token")
    print(trade_token,"TRADE TOKEN")
    print(sid,"SIDDD")
    headers = {
        "accept": "application/json",
        "Sid": sid,   # sessionId from Login API
        "Auth":trade_token,
        "Authorization": f"Bearer {os.getenv('KOTAK_ACCESS_TOKEN')}",  # Bearer access token
        "neo-fin-key": "neotradeapi",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    url = f"https://gw-napi.kotaksecurities.com/Orders/2.0/quick/user/check-margin?sId={sId}"

    jdata = {
        "brkName": "KOTAK",
        "brnchId": "ONLINE",
        "exSeg": "nse_cm",
        "prc": "12500",
        "prcTp": "L",
        "prod": "CNC",
        "qty": "1",
        "tok": "11536",
        "trnsTp": "B"
    }
    # ✅ wrap JSON inside jData string
    form_data = {"jData": json.dumps(jdata)}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, data=form_data, headers=headers)
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=e.response.text
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/orders/normal")
async def place_order(orderData: dict = Body(...)):
    sid = await token_store.get_token("sid")
    trade_token = await token_store.get_token("trade_token")

    headers = {
        "accept": "application/json",
        "Sid": sid,
        "Auth": trade_token,
        "Authorization": f"Bearer {os.getenv('KOTAK_ACCESS_TOKEN')}",
        "neo-fin-key": "neotradeapi",
        "Content-Type": "application/x-www-form-urlencoded",  # required
    }

    form_data = {"jData": json.dumps(orderData)}

    PLACE_ORDER_PATH = "/Orders/2.0/quick/order/rule/ms/place"

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        try:
            response = await client.post(PLACE_ORDER_PATH, data=form_data, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.post("/orders/modify")
async def modify_order(orderData: dict = Body(...)):
    sid = await token_store.get_token("sid")
    trade_token = await token_store.get_token("trade_token")
    # hs_server_id = await token_store.get_token("hsServerId") or ""   # optional

    headers = {
        "accept": "application/json",
        "Sid": sid,
        "Auth": trade_token,
        "Authorization": f"Bearer {os.getenv('KOTAK_ACCESS_TOKEN')}",
        "neo-fin-key": "neotradeapi",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    form_data = {"jData": json.dumps(orderData)}

    MODIFY_ORDER_PATH = "/Orders/2.0/quick/order/vr/modify"

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        try:
            response = await client.post(MODIFY_ORDER_PATH, data=form_data, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))



@app.post("/orders/cancel")
async def cancel_order(order: dict = Body(...)):
    """
    Cancel an order in Kotak API
    Body Example:
    {
        "on": "2105199703091997",
        "am": "NO",
        "ts": "TCS-EQ"   # optional, only required if AMO
    }
    """
    sid = await token_store.get_token("sid")
    trade_token = await token_store.get_token("trade_token")

    headers = {
        "accept": "application/json",
        "Sid": sid,
        "Auth": trade_token,
        "Authorization": f"Bearer {os.getenv('KOTAK_ACCESS_TOKEN')}",
        "neo-fin-key": "neotradeapi",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    form_data = {"jData": json.dumps(order)}

    CANCEL_ORDER_PATH = "/Orders/2.0/quick/order/cancel"

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        try:
            response = await client.post(CANCEL_ORDER_PATH, data=form_data, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        

@app.get("/portfolio/holdings")
async def get_holdings():
    sid = await token_store.get_token("sid")
    trade_token = await token_store.get_token("trade_token")

    headers = {
        "accept": "*/*",
        "Sid": sid,
        "Auth": trade_token,
        "Authorization": f"Bearer {os.getenv('KOTAK_ACCESS_TOKEN')}",
        "neo-fin-key": "neotradeapi",  
    }

    params = {"alt": "false"}  # convert Python bool → "true"/"false"

    HOLDINGS_PATH = "/Portfolio/1.0/portfolio/v1/holdings"

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        try:
            response = await client.get(HOLDINGS_PATH, params=params, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))



@app.get("/master-scrip")
async def get_master_scrip():
    BASE_URL = "https://gw-napi.kotaksecurities.com/Files/1.0/masterscrip/v2/file-paths"

    """
    Fetch master scrip file paths from Kotak API
    """
    headers = {
        "accept": "*/*",
        "Authorization": f"Bearer {os.getenv('KOTAK_ACCESS_TOKEN')}"  # Store token in .env
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(BASE_URL, headers=headers)
            response.raise_for_status()
            data = response.json()

            # Extract filesPaths directly
            file_paths = data.get("data", {}).get("filesPaths", [])

            return {
                "status": "success",
                "count": len(file_paths),
                "files": file_paths
            }

        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/intial/scrips")
async def get_scrips(limit: int = 10):
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CSV_FILE = os.path.join(BASE_DIR, "nse_cm-v1.csv")    
    try:
        df = pd.read_csv(CSV_FILE)

        # Replace NaN, inf, -inf with None (valid JSON null)
        df = df.replace([pd.NA, float("nan"), float("inf"), float("-inf")], None)
        df = df.where(pd.notnull(df), None)

        # Convert first `limit` rows to dictionary
        data = df.head(limit).to_dict(orient="records")

        return {"count": len(data), "data": data}

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="CSV file not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/get_stock_data")
async def get_scrips(
    limit: int = 10,
    search: str | None = Query(None, description="Search by symbol name (e.g. VENKEYS)")
):
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CSV_FILE = os.path.join(BASE_DIR, "nse_cm-v1.csv")

    try:
        # Read CSV safely (read everything as string)
        df = pd.read_csv(CSV_FILE, low_memory=False)

        # Clean out all non-JSON values
        df = df.replace([np.nan, np.inf, -np.inf], None)

        if search:
            # Search by symbol name (case-insensitive)
            if "pSymbolName" in df.columns:
                df = df[df["pSymbolName"].astype(str).str.contains(search, case=False, na=False)]
            else:
                raise HTTPException(status_code=400, detail="Column 'pSymbolName' not found in CSV")

        # Limit to given number of rows
        df = df.head(limit)

        # Convert to JSON-safe Python objects
        data = df.to_dict(orient="records")

        return {"count": len(data), "data": data}

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="CSV file not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    
# AWS DYNAMO DB

dynamodb = boto3.resource(
    'dynamodb',
    region_name=os.getenv("AWS_DEFAULT_REGION"),
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
)
table = dynamodb.Table('watchlists')

@app.post("/watchlists")
def create_watchlist(user_id: str, name: str, stocks: list):
    watchlist_id = str(uuid.uuid4())
    item = {
        "user_id": user_id,
        "watchlist_id": watchlist_id,
        "name": name,
        "stocks": stocks,
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat()
    }
    table.put_item(Item=item)
    return {"message": "Watchlist created", "watchlist_id": watchlist_id}

#Fetching all watchlist according to user

@app.get("/watchlists/{user_id}")
def get_user_watchlists(user_id: str):
    response = table.query(
        KeyConditionExpression=Key("user_id").eq(user_id)
    )
    return {"watchlists": response["Items"]}

# ADDING STOCK TO WATCH LIST
# 🟢 Create or Add Stocks to a Watchlist
@app.post("/watchlists/add-stocks")
async def add_stocks(request: Request):
    body = await request.json()
    user_id = body["user_id"]
    watchlist_id = body["watchlist_id"]
    new_stocks = body["stocks"]

    # Add stocks or create a new watchlist if not exists
    response = table.update_item(
        Key={
            "user_id": user_id,
            "watchlist_id": watchlist_id
        },
        UpdateExpression="SET stocks = list_append(if_not_exists(stocks, :empty_list), :new_stocks)",
        ExpressionAttributeValues={
            ":new_stocks": new_stocks,
            ":empty_list": []
        },
        ReturnValues="UPDATED_NEW"
    )

    return {"message": "Stocks added successfully", "updated_stocks": response["Attributes"]["stocks"]}


# 🔵 Get specific watchlist for a user
@app.get("/watchlists/{user_id}/{watchlist_id}")
def get_watchlist(user_id: str, watchlist_id: str):
    response = table.get_item(
        Key={
            "user_id": user_id,
            "watchlist_id": watchlist_id
        }
    )
    return response.get("Item", {"message": "Watchlist not found"})