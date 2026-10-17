import os

import redis.asyncio as redis

# Async client: never blocks the server while waiting for Redis.
redis_client = redis.from_url(
    os.environ.get("REDIS_URL", "redis://localhost:6379/5"),
    decode_responses=True,
    socket_timeout=3,
    socket_connect_timeout=3,
    health_check_interval=30,
)
