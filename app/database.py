from uuid import uuid4

from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import get_settings

settings = get_settings()

# ── Connection pool strategy ───────────────────────────────────────────────────
#
# Streamlit Cloud is a serverless-style environment: each page run is short-
# lived, there is no persistent process that benefits from a warm pool, and
# Supabase's Transaction Pooler (PgBouncer, pool_mode=transaction) already
# does the real pooling on its side.
#
# Using NullPool means SQLAlchemy opens a fresh asyncpg connection for every
# AsyncSessionLocal() context and closes it immediately on exit.  This avoids
# stale connections (PgBouncer aggressively drops idle backends) and cross-
# event-loop reuse issues (Streamlit reruns pages in different asyncio tasks).
#
# ── PgBouncer + prepared statements ──────────────────────────────────────────
# Even with NullPool on the client side, Supabase's Transaction Pooler reuses
# *server-side* backend connections across unrelated clients.  That means two
# SQLAlchemy sessions on different asyncpg connections can still land on the
# same Postgres backend, where they try to PREPARE statements with the same
# auto-generated name ("__asyncpg_stmt_9__" etc.) and raise
# DuplicatePreparedStatementError.
#
# Three settings together make this safe:
#
#   • statement_cache_size=0 (asyncpg connect_arg) — disables asyncpg's own
#     client-side prepared-statement LRU cache.
#   • prepared_statement_cache_size=0 (URL query param on the asyncpg dialect)
#     — disables SQLAlchemy's asyncpg-dialect prepared-statement cache.  Must
#     be on the URL; passing it as an engine kwarg raises TypeError.
#   • prepared_statement_name_func — generates a UUID-based statement name
#     per PREPARE, so names from different client connections can never
#     collide on a shared PgBouncer backend.  Required for Supabase's
#     Transaction Pooler; see the SQLAlchemy asyncpg docs under
#     "Prepared Statement Name with PGBouncer".
_db_url = make_url(settings.database_url).update_query_dict(
    {"prepared_statement_cache_size": "0"}, append=True
)

engine = create_async_engine(
    _db_url,
    poolclass=NullPool,
    echo=not settings.is_production,
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
    },
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def create_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
