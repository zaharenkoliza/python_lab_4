"""SUT: FastAPI-сервис. Драйвер БД выбирается переменной окружения DRIVER.

sync_psycopg2  - синхронные def-эндпоинты (threadpool AnyIO) + psycopg2
async_asyncpg  - async def-эндпоинты + asyncpg
async_psycopg3 - async def-эндпоинты + psycopg 3 (async)

Одинаковы во всех вариантах: размер пула соединений (POOL_SIZE), лимит потоков
threadpool (= POOL_SIZE), SQL-запросы, сериализация ответа.
"""
import os
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

DRIVER = os.environ["DRIVER"]
DSN = os.environ.get("DSN", "postgresql://bench:bench@db:5432/bench")
POOL = int(os.environ.get("POOL_SIZE", "20"))
SLOW_SQL = "SELECT pg_sleep(0.01)"  # имитация I/O-ожидания 10 мс
ROWS_PG = "SELECT id, name, payload FROM items WHERE id >= $1 ORDER BY id LIMIT 100"
ROWS_PY = ROWS_PG.replace("$1", "%s")

state = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if DRIVER == "sync_psycopg2":
        import psycopg2.pool
        anyio.to_thread.current_default_thread_limiter().total_tokens = POOL
        state["pool"] = psycopg2.pool.ThreadedConnectionPool(POOL, POOL, DSN)
        yield
        state["pool"].closeall()
    elif DRIVER == "async_asyncpg":
        import asyncpg
        state["pool"] = await asyncpg.create_pool(DSN, min_size=POOL, max_size=POOL)
        yield
        await state["pool"].close()
    elif DRIVER == "async_psycopg3":
        from psycopg_pool import AsyncConnectionPool
        pool = AsyncConnectionPool(DSN, min_size=POOL, max_size=POOL, open=False)
        await pool.open(wait=True)
        state["pool"] = pool
        yield
        await pool.close()
    else:
        raise RuntimeError(f"unknown DRIVER={DRIVER}")


app = FastAPI(lifespan=lifespan)
Instrumentator().instrument(app).expose(app)


@app.get("/health")
async def health():
    return {"driver": DRIVER, "pool": POOL}


if DRIVER == "sync_psycopg2":
    def _q(sql, args=()):
        pool = state["pool"]
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchall() if cur.description else None
        finally:
            conn.rollback()
            pool.putconn(conn)

    @app.get("/rows")
    def rows(start: int = 1):
        return [{"id": i, "name": n, "payload": p} for i, n, p in _q(ROWS_PY, (start,))]

    @app.get("/slow")
    def slow():
        _q(SLOW_SQL)
        return {"ok": True}

elif DRIVER == "async_asyncpg":
    @app.get("/rows")
    async def rows(start: int = 1):
        async with state["pool"].acquire() as c:
            recs = await c.fetch(ROWS_PG, start)
        return [{"id": r[0], "name": r[1], "payload": r[2]} for r in recs]

    @app.get("/slow")
    async def slow():
        async with state["pool"].acquire() as c:
            await c.execute(SLOW_SQL)
        return {"ok": True}

else:  # async_psycopg3
    @app.get("/rows")
    async def rows(start: int = 1):
        async with state["pool"].connection() as c:
            cur = await c.execute(ROWS_PY, (start,))
            recs = await cur.fetchall()
        return [{"id": i, "name": n, "payload": p} for i, n, p in recs]

    @app.get("/slow")
    async def slow():
        async with state["pool"].connection() as c:
            await c.execute(SLOW_SQL)
        return {"ok": True}
