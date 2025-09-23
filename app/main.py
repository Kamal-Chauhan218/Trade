from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import os
import httpx
import json
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
    headers = {
        "accept": "*/*",
        "sid": os.getenv("SID"),         # generated in step 1
        "Auth": os.getenv("ACCESS_TOKEN"), # token from step 1
        "Authorization": f"Bearer {os.getenv('KOTAK_ACCESS_TOKEN')}",
        "Content-Type": "application/json"
    }
    print(headers,"HEADERS")
    body = {"userId": data.userId, "otp": data.otp}

    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=8.0) as client:
            r = await client.post(path, json=body, headers=headers)
            r.raise_for_status()
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
            return response.json()  # contains view token and sid
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/login/otp/generate")
async def generate_otp(data: GenerateOtpRequest):
    base_url = "https://gw-napi.kotaksecurities.com"
    path = "/login/1.0/login/otp/generate"

    # Headers for the request
    headers = {
        "accept": "*/*",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.getenv('KOTAK_ACCESS_TOKEN')}"  # Set your access token in env
    }

    body = {
        "userId": data.userId,
        "sendEmail": data.sendEmail,
        "isWhitelisted": data.isWhitelisted
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
    
@app.post("/check-margin")
async def check_margin(sId: str = "server1"):
    """
    Wrapper for Kotak Check Margin API.
    """

    headers = {
        "accept": "application/json",
        "Sid": os.getenv("SID"),   # sessionId from Login API
        "Auth": os.getenv("TRADE_ACCESS_TOKEN"),
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