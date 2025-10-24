# backend/slicer.py
from __future__ import annotations
import os, re, uuid, subprocess, base64
from typing import Optional, Dict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from auth import get_confirmed_user
from models import User
from s3util import download_to_file, upload_bytes, presign_get, new_staging_key

router = APIRouter(prefix="/api/slicer", tags=["slicer"])

# --- env / presets ---
PRUSA_SLICER_BIN = os.getenv(
    "PRUSA_SLICER_BIN",
    r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer-console.exe"
)
PRUSA_DATADIR = os.getenv(
    "PRUSA_DATADIR",
    os.path.join(os.getenv("APPDATA", r"C:\Users\Public"), "PrusaSlicer")
)

PRUSA_PRINTER_PRESET  = os.getenv("PRUSA_PRINTER_PRESET")
PRUSA_PRINT_PRESET    = os.getenv("PRUSA_PRINT_PRESET")
PRUSA_FILAMENT_PRESET = os.getenv("PRUSA_FILAMENT_PRESET")

# เปิด/ปิดการฝัง thumbnail (บางเวอร์ชันเก่าอาจไม่รู้จัก flag --thumbnail)
PRUSA_ENABLE_THUMBNAIL = os.getenv("PRUSA_ENABLE_THUMBNAIL", "1").lower() not in ("0", "false", "no")

WORK_DIR = Path(os.getenv("UPLOAD_DIR", "uploads")) / "_work_slicer"
WORK_DIR.mkdir(parents=True, exist_ok=True)


# ---------- schemas ----------
class PreviewIn(BaseModel):
    fileId: str                 # S3 object key (staging/...stl หรือ staging/...gcode)
    originExt: str              # 'stl' | 'gcode' | ...
    jobName: str
    model: str
    slicing: Optional[Dict] = None   # {infill, walls, support}

class PreviewOut(BaseModel):
    snapshotUrl: Optional[str] = None
    gcodeUrl: Optional[str] = None          # presigned GET สำหรับ viewer
    printer: Optional[str] = None
    settings: Optional[Dict] = None
    result: Optional[Dict] = None
    gcodeKey: str
    originalKey: str
    estimateMin: Optional[int] = None
    isGcode: bool


# ---------- helpers ----------
_time_re = re.compile(r'(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?', re.I)
THUMBS_RE = re.compile(r';\s*thumbnail begin (\d+)x(\d+)\s+\d+\s*\n(.*?)\n;\s*thumbnail end', re.I | re.S)

def _parse_time_to_min(txt: Optional[str]) -> Optional[int]:
    if not txt:
        return None
    m = _time_re.search(txt.strip())
    if not m:
        return None
    h = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    return h * 60 + mi

def _read_metrics_from_gcode(path: Path) -> Dict:
    fil_g = None
    total = None
    first = None
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if fil_g is None:
                    m = re.search(r";\s*filament used \[g\]\s*=\s*([0-9.]+)", line, re.I)
                    if m: fil_g = float(m.group(1))
                if total is None:
                    m = re.search(r";\s*estimated printing time .*=\s*(.+)$", line, re.I)
                    if m: total = m.group(1).strip()
                if first is None:
                    m = re.search(r";\s*first layer.*=\s*([0-9hms\s.]+)", line, re.I)
                    if m: first = m.group(1).strip()
                if fil_g is not None and total is not None and first is not None:
                    break
    except Exception:
        pass
    return {
        # legacy
        "filamentG": fil_g, "total": total, "firstLayer": first,
        # clearer keys
        "filament_g": fil_g, "total_text": total, "first_layer": first,
    }

def _extract_best_thumb_bytes(gcode_text: str) -> Optional[bytes]:
    matches = list(THUMBS_RE.finditer(gcode_text))
    if not matches:
        return None
    best = max(matches, key=lambda m: int(m.group(1)) * int(m.group(2)))
    blob = ''.join(line.strip() for line in best.group(3).splitlines() if line.strip())
    try:
        data = base64.b64decode(blob, validate=False)
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            return data
    except Exception:
        pass
    return None

def _ensure_exe():
    if not os.path.isfile(PRUSA_SLICER_BIN):
        raise HTTPException(500, f"PrusaSlicer not found: {PRUSA_SLICER_BIN}")

def _run_with_fallback(cli: list[str]) -> None:
    """
    เรียก PrusaSlicer พร้อม fallback อัตโนมัติเมื่อ flag ไม่รองรับ:
    - Unknown option --thumbnail      -> ตัดทุก --thumbnail=*
    - Unknown option --filament-profile / --material-profile -> ตัด flag นั้น + ค่า preset ถัดไป
    """
    try:
        subprocess.run(cli, capture_output=True, text=True, check=True)
        return
    except subprocess.CalledProcessError as e:
        out = (e.stderr or e.stdout or "")
        joined = " ".join(cli)

        # 1) ตัด --thumbnail ถ้าไม่รองรับ
        if "Unknown option --thumbnail" in out and any(
            isinstance(c, str) and c.startswith("--thumbnail") for c in cli
        ):
            cli2 = [c for c in cli if not (isinstance(c, str) and c.startswith("--thumbnail"))]
            subprocess.run(cli2, capture_output=True, text=True, check=True)
            return

        # 2) ตัด --filament-profile / --material-profile ถ้าไม่รองรับ
        if ("--filament-profile" in joined and "Unknown option --filament-profile" in out) or \
           ("--material-profile" in joined and "Unknown option --material-profile" in out):
            filtered: list[str] = []
            skip_next = False
            for c in cli:
                if skip_next:
                    skip_next = False
                    continue
                if c in ("--filament-profile", "--material-profile"):
                    skip_next = True
                    continue
                filtered.append(c)
            subprocess.run(filtered, capture_output=True, text=True, check=True)
            return

        raise HTTPException(500, f"PrusaSlicer failed: {out or str(e)}")


# ---------- route ----------
@router.post("/preview", response_model=PreviewOut)
def preview(data: PreviewIn, user: User = Depends(get_confirmed_user)):
    """
    - ถ้าเป็น GCODE: ไม่ต้องสไลซ์ อ่าน metrics + สร้าง snapshot (ถ้ามีในไฟล์) แล้วส่งกุญแจ/URL กลับ
    - ถ้าเป็น STL : ดาวน์โหลดจาก S3 -> เรียก PrusaSlicer (พร้อม --thumbnail เมื่อเปิดใช้)
                     -> อัปโหลด .gcode ที่สไลซ์แล้วกลับ S3 (staging/...) -> คืนค่าพร้อมตัวเลขและ URL
    """
    key = (data.fileId or "").strip()
    if not key:
        raise HTTPException(422, "fileId is required")

    ext = (data.originExt or "").lower().strip()
    job_name = data.jobName or Path(key).stem or "print"

    # -------- GCODE ----------
    if ext in ("gcode", "gco", "gc"):
        tmp_in = WORK_DIR / f"in_{uuid.uuid4().hex}.gcode"
        try:
            download_to_file(key, str(tmp_in))
        except Exception as e:
            raise HTTPException(500, f"Failed to download G-code from S3: {e}")

        metrics = _read_metrics_from_gcode(tmp_in)
        est_min = _parse_time_to_min(metrics.get("total"))

        # presign G-code
        try:
            gcode_url = presign_get(key)
        except Exception:
            gcode_url = None

        # snapshot จาก thumbnail ที่ฝัง (ถ้ามี)
        snapshot_url = None
        try:
            gtxt = tmp_in.read_text(encoding="utf-8", errors="ignore")
            th = _extract_best_thumb_bytes(gtxt)
            if th:
                tkey = new_staging_key(f"{job_name}_preview.png")
                upload_bytes(th, tkey, content_type="image/png")
                try:
                    snapshot_url = presign_get(tkey)
                except Exception:
                    snapshot_url = None
        except Exception:
            pass

        try:
            tmp_in.unlink(missing_ok=True)
        except Exception:
            pass

        return PreviewOut(
            snapshotUrl=snapshot_url,
            gcodeUrl=gcode_url,
            printer=PRUSA_PRINTER_PRESET or "PrusaSlicer",
            settings={
                "infill":   (data.slicing or {}).get("infill"),
                "walls":    (data.slicing or {}).get("walls"),
                "support":  (data.slicing or {}).get("support", "none"),
                "model":    data.model,
                "name":     job_name,
            },
            result={
                "filamentG": metrics.get("filamentG"),
                "firstLayer": metrics.get("firstLayer"),
                "total": metrics.get("total"),
                # clearer keys
                "filament_g": metrics.get("filament_g"),
                "first_layer": metrics.get("first_layer"),
                "total_text": metrics.get("total_text"),
                "estimate_min": est_min,
            },
            gcodeKey=key,
            originalKey=key,
            estimateMin=est_min,
            isGcode=True,
        )

    # -------- STL ----------
    if ext not in ("stl", "obj", "3mf"):
        raise HTTPException(422, f"Unsupported originExt: {ext}")

    _ensure_exe()

    # 1) ดาวน์โหลดไฟล์เข้า temp
    tmp_id = uuid.uuid4().hex[:12]
    tmp_dir = WORK_DIR / f"job_{tmp_id}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    src_ext = ".stl" if ext == "stl" else f".{ext}"
    in_path  = tmp_dir / f"in{src_ext}"
    out_path = tmp_dir / "out.gcode"

    try:
        download_to_file(key, str(in_path))
    except Exception as e:
        raise HTTPException(500, f"Failed to download source from S3: {e}")

    # 2) ประกอบคำสั่ง (เปิด --thumbnail ถ้าอนุญาต)
    cli = [PRUSA_SLICER_BIN]
    if PRUSA_DATADIR:
        cli += ["--datadir", PRUSA_DATADIR]
    if PRUSA_PRINTER_PRESET:
        cli += ["--printer-profile", PRUSA_PRINTER_PRESET]
    if PRUSA_PRINT_PRESET:
        cli += ["--print-profile", PRUSA_PRINT_PRESET]
    if PRUSA_FILAMENT_PRESET:
        cli += ["--filament-profile", PRUSA_FILAMENT_PRESET]   # มี fallback ถ้าไม่รู้จัก

    cli += ["--export-gcode"]
    if PRUSA_ENABLE_THUMBNAIL:
        cli += ["--thumbnail=400x300", "--thumbnail=220x124"]
    cli += ["-o", str(out_path)]

    s = data.slicing or {}
    if s.get("infill") is not None:
        cli.append(f"--fill-density={int(s['infill'])}")
    if s.get("walls") is not None:
        cli.append(f"--perimeters={int(s['walls'])}")
    support = (s.get("support") or "none").lower()
    if support != "none":
        cli.append("--support-material")
        if support == "build_plate_only":
            cli.append("--support-material-buildplate-only")

    cli.append(str(in_path))

    # 3) เรียก CLI (พร้อม fallback)
    _run_with_fallback(cli)

    if not out_path.exists():
        raise HTTPException(500, "Slicer did not produce G-code")

    # 4) อ่าน metrics + ดึง thumbnail จาก .gcode ที่ได้
    try:
        gtxt = out_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        gtxt = ""
    metrics = _read_metrics_from_gcode(out_path)
    est_min = _parse_time_to_min(metrics.get("total"))

    snapshot_url = None
    if gtxt:
        th = _extract_best_thumb_bytes(gtxt)
        if th:
            tkey = new_staging_key(f"{job_name}_preview.png")
            upload_bytes(th, tkey, content_type="image/png")
            try:
                snapshot_url = presign_get(tkey)
            except Exception:
                snapshot_url = None

    # 5) อัปโหลด .gcode กลับ S3 (ขึ้นที่ staging/*)
    safe_base = (Path(data.jobName).stem or "print").replace(" ", "_")
    gkey = new_staging_key(f"{safe_base}.gcode")
    try:
        with out_path.open("rb") as f:
            upload_bytes(f.read(), gkey, content_type="text/plain")
    except Exception as e:
        raise HTTPException(500, f"Failed to upload sliced gcode: {e}")

    # presign G-code
    try:
        gcode_url = presign_get(gkey)
    except Exception:
        gcode_url = None

    # 6) เคลียร์ไฟล์ชั่วคราว (best-effort)
    try:
        in_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        tmp_dir.rmdir()
    except Exception:
        pass

    return PreviewOut(
        snapshotUrl=snapshot_url,                    # รูปจาก thumbnail (ถ้ามี)
        gcodeUrl=gcode_url,                          # ให้ viewer ใช้ได้เลย
        printer=PRUSA_PRINTER_PRESET or "PrusaSlicer",
        settings={
            "infill": s.get("infill"),
            "walls": s.get("walls"),
            "support": support,
            "model": data.model,
            "name": data.jobName,
        },
        result={
            "filamentG": metrics.get("filamentG"),
            "firstLayer": metrics.get("firstLayer"),
            "total": metrics.get("total"),
            # clearer keys
            "filament_g": metrics.get("filament_g"),
            "first_layer": metrics.get("first_layer"),
            "total_text": metrics.get("total_text"),
            "estimate_min": est_min,
        },
        gcodeKey=gkey,
        originalKey=key,
        estimateMin=est_min,
        isGcode=False,
    )
