"""Database connection and session management."""

from collections.abc import AsyncGenerator

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from loguru import logger

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,  # SELECT 1 验证连接有效后再使用，避免陈旧的断开连接
    pool_recycle=3600,  # 每小时回收连接，防止防火墙/代理超时断开
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""

    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting async database sessions."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except HTTPException:
            # 让 FastAPI HTTPException 直接传播，不拦截到 except Exception
            await session.rollback()
            raise
        except Exception:
            logger.exception("[DB] Session commit failed, rolling back")
            try:
                await session.rollback()
            except Exception as rb_exc:
                logger.warning("[DB] Rollback also failed: {}", rb_exc)
            raise
