# backend/models.py
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    Float,
    Index,
    UniqueConstraint,
    CheckConstraint,
    event,  # NEW
)
from sqlalchemy.orm import relationship

from db import Base


# ----------------------------- helpers -----------------------------

def _json_loads(s: Optional[str]) -> Optional[Dict[str, Any]]:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _json_dumps(obj: Optional[Dict[str, Any]]) -> Optional[str]:
    if obj is None:
        return None
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return None


# ============================ Users ============================

class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("employee_id", name="uq_users_employee_id"),
        Index("ix_users_employee", "employee_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    # เก็บ "เลขล้วน" 6–7 หลัก เป็นรหัสพนักงาน
    employee_id = Column(String, unique=True, index=True, nullable=False)

    name = Column(String, nullable=True)
    email = Column(String, nullable=True)
    department = Column(String, nullable=True)
    avatar_url = Column(String, nullable=True)

    # เคยยืนยันข้อมูลครั้งแรกแล้วหรือยัง
    confirmed = Column(Boolean, default=False, nullable=False)
    last_login_at = Column(DateTime, nullable=True)

    # บัญชีพิเศษ: จัดคิว/ยกเลิกของผู้อื่นได้
    can_manage_queue = Column(Boolean, default=False, nullable=False)

    # ความสัมพันธ์ (viewonly กันเขียนพลาด)
    notifications = relationship(
        "NotificationTarget",
        primaryjoin="User.employee_id==foreign(NotificationTarget.employee_id)",
        viewonly=True,
        lazy="noload",
    )
    storage_files = relationship(
        "StorageFile",
        primaryjoin="User.employee_id==foreign(StorageFile.employee_id)",
        viewonly=True,
        lazy="noload",
    )
    print_jobs = relationship(
        "PrintJob",
        primaryjoin="User.employee_id==foreign(PrintJob.employee_id)",
        viewonly=True,
        lazy="noload",
    )

    def __repr__(self) -> str:
        return f"<User emp={self.employee_id} name={self.name!r}>"


# ======================== Notifications ========================

class Notification(Base):
    """ตัวแจ้งเตือน (เนื้อหากลาง 1 รายการ) — ผูกผู้รับผ่าน NotificationTarget"""
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True)
    ntype = Column(String, nullable=False)                         # เช่น print.completed
    severity = Column(String, nullable=False, default="info")      # info|success|warning|error
    title = Column(String, nullable=False)
    message = Column(Text, nullable=True)
    data_json = Column(Text, nullable=True)                        # เก็บ JSON string
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    targets = relationship(
        "NotificationTarget", back_populates="notification", cascade="all, delete-orphan"
    )


class NotificationTarget(Base):
    """การกระจายแจ้งเตือนไปยังผู้รับ (ต่อ 1 ผู้รับ = 1 แถว)"""
    __tablename__ = "notification_targets"
    __table_args__ = (Index("ix_notification_targets_emp", "employee_id"),)

    id = Column(Integer, primary_key=True)
    notification_id = Column(
        Integer,
        ForeignKey("notifications.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    employee_id = Column(String, index=True, nullable=False)   # อิง user ด้วย employee_id
    read_at = Column(DateTime, nullable=True)

    notification = relationship("Notification", back_populates="targets")


# ============================ Printers ============================

class Printer(Base):
    __tablename__ = "printers"

    # ใช้ slug เป็นไอดี เช่น "prusa-core-one"
    id = Column(String, primary_key=True, index=True)
    display_name = Column(String, nullable=True)

    # สถานะหลัก
    state = Column(String, default="ready")  # ready|printing|paused|error|offline|connecting
    status_text = Column(String, default="Printer is ready")
    busy = Column(Boolean, default=False)

    # ค่าที่ชอบโชว์
    progress = Column(Float, nullable=True)      # 0..100
    temp_nozzle = Column(Float, nullable=True)
    temp_bed = Column(Float, nullable=True)

    # ใช้คำนวณ online จาก heartbeat
    last_heartbeat_at = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ความสัมพันธ์คิวพิมพ์
    jobs = relationship(
        "PrintJob",
        back_populates="printer",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Printer id={self.id!r} state={self.state!r}>"


# ===================== Print Queue / Jobs ======================

class PrintJob(Base):
    """
    งานพิมพ์หนึ่งรายการในคิวของเครื่อง
    """
    __tablename__ = "print_jobs"
    __table_args__ = (
        Index("ix_print_jobs_printer_status", "printer_id", "status"),
        Index("ix_print_jobs_uploaded", "printer_id", "uploaded_at"),
        Index("ix_print_jobs_owner_uploaded", "employee_id", "uploaded_at"),
        Index("ix_print_jobs_owner_status", "employee_id", "status"),
        CheckConstraint(
            "status in ('queued','processing','paused','canceled','failed','completed')",
            name="ck_print_jobs_status",
        ),
    )

    id = Column(Integer, primary_key=True)

    # ผูกเครื่อง
    printer_id = Column(
        String, ForeignKey("printers.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # เจ้าของงาน (อิง employee_id)
    employee_id = Column(String, index=True, nullable=False)

    # คนที่ "กดพิมพ์" งานนี้ (อาจไม่ใช่เจ้าของไฟล์) — ใช้เลือกผู้รับแจ้งเตือน DM/Email
    requested_by_employee_id = Column(String, index=True, nullable=True)

    # ข้อมูลชิ้นงาน
    name = Column(String, nullable=False)      # ชื่อไฟล์/โมเดล
    thumb = Column(String, nullable=True)      # path/URL preview (optional)
    time_min = Column(Integer, nullable=True)  # เวลาประมาณ (นาที) ใช้คำนวณคิว

    # แหล่งที่มา: upload | history | storage
    source = Column(String, default="upload", nullable=False)

    # สถานะคิว
    status = Column(String, default="queued", nullable=False)
    # queued | processing | paused | canceled | failed | completed

    # ไทม์สแตมป์
    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    # ฟิลด์ต่อ OctoPrint / ไฟล์
    octoprint_job_id = Column(String, nullable=True)  # ไอดีงานใน OctoPrint (ถ้ามี)
    gcode_path = Column(String, nullable=True)        # ที่อยู่ไฟล์บน storage (ถ้ามี)

    # ✅ object_key ของไฟล์ G-code ใน storage
    gcode_key = Column(String(512), nullable=True, index=True)

    # ✅ เก็บพารามิเตอร์และสถิติการพิมพ์ (JSON string)
    template_json = Column(Text, nullable=True)       # เช่น printer/material/nozzle/layer/infill/...
    stats_json = Column(Text, nullable=True)          # เช่น filament_g / time_min / time_text / source
    file_json = Column(Text, nullable=True)           # ชื่อไฟล์/ขนาด/ฯลฯ ตอนอัปโหลด

    # ความสัมพันธ์
    printer = relationship("Printer", back_populates="jobs")
    owner = relationship(
        "User",
        primaryjoin="foreign(PrintJob.employee_id)==User.employee_id",
        viewonly=True,
        lazy="noload",
    )

    # (ออปชัน) ความสัมพันธ์ไปยัง "ผู้กดพิมพ์" เพื่ออ้างอิงชื่อ/อีเมลได้ง่าย
    requester = relationship(
        "User",
        primaryjoin="foreign(PrintJob.requested_by_employee_id)==User.employee_id",
        viewonly=True,
        lazy="noload",
    )

    # view-only reference ไปที่ StorageFile โดยเทียบ object_key (ไม่มี FK จริง)
    storage_ref = relationship(
        "StorageFile",
        primaryjoin="foreign(PrintJob.gcode_key)==StorageFile.object_key",
        viewonly=True,
        lazy="noload",
    )

    # ---------- convenience JSON accessors ----------
    @property
    def template(self) -> Optional[Dict[str, Any]]:
        return _json_loads(self.template_json)

    @template.setter
    def template(self, val: Optional[Dict[str, Any]]) -> None:
        self.template_json = _json_dumps(val)

    @property
    def stats(self) -> Optional[Dict[str, Any]]:
        return _json_loads(self.stats_json)

    @stats.setter
    def stats(self, val: Optional[Dict[str, Any]]) -> None:
        self.stats_json = _json_dumps(val)

    @property
    def file(self) -> Optional[Dict[str, Any]]:
        return _json_loads(self.file_json)

    @file.setter
    def file(self, val: Optional[Dict[str, Any]]) -> None:
        self.file_json = _json_dumps(val)

    def __repr__(self) -> str:
        return f"<PrintJob id={self.id} emp={self.employee_id} status={self.status} name={self.name!r}>"


# =================== Custom Storage (S3 / MinIO) ===================

class StorageFile(Base):
    """
    เมทาดาต้าไฟล์ที่เก็บจริงบน S3/MinIO (อัปโหลด/ดาวน์โหลดด้วย Presigned URL)

    NOTE:
    - filename: ชื่อไฟล์ดั้งเดิม (คงไว้เพื่อเข้ากันได้ย้อนหลัง)
    - name:     ชื่อที่ใช้แสดง/เทียบซ้ำในระบบ (เช่น {Model}_V1.gcode)
    - name_low: ตัวพิมพ์เล็กของ name เพื่อค้น/กันซ้ำเร็ว
    """
    __tablename__ = "storage_files"
    __table_args__ = (
        Index("ix_storage_files_emp_uploaded", "employee_id", "uploaded_at"),
        Index("ix_storage_files_key", "object_key"),
        # ป้องกันสร้างซ้ำ object_key
        UniqueConstraint("object_key", name="uq_storage_files_object_key"),
        # กันชื่อซ้ำต่อผู้ใช้ (ไม่แคร์ตัวพิมพ์)
        UniqueConstraint("employee_id", "name_low", name="uq_storage_files_emp_name_low"),
        Index("ix_storage_files_name_low", "name_low"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(String, index=True, nullable=False)   # ผู้ที่อัปโหลด (อิง employee_id)

    # เดิม
    filename = Column(String(255), nullable=False)

    # ใหม่: ชื่อสำหรับโชว์/ตรวจซ้ำ (ถ้าไม่ได้ระบุ จะ fallback จาก filename อัตโนมัติ)
    name = Column(String(255), nullable=False, default="")      # e.g. MyPart_V3.gcode
    name_low = Column(String(255), nullable=False, default="")  # e.g. mypart_v3.gcode

    object_key = Column(String(512), nullable=False)            # key ใน S3 เช่น storage/2025/09/02/uuid.gcode
    content_type = Column(String(128), nullable=True)
    size = Column(Integer, nullable=True)                       # bytes
    etag = Column(String(64), nullable=True)                    # จาก S3 (HeadObject)

    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    uploader = relationship(
        "User",
        primaryjoin="foreign(StorageFile.employee_id)==User.employee_id",
        viewonly=True,
        lazy="noload",
    )

    def __repr__(self) -> str:
        return f"<StorageFile id={self.id} key={self.object_key!r} emp={self.employee_id} name={self.name!r}>"


# ---------- sync hooks: ทำให้ name/name_low สอดคล้อง และไม่ทำของเก่าพัง ----------

@event.listens_for(StorageFile, "before_insert")
def _sf_before_insert(mapper, connection, target: StorageFile):
    # ถ้า caller ยังไม่ได้ตั้ง name ให้ใช้ filename เป็นค่าเริ่มต้น
    if not (target.name and target.name.strip()):
        target.name = (target.filename or "").strip()
    target.name_low = (target.name or "").strip().lower()

@event.listens_for(StorageFile, "before_update")
def _sf_before_update(mapper, connection, target: StorageFile):
    # ถ้ามีการแก้ name หรือ filename ให้คงความสอดคล้อง
    if not (target.name and target.name.strip()):
        target.name = (target.filename or "").strip()
    target.name_low = (target.name or "").strip().lower()
