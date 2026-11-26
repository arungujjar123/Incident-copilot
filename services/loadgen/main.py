import asyncio
import os
import random

import httpx

from common.logging_setup import get_logger

log = get_logger()

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8000")
RPS = int(os.getenv("REQUESTS_PER_SECOND", "5"))
SKUS = ["sku-keyboard", "sku-mouse", "sku-monitor", "sku-headset"]


async def one_customer(client, stats):
    items = [{"sku": random.choice(SKUS), "qty": random.randint(1, 3)} for _ in range(random.randint(1, 3))]
    try:
        resp = await client.post(f"{GATEWAY_URL}/checkout",
                                 json={"user_id": f"user-{random.randint(1, 500)}", "items": items})
        stats["ok" if resp.status_code == 200 else "failed"] += 1
    except httpx.HTTPError:
        stats["failed"] += 1


async def main():
    stats = {"ok": 0, "failed": 0}
    seconds = 0
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            await asyncio.gather(*(one_customer(client, stats) for _ in range(RPS)), asyncio.sleep(1))
            seconds += 1
            if seconds % 10 == 0:
                log.info("load stats", extra=stats)


if __name__ == "__main__":
    asyncio.run(main())