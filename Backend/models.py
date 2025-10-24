# backend/models.py
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Text, ForeignKey, Float, Index
)
from sqlalchemy.orm import relationship
from datetime import datetime

from db import Base


# ========== Users ==========
class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    # เก็บ "เลขล้วน" 6–7 หลัก เป็นรหัสพนักงาน
    employee_id   = Column(String, unique=True, index=True, nullable=False)

    name          = Column(String, nullable=True)
    email         = Column(String, nullable=True)
    department    = Column(String, nullable=True)
    avatar_url    = Column(String, nullable=True)

    # ใช้บอกว่าเคย "ยืนยันข้อมูลครั้งแรก" แล้วหรือยัง
    confirmed     = Column(Boolean, default=False, nullable=False)
    last_login_at = Column(DateTime, nullable=True)

    # ✅ บัญชีพิเศษ: ยกเลิก/จัดคิวของผู้อื่นได้
    can_manage_queue = Column(Boolean, default=False, nullable=False)

    # ความสัมพันธ์กับ NotificationTarget (optional)
    notifications = relationship(
        "NotificationTarget",
        primaryjoin="User.employee_id==foreign(NotificationTarget.employee_id)",
        viewonly=True,
        lazy="noload",
    )


# ========== Notifications ==========
class Notification(Base):
    """ตัวแจ้งเตือน (เนื้อหากลาง 1 รายการ) — ผูกผู้รับผ่าน NotificationTarget"""
    __tablename__ = "notifications"

    id         = Column(Integer, primary_key=True)
    ntype      = Column(String,  nullable=False)                   # เช่น print.completed
    severity   = Column(String,  nullable=False, default="info")   # info|success|warning|error
    title      = Column(String,  nullable=False)
    message    = Column(Text,    nullable=True)
    data_json  = Column(Text,    nullable=True)                    # เก็บ JSON string
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    targets = relationship(
        "NotificationTarget", back_populates="notification", cascade="all, delete-orphan"
    )


class NotificationTarget(Base):
    """การกระจายแจ้งเตือนไปยังผู้รับ (ต่อ 1 ผู้รับ = 1 แถว)"""
    __tablename__ = "notification_targets"

    id              = Column(Integer, primary_key=True)
    notification_id = Column(Integer, ForeignKey("notifications.id", ondelete="CASCADE"),
                              index=True, nullable=False)
    employee_id     = Column(String, index=True, nullable=False)   # อ้างอิง user ด้วย employee_id
    read_at         = Column(DateTime, nullable=True)

    notification = relationship("Notification", back_populates="targets")


# ========== Printers ==========
class Printer(Base):
    __tablename__ = "printers"

    # ใช้ slug เป็นไอดี เช่น "prusa-core-one"
    id            = Column(String, primary_key=True, index=True)
    display_name  = Column(String, nullable=True)

    # สถานะหลัก
    state         = Column(String, default="ready")   # ready|printing|paused|error|offline|connecting
    status_text   = Column(String, default="Printer is ready")
    busy          = Column(Boolean, default=False)

    # ค่าที่ชอบโชว์
    progress      = Column(Float, nullable=True)      # 0..100
    temp_nozzle   = Column(Float, nullable=True)
    temp_bed      = Column(Float, nullable=True)

    # การวัด online จาก heartbeat
    last_heartbeat_at = Column(DateTime, nullable=True)

    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ✅ ความสัมพันธ์คิวพิมพ์
    jobs = relationship(
        "PrintJob", back_populates="printer", cascade="all, delete-orphan", passive_deletes=True
    )


# ========== Print Queue / Jobs ==========
class PrintJob(Base):
    """
    งานพิมพ์หนึ่งรายการในคิวของเครื่อง (ยังไม่ผูก OctoPrint จริง แต่เตรียมฟิลด์ไว้แล้ว)
    """
    __tablename__ = "print_jobs"

    id          = Column(Integer, primary_key=True)
    printer_id  = Column(String, ForeignKey("printers.id", ondelete="CASCADE"), index=True, nullable=False)

    # เจ้าของงาน (อิง employee_id)
    employee_id = Column(String, index=True, nullable=False)

    # ข้อมูลชิ้นงาน
    name        = Column(String, nullable=False)           # ชื่อไฟล์/โมเดล
    thumb       = Column(String, nullable=True)            # path/URL preview (optional)
    time_min    = Column(Integer, nullable=True)           # เวลาประมาณ (นาที) ใช้คำนวณคิว

    # แหล่งที่มา: upload | history | storage
    source      = Column(String, default="upload", nullable=False)

    # สถานะคิว
    status      = Column(String, default="queued", nullable=False)
    # queued | processing | paused | canceled | failed | completed

    # ไทม์สแตมป์
    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at  = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    # ฟิลด์เตรียมไว้ต่อ OctoPrint ภายหลัง
    octoprint_job_id = Column(String, nullable=True)       # ไอดีงานใน OctoPrint (ถ้ามี)
    gcode_path       = Column(String, nullable=True)       # ที่อยู่ไฟล์บน storage (ถ้ามี)

    printer     = relationship("Printer", back_populates="jobs")

# ดัชนีช่วย query เร็วขึ้น
Index("ix_print_jobs_printer_status", PrintJob.printer_id, PrintJob.status)
Index("ix_print_jobs_uploaded", PrintJob.printer_id, PrintJob.uploaded_at)


# ========== Custom Storage (S3 / MinIO) ==========
class StorageFile(Base):
    """
    เมทาดาต้าไฟล์ที่เก็บจริงบน S3/MinIO (อัปโหลด/ดาวน์โหลดด้วย Presigned URL)
    """
    __tablename__ = "storage_files"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    employee_id  = Column(String, index=True, nullable=False)   # ผู้ที่อัปโหลด (อิง employee_id)

    filename     = Column(String(255), nullable=False)          # ชื่อไฟล์ที่แสดง
    object_key   = Column(String(512), nullable=False)          # key ใน S3 เช่น storage/2025/09/02/uuid.gcode
    content_type = Column(String(128), nullable=True)
    size         = Column(Integer, nullable=True)               # bytes
    etag         = Column(String(64), nullable=True)            # จาก S3 (HeadObject)

    uploaded_at  = Column(DateTime, default=datetime.utcnow, nullable=False)

# ดัชนีที่ใช้บ่อย (ค้นหาตามคน/เวลา)
Index("ix_storage_files_emp_uploaded", StorageFile.employee_id, StorageFile.uploaded_at)
