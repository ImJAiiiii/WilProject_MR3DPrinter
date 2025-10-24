# db.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import NullPool, QueuePool

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./users.db")
url = make_url(DATABASE_URL)

if url.get_backend_name() == "sqlite":
    # ไม่มี pool สำหรับ SQLite เพื่อลด detached/timeout ปัญหา multi-req
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
        future=True,
    )
else:
    # ปรับ pool ได้ตามสภาพแวดล้อมจริง (Postgres/MySQL)
    engine = create_engine(
        DATABASE_URL,
        poolclass=QueuePool,
        pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
        pool_pre_ping=True,
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
        future=True,
    )

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,  # ลดโอกาสต้องใช้ connection หลัง commit
    bind=engine,
)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
