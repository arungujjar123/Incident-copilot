import os
import time

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from common.logging_setup import get_logger

log = get_logger()
app = FastAPI(title="gateway")

ORDERS_URL = os.getenv("ORDERS_URL", "http://orders:8001")
TIMEOUT_S = float(os.getenv("DOWNSTREAM_TIMEOUT_S", "3"))

PRODUCTS = {
    "sku-keyboard": 2499.0,
    "sku-mouse": 799.0,
    "sku-monitor": 12999.0,
    "sku-headset": 3499.0,
}

client = httpx.AsyncClient(timeout=TIMEOUT_S)


class Item(BaseModel):
    sku: str
    qty: int


class CheckoutRequest(BaseModel):
    user_id: str
    items: list[Item]


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/products")
async def products():
    return PRODUCTS


@app.post("/checkout")
async def checkout(req: CheckoutRequest):
    start = time.perf_counter()
    unknown = [i.sku for i in req.items if i.sku not in PRODUCTS]
    if unknown:
        raise HTTPException(400, f"unknown sku(s): {unknown}")

    amount = sum(PRODUCTS[i.sku] * i.qty for i in req.items)
    try:
        resp = await client.post(
            f"{ORDERS_URL}/orders",
            json={"user_id": req.user_id, "amount": amount, "items": [i.model_dump() for i in req.items]},
        )
        resp.raise_for_status()
    except httpx.TimeoutException:
        log.error("orders service timed out", extra={"user_id": req.user_id, "timeout_s": TIMEOUT_S})
        raise HTTPException(504, "orders service timed out")
    except httpx.HTTPStatusError as e:
        log.error("orders service returned error", extra={"user_id": req.user_id, "status_code": e.response.status_code})
        raise HTTPException(502, "orders service error")
    except httpx.RequestError as e:
        log.error("cannot reach orders service", extra={"user_id": req.user_id, "error_type": type(e).__name__})
        raise HTTPException(503, "orders service unreachable")

    order = resp.json()
    log.info("checkout completed", extra={
        "user_id": req.user_id, "order_id": order["order_id"], "status": order["status"],
        "amount": amount, "duration_ms": round((time.perf_counter() - start) * 1000, 1),
    })
    return order