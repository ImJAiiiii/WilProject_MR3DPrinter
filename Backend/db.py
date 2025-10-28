# backend/db.py
from __future__ import annotations

import os
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import declarative_base   # ★ เพิ่มบรรทัดนี้

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./users.db").strip()

# ★ ประกาศ Base กลางให้ทุก model ใช้ร่วมกัน
Base = declarative_base()

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
        future=True,
    )
else:
    pool_size = int(os.getenv("DB_POOL_SIZE", "5"))
    max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    pool_recycle = int(os.getenv("DB_POOL_RECYCLE", "1800"))

    engine = create_engine(
        DATABASE_URL,
        future=True,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=pool_recycle,
    )

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    bind=engine,
    future=True,
)

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:
            pass
