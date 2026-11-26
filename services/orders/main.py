import json
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from common.logging_setup import get_logger

log = get_logger()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shop:shop@postgres:5432/shop")
PAYMENTS_URL = os.getenv("PAYMENTS_URL", "http://payments:8002")
POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
POOL_TIMEOUT_S = float(os.getenv("DB_POOL_TIMEOUT_S", "2"))

pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=POOL_SIZE, timeout=POOL_TIMEOUT_S, open=False)
client = httpx.AsyncClient(timeout=float(os.getenv("DOWNSTREAM_TIMEOUT_S", "2")))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.open(wait=True, timeout=30)
    async with pool.connection() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id         SERIAL PRIMARY KEY,
                user_id    TEXT NOT NULL,
                amount     NUMERIC NOT NULL,
                items      JSONB NOT NULL,
                status     TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )""")
    log.info("orders service started", extra={"db_pool_size": POOL_SIZE})
    yield
    await pool.close()


app = FastAPI(title="orders", lifespan=lifespan)


class OrderRequest(BaseModel):
    user_id: str
    amount: float
    items: list[dict]


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/orders")
async def create_order(req: OrderRequest):
    # 1) Save the order as "pending"
    t0 = time.perf_counter()
    try:
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "INSERT INTO orders (user_id, amount, items, status) VALUES (%s, %s, %s, 'pending') RETURNING id",
                (req.user_id, req.amount, json.dumps(req.items)),
            )).fetchone()
    except Exception as e:
        log.error("database insert failed", extra={"user_id": req.user_id, "error_type": type(e).__name__, "detail": str(e)[:200]})
        raise HTTPException(500, "database error")
    order_id = row[0]
    db_ms = round((time.perf_counter() - t0) * 1000, 1)

    # 2) Ask payments to charge the customer
    try:
        resp = await client.post(f"{PAYMENTS_URL}/pay", json={"order_id": order_id, "amount": req.amount})
        resp.raise_for_status()
        status = resp.json()["status"]
    except httpx.TimeoutException:
        log.error("payments service timed out", extra={"order_id": order_id})
        status = "payment_timeout"
    except httpx.HTTPError as e:
        log.error("payments call failed", extra={"order_id": order_id, "error_type": type(e).__name__})
        status = "payment_error"

    # 3) Save the final status
    try:
        async with pool.connection() as conn:
            await conn.execute("UPDATE orders SET status = %s WHERE id = %s", (status, order_id))
    except Exception as e:
        log.error("database update failed", extra={"order_id": order_id, "error_type": type(e).__name__})
        raise HTTPException(500, "database error")

    if status in ("payment_timeout", "payment_error"):
        raise HTTPException(502, f"order {order_id}: {status}")

    log.info("order processed", extra={"order_id": order_id, "status": status, "db_ms": db_ms})
    return {"order_id": order_id, "status": status}