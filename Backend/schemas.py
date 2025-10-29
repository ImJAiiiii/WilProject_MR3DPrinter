# backend/schemas.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

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

    # ฟิลด์ที่ FE ใช้เช็คสิทธิ์
    can_manage_queue: Optional[bool] = None
    is_manager: Optional[bool] = None
    role: Optional[str] = None

    # เผื่อใช้ร่วมกับ token version
    token_version: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class LoginIn(BaseModel):
    employee_id: str = Field(..., examples=["123456", "1234567"])


class RefreshIn(BaseModel):
    refresh_token: str


class RefreshOut(BaseModel):
    access_token: str
    # ถ้าทำ refresh rotation จะส่ง refresh_token ตัวใหม่กลับมาด้วย
    refresh_token: Optional[str] = None
    token_type: Literal["bearer"] = "bearer"


# ให้ตรงกับ API: login คืน access_token + refresh_token
# และรองรับ FE เก่าที่อ่าน field ชื่อ token
class LoginOut(BaseModel):
    access_token: str
    refresh_token: str
    # ✅ เพิ่มเพื่อความเข้ากันได้ย้อนหลัง (FE เก่าอ่าน token)
    token: Optional[str] = None
    token_type: Literal["bearer"] = "bearer"
    user: UserOut
    needs_confirm: bool = False

    model_config = ConfigDict(populate_by_name=True)


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
    # BE เก็บเป็น TEXT (JSON string) — แปลงเป็น dict ให้ FE ใช้สะดวก
    data: Optional[dict] = Field(default=None, alias="data_json")
    created_at: datetime
    read: bool = False

    @model_validator(mode="after")
    def _decode_json_fields(self):
        import json
        if isinstance(self.data, str):
            try:
                self.data = json.loads(self.data)
            except Exception:
                # ปล่อยเป็น string ถ้า parse ไม่ได้ (กันข้อมูลเดิม)
                pass
        return self

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
# 👇 เพิ่ม "octoprint" ให้รองรับค่าในฐานข้อมูล
PrintJobSource = Literal["upload", "history", "storage", "octoprint"]


class PrintJobTemplate(BaseModel):
    profile: Optional[str] = None
    model: Optional[str] = None
    printer: Optional[str] = None
    material: Optional[str] = None
    material_brand: Optional[str] = None
    material_color: Optional[str] = None
    nozzle: Optional[float] = None        # mm
    layer: Optional[float] = None         # mm
    infill: Optional[float] = None        # %
    wallLoops: Optional[int] = None
    supports: Optional[bool] = None
    sparseInfillDensity: Optional[float] = None

    model_config = ConfigDict(extra="allow")


class PrintJobStats(BaseModel):
    filament_g: Optional[float] = None
    time_min: Optional[int] = None
    time_text: Optional[str] = None       # e.g. "1h 13m"
    time_source: Optional[str] = None     # "stored" | "estimated" | others

    model_config = ConfigDict(extra="allow")


class PrintJobFileMeta(BaseModel):
    filename: Optional[str] = None
    size: Optional[int] = None
    sizeMB: Optional[float] = None
    preview: Optional[str] = None
    thumb: Optional[str] = None

    model_config = ConfigDict(extra="allow")


_ALLOWED_SOURCES = {"upload", "history", "storage", "octoprint"}


class PrintJobOut(BaseModel):
    id: int
    printer_id: str
    employee_id: str
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

    # ฟิลด์ใหม่ (decode อัตโนมัติถ้ามาจาก JSON string)
    template: Optional[PrintJobTemplate] = None
    stats: Optional[PrintJobStats] = None
    file: Optional[PrintJobFileMeta] = None

    # FE ใช้ตัดสินใจปุ่ม Cancel
    me_can_cancel: bool = False

    # เวลารอ/คงเหลือ (นาที)
    wait_before_min: Optional[int] = None
    wait_total_min: Optional[int] = None
    remaining_min: Optional[int] = None

    # aliases เพื่อความเข้ากันได้กับโค้ดเก่า
    waiting_min: Optional[int] = None
    waitingTimeMin: Optional[int] = None

    # ✅ กันค่าจาก DB ที่เป็น string แปลก ๆ ก่อนเข้า Literal
    @model_validator(mode="before")
    @classmethod
    def _normalize_source_before(cls, values):
        if isinstance(values, dict):
            src = values.get("source")
            if src and src not in _ALLOWED_SOURCES:
                # map ค่าไม่รู้จักให้ปลอดภัย (เลือก history เป็นกลาง ๆ)
                values["source"] = "history"
        return values

    @model_validator(mode="after")
    def _fill_wait_aliases(self):
        total = self.wait_total_min
        alias_compact = self.waiting_min
        alias_camel = self.waitingTimeMin

        if total is not None:
            if alias_compact is None:
                self.waiting_min = total
            if alias_camel is None:
                self.waitingTimeMin = total
        elif alias_compact is not None:
            self.wait_total_min = alias_compact
            if alias_camel is None:
                self.waitingTimeMin = alias_compact
        elif alias_camel is not None:
            self.wait_total_min = alias_camel
            if alias_compact is None:
                self.waiting_min = alias_camel
        return self

    @model_validator(mode="after")
    def _decode_embedded_json(self):
        """
        หากมาจาก ORM แล้วฟิลด์ template_json/stats_json/file_json ถูกแมปมาเป็น string
        ให้แปลงเป็น object ของ Pydantic ให้เอง
        """
        import json
        if isinstance(self.template, str):
            try:
                self.template = PrintJobTemplate(**json.loads(self.template))
            except Exception:
                self.template = None
        if isinstance(self.stats, str):
            try:
                self.stats = PrintJobStats(**json.loads(self.stats))
            except Exception:
                self.stats = None
        if isinstance(self.file, str):
            try:
                self.file = PrintJobFileMeta(**json.loads(self.file))
            except Exception:
                self.file = None
        return self

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class PrintJobCreate(BaseModel):
    """
    ใช้ตอนกด Confirm พิมพ์
    - ถ้าเก็บไฟล์บน S3/MinIO ให้ส่ง gcode_key (object_key)
    - ถ้ายังใช้ไฟล์บนดิสก์ ให้ส่ง gcode_path แทน
    - ถ้าไฟล์ต้นฉบับอยู่ใน staging/ ให้ส่ง original_key เพื่อ finalize → storage
    - เพิ่ม: template / stats / file เพื่อเก็บ history ให้ครบ
    """
    name: str
    thumb: Optional[str] = None
    time_min: Optional[int] = None
    source: PrintJobSource = "upload"

    gcode_key: Optional[str] = None
    gcode_path: Optional[str] = None
    original_key: Optional[str] = None

    # ใหม่
    template: Optional[Dict[str, Any]] = None
    stats: Optional[Dict[str, Any]] = None
    file: Optional[Dict[str, Any]] = None

    # ✅ กัน input แปลกก่อน (เช่น FE ส่ง source ผิด)
    @model_validator(mode="before")
    @classmethod
    def _normalize_source_before(cls, values):
        if isinstance(values, dict):
            src = values.get("source")
            if src and src not in _ALLOWED_SOURCES:
                values["source"] = "history"
        return values

    @model_validator(mode="after")
    def _derive_time_min(self):
        """ถ้าไม่ได้ส่ง time_min มา แต่ stats.time_min มีค่า → ใช้ค่านั้น"""
        if self.time_min is None and isinstance(self.stats, dict):
            tm = self.stats.get("time_min")
            try:
                if tm is not None:
                    self.time_min = int(tm)
            except Exception:
                pass
        return self


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
    remaining_min: Optional[int] = Field(None, alias="remainingMin")

    model_config = ConfigDict(populate_by_name=True)


# =========================
# Custom Storage
# =========================
class StorageUploaderOut(BaseModel):
    # ปล่อยเป็น Optional เพื่อกันข้อมูลเก่า/ขาด
    employee_id: Optional[str] = None
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


# ⬇️ ใช้ตอน FE เรียก /storage/upload/complete
# เพิ่ม auto_finalize/model/target เพื่อ "อัปแล้วเข้า catalog พร้อมพรีวิว/แมนิเฟสต์" ทันที
class StorageUploadCompleteIn(BaseModel):
    object_key: str
    filename: str
    content_type: Optional[str] = None
    size: Optional[int] = None

    # ใหม่
    auto_finalize: bool = False
    model: Optional[str] = None
    target: Literal["catalog", "storage"] = "catalog"


# ⬇️ ใช้ตอน backend เรียก finalize_to_storage(...) โดยตรงระหว่างคิว/เวิร์กโฟลว์
class FinalizeIn(BaseModel):
    object_key: str                 # คีย์ใน staging/*
    filename: str                   # ชื่อไฟล์ต้นฉบับ (.stl หรือ .gcode)
    content_type: Optional[str] = None
    size: Optional[int] = None
    model: str                      # เช่น "Delta"
    target: Literal["catalog", "storage"] = "catalog"


class StorageFileOut(BaseModel):
    id: int
    # ชื่อไฟล์ดั้งเดิม (อาจไม่ซ้ำ)
    filename: str
    # ชื่อ “ใช้งานจริง” ในระบบ (บังคับ uniqueness ผ่าน name_low)
    name: Optional[str] = None
    # ซ่อน name_low จากการส่งออก
    name_low: Optional[str] = Field(default=None, exclude=True)

    object_key: str
    content_type: Optional[str] = None
    size: Optional[int] = None
    uploaded_at: datetime
    url: Optional[str] = None
    uploader: Optional[StorageUploaderOut] = None

    model_config = ConfigDict(from_attributes=True)


# ====== ชุดสคีมาช่วย “ตรวจชื่อ / ค้นชื่อ” ======
class StorageValidateNameIn(BaseModel):
    """
    ใช้กับ POST /storage/validate-name
    """
    name: str                           # ชื่อที่ผู้ใช้กรอก (ไม่ต้องมีนามสกุลก็ได้)
    ext: Optional[str] = "gcode"        # นามสกุลคาดหวัง (เติมให้อัตโนมัติ)
    require_pattern: bool = True        # บังคับแพทเทิร์น NAME_VN หรือไม่


class StorageValidateNameOut(BaseModel):
    ok: bool                            # true = ใช้ได้, false = ห้ามใช้
    reason: Optional[str] = None        # invalid_format | duplicate | None
    normalized: Optional[str] = None    # ชื่อหลัง normalize + ต่อ .ext แล้ว
    exists: bool = False
    suggestions: List[str] = Field(default_factory=list)


class StorageSearchNamesOut(BaseModel):
    """
    ใช้กับ GET /storage/search-names?q=...
    """
    items: List[str] = Field(default_factory=list)


# =========================
# Slicer
# =========================
SupportType = Literal[
    "none",
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
    content_type: Optional[str] = None
    job_name: str
    model: str

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
    total_text: Optional[str] = None
    estimate_min: Optional[int] = None
    filament_g: Optional[float] = None
    first_layer: Optional[str] = None
    applied: Optional[SlicerAppliedOut] = None


class SlicerPrepareOut(BaseModel):
    is_gcode: bool

    gcode_key: Optional[str] = None
    gcode_id: Optional[str] = None
    gcode_url: Optional[str] = None

    snapshotUrl: Optional[str] = None
    preview_image_url: Optional[str] = None

    settings: Optional[Dict[str, object]] = None

    result: SlicerResultOut

    estimate_min: Optional[int] = None
    filament_g: Optional[float] = None
    printer_preset: Optional[str] = None
    gcode_storage_id: Optional[int] = None

    model_config = ConfigDict(populate_by_name=True)


# =========================
# Print History (NEW)
# =========================
class HistoryItemOut(BaseModel):
    id: int
    employee_id: str
    name: str
    thumb: Optional[str] = None
    time_min: Optional[int] = None
    status: str
    uploaded_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    gcode_path: Optional[str] = None
    gcode_key: Optional[str] = None


class HistoryListOut(BaseModel):
    items: List[HistoryItemOut]


class HistoryMergeItemIn(BaseModel):
    # payload จาก FE (localStorage) เพื่อ migrate ขึ้น server
    name: str
    thumb: Optional[str] = None
    stats: Optional[dict] = None
    template: Optional[dict] = None
    gcode_key: Optional[str] = None
    gcode_path: Optional[str] = None
    original_key: Optional[str] = None
    uploadedAt: Optional[datetime] = None
    source: Optional[str] = None


class HistoryMergeIn(BaseModel):
    items: List[HistoryMergeItemIn]
