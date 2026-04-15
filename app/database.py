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
# Two things must be zeroed out — they live at different layers:
#
#   asyncpg  (connect_args statement_cache_size=0, above)
#     → stops asyncpg from caching PreparedStatement handles client-side.
#
#   SQLAlchemy asyncpg adapter  (_prepared_statement_cache_size / name func)
#     → SQLAlchemy generates its own named statements independently.
#       Setting _prepared_statement_cache_size=0 makes its adapter call
#       asyncpg.prepare(name=None), which PostgreSQL treats as an *unnamed*
#       prepared statement.  Unnamed statements are silently overwritten on
#       each use instead of raising a duplicate-name error.
#
# create_async_engine() does not accept prepared_statement_cache_size as a
# kwarg in all 2.0.x builds, so we patch each connection via an event.
@event.listens_for(engine.sync_engine, "connect")
def _patch_asyncpg_for_pgbouncer(dbapi_connection, _connection_record):
    # Zero the SQLAlchemy-layer cache size so the name function is consulted
    # on every statement execution (no SQLAlchemy-level reuse).
    dbapi_connection._prepared_statement_cache_size = 0
    # Return None → asyncpg calls prepare(name=None) → unnamed statement.
    dbapi_connection._prepared_statement_name_func = lambda: None


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
