import asyncio
import os
import random

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from opentelemetry.instrumentation.redis import RedisInstrumentor

from common.logging_setup import get_logger
from common.observability import DEPENDENCY_ERRORS, setup_observability

log = get_logger()
app = FastAPI(title="payments")
setup_observability(app)
RedisInstrumentor().instrument()  # every Redis command becomes a span

r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_timeout=1.0)
DECLINE_RATE = float(os.getenv("DECLINE_RATE", "0.03"))  # normal card declines, not errors


class PayRequest(BaseModel):
    order_id: int
    amount: float


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/pay")
async def pay(req: PayRequest):
    key = f"payment:{req.order_id}"
    try:
        previous = await r.get(key)
        if previous:
            log.warning("duplicate payment request", extra={"order_id": req.order_id})
            return {"order_id": req.order_id, "status": previous.decode()}

        await asyncio.sleep(random.uniform(0.02, 0.08))  # pretend to call a bank
        status = "declined" if random.random() < DECLINE_RATE else "approved"
        await r.set(key, status, ex=3600)
    except redis.RedisError as e:
        DEPENDENCY_ERRORS.labels("redis", type(e).__name__).inc()
        log.error("redis error", extra={"order_id": req.order_id, "error_type": type(e).__name__})
        raise HTTPException(500, "payment store unavailable")

    log.info("payment processed", extra={"order_id": req.order_id, "status": status, "amount": req.amount})
    return {"order_id": req.order_id, "status": status}