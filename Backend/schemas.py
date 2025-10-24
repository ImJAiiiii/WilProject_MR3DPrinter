# backend/schemas.py
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# =========================
# User / Auth
# =========================
class UserOut(BaseModel):
    id: int
    employee_id: str = Field(..., examples=["123456", "1234567"])
    name: Optional[str] = None
    email: Optional[str] = None
    department: Optional[str] = None
    avatar_url: Optional[str] = None
    confirmed: Optional[bool] = None
    last_login_at: Optional[datetime] = None
    can_manage_queue: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class LoginIn(BaseModel):
    employee_id: str = Field(..., examples=["123456", "1234567"])


class RefreshIn(BaseModel):
    # (เผื่ออนาคตมี refresh; ตอนนี้ไม่ได้ใช้)
    refresh_token: str

class RefreshOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    
class LoginOut(BaseModel):
    # ให้ตรงกับ main.py (token เดียว)
    token: str
    token_type: Literal["bearer"] = "bearer"
    user: UserOut
    needs_confirm: bool = False


class UpdateMeIn(BaseModel):
    name: str
    email: Optional[str] = None


# =========================
# Notifications
# =========================
class NotificationOut(BaseModel):
    id: int
    type: str = Field(alias="ntype")
    severity: Literal["info", "success", "warning", "error"] = "info"
    title: str
    message: Optional[str] = None
    data: Optional[dict] = Field(default=None, alias="data_json")
    created_at: datetime
    read: bool = False

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class NotificationCreate(BaseModel):
    type: str
    severity: Literal["info", "success", "warning", "error"] = "info"
    title: str
    message: Optional[str] = None
    data: Optional[dict] = None
    recipients: Optional[List[str]] = None


class NotificationMarkRead(BaseModel):
    ids: List[int]


# =========================
# Printer status
# =========================
class PrinterStatusOut(BaseModel):
    printer_id: str
    display_name: Optional[str] = None
    is_online: bool
    state: Literal["ready", "printing", "paused", "error", "offline", "connecting"]
    status_text: str
    progress: Optional[float] = None
    temp_nozzle: Optional[float] = None
    temp_bed: Optional[float] = None
    updated_at: datetime


class PrinterHeartbeatIn(BaseModel):
    progress: Optional[float] = None
    temp_nozzle: Optional[float] = None
    temp_bed: Optional[float] = None
    status_text: Optional[str] = None


class PrinterStatusUpdateIn(BaseModel):
    state: Optional[str] = None
    status_text: Optional[str] = None
    progress: Optional[float] = None
    temp_nozzle: Optional[float] = None
    temp_bed: Optional[float] = None


# =========================
# Print Queue / Jobs
# =========================
PrintJobStatus = Literal["queued", "processing", "paused", "canceled", "failed", "completed"]
PrintJobSource = Literal["upload", "history", "storage"]


class PrintJobOut(BaseModel):
    id: int
    printer_id: str
    employee_id: str
    # BE เติมให้เพื่อแสดงชื่อเจ้าของงาน
    employee_name: Optional[str] = None

    name: str
    thumb: Optional[str] = None
    time_min: Optional[int] = None
    source: PrintJobSource = "upload"
    status: PrintJobStatus = "queued"
    uploaded_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    octoprint_job_id: Optional[str] = None

    # ให้ FE ใช้ตัดสินใจเปิด/ปิดปุ่ม Cancel ตามสิทธิ์ที่ BE คิดให้แล้ว
    me_can_cancel: bool = False

    # ===== เวลารอ/คงเหลือ (หน่วย: นาที) =====
    # BE จะเติมค่า 2 ตัวหลักนี้ให้
    wait_before_min: Optional[int] = None   # เวลารวมของงานก่อนหน้า (จาก "ตอนนี้")
    wait_total_min: Optional[int] = None    # wait_before_min + ระยะเวลางานนี้ (หรือเวลาที่เหลือถ้ากำลังพิมพ์)

    # 👉 เวลาที่เหลือของ “งานนี้เอง”
    #   - processing: time_min - elapsed (floor เป็นนาที, ไม่น้อยกว่า 0)
    #   - queued/paused: time_min (ยังไม่เริ่ม)
    remaining_min: Optional[int] = None

    # ===== Alias สำหรับ FE บางหน้า =====
    # บางหน้าอ่าน waiting_min หรือ waitingTimeMin → map ให้ชัวร์
    waiting_min: Optional[int] = None
    waitingTimeMin: Optional[int] = None

    @model_validator(mode="after")
    def _fill_wait_aliases(self):
        """
        ออโต้แมพค่า:
        - ถ้า waiting_min / waitingTimeMin ยังว่าง → ใส่ค่าจาก wait_total_min
        - ถ้า wait_total_min ว่างแต่ alias ใด ๆ มีค่า → ย้อนแมพกลับ
        (หมายเหตุ: remaining_min แยกอิสระ ไม่ยุ่งกับ alias)
        """
        total = self.wait_total_min
        alias_compact = self.waiting_min
        alias_camel = self.waitingTimeMin

        # primary -> aliases
        if total is not None:
            if alias_compact is None:
                self.waiting_min = total
            if alias_camel is None:
                self.waitingTimeMin = total
        # alias -> primary
        elif alias_compact is not None:
            self.wait_total_min = alias_compact
            if alias_camel is None:
                self.waitingTimeMin = alias_compact
        elif alias_camel is not None:
            self.wait_total_min = alias_camel
            if alias_compact is None:
                self.waiting_min = alias_camel

        return self

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class PrintJobCreate(BaseModel):
    """
    ใช้ตอนกด Confirm พิมพ์
    - ถ้าเก็บไฟล์บน S3/MinIO ให้ส่ง gcode_key (object_key)
    - ถ้ายังใช้ไฟล์บนดิสก์ ให้ส่ง gcode_path แทน
    - ถ้าไฟล์ต้นฉบับอยู่ใน staging/ ให้ส่ง original_key เพื่อ finalize → storage
    """
    name: str
    thumb: Optional[str] = None
    time_min: Optional[int] = None
    source: PrintJobSource = "upload"
    gcode_key: Optional[str] = None
    gcode_path: Optional[str] = None
    original_key: Optional[str] = None


class PrintJobPatch(BaseModel):
    name: Optional[str] = None
    status: Optional[PrintJobStatus] = None


class QueueReorderIn(BaseModel):
    job_ids: List[int]


class QueueListOut(BaseModel):
    printer_id: str
    items: List[PrintJobOut]


class CurrentJobOut(BaseModel):
    queue_number: int = Field(..., alias="queueNumber")
    file_name: str = Field(..., alias="fileName")
    thumbnail_url: Optional[str] = Field(None, alias="thumbnailUrl")
    job_id: int = Field(..., alias="jobId")
    status: PrintJobStatus
    started_at: Optional[datetime] = Field(None, alias="startedAt")
    time_min: Optional[int] = Field(None, alias="timeMin")

    # 👉 ใหม่: เวลาที่เหลือของงานปัจจุบัน (นาที)
    remaining_min: Optional[int] = Field(None, alias="remainingMin")

    model_config = ConfigDict(populate_by_name=True)


# =========================
# Custom Storage
# =========================
class StorageUploaderOut(BaseModel):
    employee_id: str
    name: Optional[str] = None
    email: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class StorageUploadRequestIn(BaseModel):
    filename: str
    content_type: Optional[str] = None
    size: Optional[int] = None


class StorageUploadRequestOut(BaseModel):
    object_key: str
    method: str = "PUT"
    url: str
    headers: Dict[str, str] = Field(default_factory=dict)
    expires_in: int


class StorageUploadCompleteIn(BaseModel):
    object_key: str
    filename: str
    content_type: Optional[str] = None
    size: Optional[int] = None


class StorageFileOut(BaseModel):
    id: int
    filename: str
    object_key: str
    content_type: Optional[str] = None
    size: Optional[int] = None
    uploaded_at: datetime
    url: Optional[str] = None
    uploader: Optional[StorageUploaderOut] = None

    model_config = ConfigDict(from_attributes=True)


# =========================
# Slicer
# =========================
SupportType = Literal[
    "none",
    # synonyms (รองรับค่าเก่า)
    "touching",
    "all",
    "build_plate_only",
    "everywhere",
    "enforcers_only",
]


class PlacementIn(BaseModel):
    rotate_z: Optional[float] = 0.0
    scale: Optional[float] = 1.0


class SlicerParams(BaseModel):
    object_key: str
    filename: Optional[str] = None
    content_type: Optional[str] = None  # 'model/stl' หรือ 'text/x.gcode'
    job_name: str
    model: str

    # slice params (ใช้เมื่อ origin_ext = 'stl')
    infill: Optional[int] = None
    walls: Optional[int] = None
    support: Optional[SupportType] = None
    layer_height: Optional[float] = None
    nozzle: Optional[float] = None

    placement: Optional[PlacementIn] = None
    origin_ext: Literal["stl", "gcode"] = "stl"


class SlicerAppliedOut(BaseModel):
    nozzle_mm: Optional[float] = None
    layer_height_mm: Optional[float] = None
    fill_density: Optional[float] = None
    perimeters: Optional[int] = None
    support: Optional[SupportType] = None


class SlicerResultOut(BaseModel):
    total_text: Optional[str] = None      # ตัวอย่าง "1h 13m"
    estimate_min: Optional[int] = None    # นาที (int)
    filament_g: Optional[float] = None
    first_layer: Optional[str] = None
    applied: Optional[SlicerAppliedOut] = None


class SlicerPrepareOut(BaseModel):
    is_gcode: bool

    # ที่อยู่ไฟล์ G-code
    gcode_key: Optional[str] = None
    gcode_id: Optional[str] = None
    gcode_url: Optional[str] = None

    # รูปตัวอย่าง
    snapshotUrl: Optional[str] = None
    preview_image_url: Optional[str] = None

    # ค่าแสดงผล/เปรียบเทียบ
    settings: Optional[Dict[str, object]] = None

    # ผลหลังสไลซ์จริงทั้งหมด
    result: SlicerResultOut

    # backward-compat fields (บางหน้า FE อาจยังอ่านอยู่)
    estimate_min: Optional[int] = None
    filament_g: Optional[float] = None
    printer_preset: Optional[str] = None
    gcode_storage_id: Optional[int] = None

    model_config = ConfigDict(populate_by_name=True)
