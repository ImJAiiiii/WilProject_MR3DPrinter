# backend/latency_api.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from models import LatencyLog

router = APIRouter(prefix="/latency", tags=["latency"])

class DetectUILatencyIn(BaseModel):
    printer_id: str
    ts_src: float               # ts เดิมจาก detector (payload.ts)
    ui_channel: Literal["web","mr"] = "web"
    note: Optional[str] = None

@router.post("/detect-ui")
def log_detect_ui_latency(data: DetectUILatencyIn, db: Session = Depends(get_db)):
    if not data.ts_src or data.ts_src <= 0:
        raise HTTPException(status_code=400, detail="invalid ts_src")

    try:
        t_send = datetime.fromtimestamp(float(data.ts_src), tz=timezone.utc)
        t_recv = datetime.now(timezone.utc)
        latency_ms = max(0.0, (t_recv - t_send).total_seconds() * 1000.0)

        note = f"printer={data.printer_id} {data.note or ''}".strip()

        row = LatencyLog(
            channel=data.ui_channel,
            path="/detect/ui_popup",
            t_send=t_send,
            t_recv=t_recv,
            latency_ms=latency_ms,
            note=note[:255] if note else None,
        )
        db.add(row)
        db.commit()
        return {"ok": True, "latency_ms": latency_ms}
    except Exception:
        db.rollback()
        raise
