# latency.py
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from datetime import datetime

from db import get_db
import models, schemas

router = APIRouter(
    prefix="/latency",
    tags=["latency"],
)

@router.post("/log", response_model=schemas.LatencyLogOut)
def log_latency(payload: schemas.LatencyLogIn, db: Session = Depends(get_db)):
    obj = models.LatencyLog(
        channel=payload.channel,
        path=payload.path,
        t_send=payload.t_send,
        t_recv=payload.t_recv,
        latency_ms=payload.latency_ms,
        note=payload.note,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


# ถ้าอยากดูคร่าว ๆ ก็ทำ endpoint list ง่าย ๆ เพิ่มได้ เช่น
@router.get("/recent", response_model=list[schemas.LatencyLogOut])
def recent_latency(limit: int = 100, db: Session = Depends(get_db)):
    q = (
        db.query(models.LatencyLog)
        .order_by(models.LatencyLog.id.desc())
        .limit(limit)
    )
    return list(q)
