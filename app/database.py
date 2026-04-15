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
# AsyncSessionLocal() context and closes it immediately on exit.  This has
# several advantages over a conventional pool in this environment:
#
#   • No stale connections — PgBouncer aggressively drops idle server-side
#     connections; a SQLAlchemy pool would hand out those dead connections
#     and produce InterfaceError on the first query.
#
#   • No DuplicatePreparedStatementError — named server-side prepared
#     statements only conflict when the same physical server connection is
#     reused by different clients.  With NullPool every session gets a brand-
#     new connection, so there is nothing to conflict with.
#
#   • No cross-event-loop issues — Streamlit reruns pages in the same process
#     but different asyncio tasks; pooled asyncpg connections are tied to the
#     loop that created them and can raise errors when reused across tasks.
#
# statement_cache_size=0 is kept as a belt-and-suspenders measure: it tells
# asyncpg not to maintain its own client-side prepared-statement LRU cache,
# so even if a connection is somehow reused it will not attempt to reuse a
# cached handle that no longer exists on the server.
engine = create_async_engine(
    settings.database_url,
    poolclass=NullPool,
    echo=not settings.is_production,
    connect_args={"statement_cache_size": 0},
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
