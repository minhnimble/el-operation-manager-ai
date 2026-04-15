from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=5,
    echo=not settings.is_production,
    # asyncpg-level: disable client-side prepared statement LRU cache.
    connect_args={"statement_cache_size": 0},
)


# ── PgBouncer / Supabase Transaction Pooler compatibility ─────────────────────
#
# The Transaction Pooler routes each transaction to an arbitrary server
# connection.  Named server-side prepared statements persist on that
# connection and cause DuplicatePreparedStatementError when PgBouncer
# hands the same connection to another client that tries the same name.
#
# AsyncAdapt_asyncpg_connection uses __slots__, so we cannot add new
# attributes — we can only overwrite the ones that already exist as slots.
# Two slots control prepared-statement behaviour:
#
#   _prepared_statement_name_func  → lambda: None
#       SQLAlchemy calls this to get the name for each server-side prepared
#       statement.  Returning None makes asyncpg use an *unnamed* prepared
#       statement (""), which PostgreSQL silently overwrites on each use
#       instead of raising a duplicate-name error.
#
#   _prepared_statement_cache  → _NeverCache() (no-op dict)
#       SQLAlchemy caches PreparedStatement objects by SQL text.  With unnamed
#       statements a cached handle from a previous connection is invalid on the
#       next (PgBouncer may have routed us to a different server connection
#       with no prepared statement at all).  Replacing the cache with a dict
#       that never stores anything forces a fresh prepare() on every query,
#       which is safe and avoids stale-handle errors.


class _NeverCache(dict):
    """Drop-in dict replacement that never stores entries.

    Every lookup misses, so SQLAlchemy always calls asyncpg.prepare() fresh
    rather than reusing a cached PreparedStatement that may no longer exist
    on the current server connection.
    """
    def __setitem__(self, key, value):
        pass  # intentionally discard — never cache


@event.listens_for(engine.sync_engine, "connect")
def _patch_asyncpg_for_pgbouncer(dbapi_connection, _connection_record):
    dbapi_connection._prepared_statement_name_func = lambda: None
    dbapi_connection._prepared_statement_cache = _NeverCache()


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
