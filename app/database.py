from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

# ── Prepared-statement safety for Supabase Transaction Pooler ─────────────────
#
# Supabase's Transaction Pooler (PgBouncer, pool_mode=transaction) routes each
# transaction to an arbitrary server connection.  Named server-side prepared
# statements survive on a connection after the transaction ends, so when
# PgBouncer hands that connection to a different client the duplicate name
# causes DuplicatePreparedStatementError.
#
# Two independent caches must both be zeroed:
#
#   connect_args  statement_cache_size=0
#       → asyncpg's own client-side LRU cache.  With the cache disabled,
#         asyncpg no longer remembers prepared statement handles between calls.
#
#   prepared_statement_cache_size=0   (SQLAlchemy dialect kwarg)
#       → SQLAlchemy's asyncpg adapter cache.  When this is 0, the adapter
#         passes name=None to asyncpg's prepare(), which tells PostgreSQL to
#         use an *unnamed* prepared statement.  Unnamed statements are
#         overwritten on re-use instead of raising a duplicate-name error.
#
# Both settings are safe for direct (non-pooler) connections: unnamed prepared
# statements perform identically to named ones; the only trade-off is a minor
# reduction in client-side caching efficiency.
engine = create_async_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=5,
    echo=not settings.is_production,
    connect_args={"statement_cache_size": 0},
    prepared_statement_cache_size=0,
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
