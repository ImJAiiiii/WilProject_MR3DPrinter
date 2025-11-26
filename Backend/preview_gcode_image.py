#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
G-code → Isometric PNG preview (true scale, clean polylines)

- อ่าน G-code แล้วรวม segment extrusion ต่อเนื่องเป็น polyline 3D
- แยกสีตาม TYPE ของ PrusaSlicer/Cura (perimeter, infill, support ฯลฯ)
- วาดแบบ orthographic isometric + floor grid + bed outline
- รองรับ depth-fade ตามเลเยอร์ (แต่ค่า default ปัจจุบันจะวาดสีทึบเท่ากันทุกเลเยอร์)
- มีการ subsample segment อัตโนมัติถ้าเส้นเยอะมาก เพื่อลดโอกาส MemoryError
"""

from __future__ import annotations
import io
import re
import math
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Line3DCollection
from matplotlib.colors import to_rgba


# ============================ REGEX / CONSTANTS ============================

NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)"
TOK_RE  = re.compile(rf"\b([XYZE])\s*({NUM})", re.I)
Z_RE    = re.compile(rf"\bZ\s*({NUM})", re.I)
TYPE_RE = re.compile(r";\s*TYPE\s*:\s*([\w /-]+)", re.I)

# เกณฑ์ extrusion
E_MIN_ABS     = 1e-4      # ΔE ขั้นต่ำที่ถือว่าฉีด
E_PER_MM_MIN  = 2e-4      # ΔE ต่อระยะ XY ขั้นต่ำ
RETRACT_TOL   = -1e-9     # ถ้า ΔE < RETRACT_TOL ถือว่า retract

# limit จำนวน segment สูงสุดตอน render ป้องกันกินเมมเยอะเกิน
MAX_SEGMENTS_RENDER = 300_000

# สีตาม TYPE (ปรับโทนให้อิ่มและ contrast ดีขึ้น)
# NOTE: เพิ่ม key "Support material interface" ให้ตรงกับ G-code ของ PrusaSlicer
TYPE_COLORS = {
    "External perimeter": "#ff9900",
    "Perimeter":          "#ffcc00",
    "Overhang perimeter": "#ffbb55",
    "Solid infill":       "#e23e3e",
    "Top solid infill":   "#ff5555",
    "Internal infill":    "#e23e3e",
    "Infill":             "#e23e3e",
    "Bridge infill":      "#ff7777",
    "Gap fill":           "#f0a2a2",
    "Skirt/Brim":         "#2fbec3",
    "Skirt":              "#2fbec3",
    "Brim":               "#2fbec3",
    "Support material":           "#9aa3ff",
    "Support interface":          "#b7c0ff",
    "Support material interface": "#b7c0ff",  # ← ชื่อ TYPE ที่ PrusaSlicer ใช้จริง
    "Generic":            "#8fa6d8",
    "default":            "#ffcc00",
    "travel":             "#5e626b",
}

__all__ = [
    "parse_gcode_polylines",
    "normalize_placement",
    "render",
    "gcode_to_preview_png",
    "empty_placeholder_png",
]


# ============================ 1) PARSER ====================================

def parse_gcode_polylines(
    path: Path,
    include_travel: bool = False,
    retract_tol: float = RETRACT_TOL,
) -> Tuple[List[np.ndarray], List[str]]:
    """
    อ่าน G-code เป็น list ของ polylines (Nx3) + list สี
    - รวม G1 extrusion ต่อเนื่องให้เป็นโพลีไลน์ยาว ๆ
    - แตกเส้นเมื่อ: travel, เปลี่ยน TYPE, เปลี่ยน Z, retract หรือ prime สั้นมาก
    - รองรับ M82/M83/G92/G10/G11
    """
    polylines: List[np.ndarray] = []
    colors: List[str] = []

    x = y = z = e = 0.0
    last_e = 0.0
    curr_type = "default"
    line_pts: List[Tuple[float, float, float]] = []
    line_type = curr_type
    line_z: float | None = None
    absolute_e = True
    gap_open = True  # กันไม่ให้เส้นแรกพุ่งจาก (0,0)

    def flush() -> None:
        nonlocal line_pts, line_type
        if len(line_pts) >= 2:
            arr = np.array(line_pts, dtype=float)
            polylines.append(arr)
            colors.append(TYPE_COLORS.get(line_type, TYPE_COLORS["default"]))
        line_pts = []

    with path.open("r", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            uline = line.upper()

            # --- mode E ---
            if uline.startswith("M82"):
                absolute_e = True
                gap_open = True
                flush()
                continue
            if uline.startswith("M83"):
                absolute_e = False
                gap_open = True
                flush()
                continue

            # reset E
            if uline.startswith("G92"):
                m = re.search(r"\bE\s*(" + NUM + ")", line, flags=re.I)
                if m:
                    e = float(m.group(1))
                    last_e = e
                gap_open = True
                flush()
                continue

            # retract macros
            if uline.startswith(("G10", "G11")):
                gap_open = True
                flush()
                continue

            # TYPE comment
            mtype = TYPE_RE.search(line)
            if mtype:
                new_type = mtype.group(1).strip()
                if new_type != curr_type:
                    flush()
                    curr_type = new_type
                    line_type = curr_type
                    gap_open = True

            if uline.startswith(("G0", "G1")):
                # เปลี่ยน Z = layer ใหม่
                mZ = Z_RE.search(line)
                if mZ:
                    nz = float(mZ.group(1))
                    if line_z is not None and abs(nz - line_z) > 1e-9:
                        flush()
                        gap_open = True
                    z = nz
                    line_z = z

                coords = dict(TOK_RE.findall(line))
                coords = {k.upper(): v for k, v in coords.items()}

                has_xy = ("X" in coords) and ("Y" in coords)
                nx = float(coords["X"]) if "X" in coords else x
                ny = float(coords["Y"]) if "Y" in coords else y
                L = ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5 if has_xy else None

                # extrusion
                dE = None
                is_extrude = False
                is_retract = False

                if "E" in coords:
                    new_e = float(coords["E"])
                    if absolute_e:
                        dE = new_e - last_e
                        e = new_e
                    else:
                        dE = new_e
                        e += new_e
                    is_retract = (dE is not None and dE < retract_tol)

                    if dE is not None:
                        if L is None or L < 1e-6:
                            is_extrude = (dE > E_MIN_ABS)
                        else:
                            is_extrude = (
                                dE > E_MIN_ABS
                                and (dE / max(L, 1e-6)) > E_PER_MM_MIN
                            )

                if has_xy:
                    if is_extrude:
                        if not line_pts:
                            line_type = curr_type
                            if gap_open:
                                # เริ่มที่จุดใหม่เลย (ไม่ลากจาก 0,0)
                                line_pts = [(nx, ny, z)]
                            else:
                                line_pts = [(x, y, z), (nx, ny, z)]
                        else:
                            line_pts.append((nx, ny, z))
                        gap_open = False
                    else:
                        # travel move
                        if include_travel and L is not None and L > 1e-3:
                            flush()
                            keep = curr_type
                            line_type = "travel"
                            polylines.append(
                                np.array([(x, y, z), (nx, ny, z)], dtype=float)
                            )
                            colors.append(TYPE_COLORS["travel"])
                            line_type = keep
                        gap_open = True
                        flush()
                    x, y = nx, ny

                # retract / prime สั้นมาก → ตัดเส้น
                if is_retract:
                    gap_open = True
                    flush()
                else:
                    if (dE is not None and dE > 0.0 and L is not None and L < 0.15):
                        gap_open = True
                        flush()

                last_e = e

    flush()

    # TYPE ว่างทั้งหมด → ทำเป็น Generic
    if polylines and all(c == TYPE_COLORS["default"] for c in colors):
        colors = [TYPE_COLORS["Generic"]] * len(colors)

    return polylines, colors


# ============================ 2) PLACEMENT =================================

def _bbox(polylines: Iterable[np.ndarray]) -> Optional[Tuple[float, float, float, float]]:
    pts = [pl[:, :2] for pl in polylines if pl.size]
    if not pts:
        return None
    xy = np.vstack(pts)
    return float(xy[:, 0].min()), float(xy[:, 1].min()), float(xy[:, 0].max()), float(xy[:, 1].max())


def normalize_placement(
    polylines: List[np.ndarray],
    *,
    mode: str = "keep",       # keep | min0 | center | match_bbox
    bed: Tuple[float, float] | None = None,
    ref_bbox: Tuple[float, float, float, float] | None = None,
) -> List[np.ndarray]:
    """
    mode:
      - keep        : ไม่เปลี่ยนตำแหน่ง (ใช้ค่าตาม G-code → เหมือนใน Prusa)
      - min0        : ขยับให้ minX,minY = 0,0
      - center      : จัดกลางเตียง (ต้องให้ bed=(W,D))
      - match_bbox  : ใช้ ref_bbox (xmin,ymin,xmax,ymax) ให้ตรงกับอีกไฟล์ (scale=1)
    """
    if not polylines:
        return polylines
    bb = _bbox(polylines)
    if not bb:
        return polylines
    xmin, ymin, xmax, ymax = bb
    dx = dy = 0.0

    if mode == "min0":
        dx, dy = -xmin, -ymin
    elif mode == "center" and bed:
        w, d = bed
        dx = (w - (xmax - xmin)) / 2 - xmin
        dy = (d - (ymax - ymin)) / 2 - ymin
    elif mode == "match_bbox" and ref_bbox:
        rxmin, rymin, rxmax, rymax = ref_bbox
        dx = rxmin - xmin
        dy = rymin - ymin
    else:
        return polylines

    shifted: List[np.ndarray] = []
    for pl in polylines:
        p = pl.copy()
        p[:, 0] += dx
        p[:, 1] += dy
        shifted.append(p)
    return shifted


# ============================ 3) FLOOR / BED ================================

def add_floor_grid(ax, xmin, xmax, ymin, ymax, step=10.0, z0=0.0):
    step = float(step) if step and step > 0 else 10.0
    x0 = np.floor(xmin / step) * step; x1 = np.ceil(xmax / step) * step
    y0 = np.floor(ymin / step) * step; y1 = np.ceil(ymax / step) * step
    if x1 <= x0:
        x1 = x0 + step
    if y1 <= y0:
        y1 = y0 + step
    xs = np.arange(x0, x1 + 0.5 * step, step)
    ys = np.arange(y0, y1 + 0.5 * step, step)
    z = float(z0)
    for x in xs:
        ax.plot([x, x], [ys[0], ys[-1]], [z, z], lw=0.35, color="#2a2a2a")
    for y in ys:
        ax.plot([xs[0], xs[-1]], [y, y], [z, z], lw=0.35, color="#2a2a2a")
    ax.plot(
        [x0, x1, x1, x0, x0],
        [y0, y0, y1, y1, y0],
        [z,  z,  z,  z,  z],
        lw=0.8,
        color="#3a8d91",
    )


def add_bed_outline(ax, bed_w=220.0, bed_d=220.0, z0=0.0):
    try:
        w = float(bed_w); d = float(bed_d)
        x0, x1 = 0.0, w
        y0, y1 = 0.0, d
        ax.plot(
            [x0, x1, x1, x0, x0],
            [y0, y0, y1, y1, y0],
            [z0, z0, z0, z0, z0],
            lw=1.0,
            color="#2fbec3",
        )
    except Exception:
        pass


# ============================ 4) RENDER ====================================

def render(
    polylines: List[np.ndarray],
    cols: List[str],
    outpath: Path | str,
    *,
    fade: float = 1.0,            # 1.0 = ไม่มี depth-fade
    lw: float = 0.9,
    zscale: float = 1.0,
    pad_factor: float = 0.40,
    grid_step: float = 10.0,
    dpi: int = 400,
    figsize: Tuple[float, float] = (8.0, 6.0),   # 4:3
    antialias: bool = True,
    bed: Tuple[float, float] | None = None,
    alpha_floor: float = 1.0,     # 1.0 = ทุกเส้นทึบ 100%
    darken: float = 1.0,          # 1.0 = ไม่ทำสีดรอปลง
    azim_deg: float = -135.0,     # หันเตียงแบบเดียวกับใน Prusa (มองจากมุมหน้า-ขวา)
    elev_deg: float = 35.2643897,
) -> None:
    # --- ประเมินจำนวน segment ทั้งหมด เพื่อหาว่าต้อง subsample ไหม ---
    total_segments = 0
    if polylines:
        for pl in polylines:
            if pl.shape[0] >= 2:
                total_segments += (pl.shape[0] - 1)

    stride = 1
    if total_segments > MAX_SEGMENTS_RENDER:
        stride = max(1, math.ceil(total_segments / MAX_SEGMENTS_RENDER))

    # ปรับ DPI ลงนิดหน่อยในกรณีที่ต้อง subsample เพื่อช่วยลดเมมเพิ่ม
    dpi_eff = dpi
    if stride > 1 and dpi > 280:
        dpi_eff = 280

    fig = plt.figure(figsize=figsize, dpi=dpi_eff)
    ax = fig.add_subplot(111, projection="3d")

    # theme
    ax.set_facecolor("#141414")
    fig.patch.set_facecolor("#141414")
    for a in (ax.xaxis, ax.yaxis, ax.zaxis):
        a.pane.set_visible(False)
        a.line.set_color((0, 0, 0, 0))

    # zscale เฉพาะตอน render
    if polylines and zscale != 1.0:
        polylines = [np.column_stack((pl[:, 0], pl[:, 1], pl[:, 2] * zscale)) for pl in polylines]

    if polylines:
        all_pts = np.vstack(polylines)
        X, Y, Z = all_pts[:, 0], all_pts[:, 1], all_pts[:, 2]

        # true-scale limits + padding
        xr, yr, zr = np.ptp(X), np.ptp(Y), np.ptp(Z)
        xr = xr if xr > 1e-9 else 1.0
        yr = yr if yr > 1e-9 else 1.0
        zr = zr if zr > 1e-9 else 1.0
        xpad, ypad, zpad = xr * pad_factor, yr * pad_factor, max(zr * 0.12, 0.25)
        xmin, xmax = X.min() - xpad, X.max() + xpad
        ymin, ymax = Y.min() - ypad, Y.max() + ypad
        zmin, zmax = Z.min() - zpad, Z.max() + zpad
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        ax.set_zlim(zmin, zmax)
        try:
            ax.set_box_aspect((xmax - xmin, ymax - ymin, zmax - zmin))
        except Exception:
            pass

        # grid & bed
        grid_z = Z.min() - max(0.02 * (Z.max() - Z.min()), 0.2)
        add_floor_grid(ax, xmin, xmax, ymin, ymax, step=grid_step, z0=grid_z)
        if bed:
            add_bed_outline(ax, bed_w=bed[0], bed_d=bed[1], z0=grid_z)

        # depth-fade (optional – ด้วยค่า default จะได้ alpha=1 เสมอ)
        z_min, z_max = Z.min(), Z.max()
        z_range = max(z_max - z_min, 1e-6)
        mean_z = [np.mean(pl[:, 2]) for pl in polylines]
        order = np.argsort(mean_z)  # วาดจากเตี้ย → สูง
        polylines = [polylines[i] for i in order]
        cols = [cols[i] for i in order]

        alphas: List[float] = []
        for mz in mean_z:
            if fade is None or fade <= 0 or abs(fade - 1.0) < 1e-3:
                # ไม่มี depth-fade
                a = 1.0
            else:
                norm = (mz - z_min) / z_range
                a = fade ** (1.0 - norm)
            a = max(alpha_floor, min(1.0, a))
            alphas.append(a)
        alphas = [alphas[i] for i in order]
        rgba_cols = [to_rgba(c, a) for c, a in zip(cols, alphas)]

        # ทำสีให้ดาร์กลงเล็กน้อยกันแสบตา (ถ้า darken < 1)
        if darken is not None and 0.0 < darken < 1.0:
            tmp = []
            for r, g, b, a in rgba_cols:
                tmp.append((r * darken, g * darken, b * darken, a))
            rgba_cols = tmp

        # ปรับความหนาเส้นตาม DPI ให้ไม่บาง/หนาเกินไป
        dpi_scale = fig.dpi / 110.0
        adj_lw = max(0.35, lw / dpi_scale)

        # แปลง polylines → segment 2 จุด (มี subsample ด้วย stride)
        segments: List[np.ndarray] = []
        seg_colors: List[Tuple[float, float, float, float]] = []
        for pl, c in zip(polylines, rgba_cols):
            n = pl.shape[0]
            if n < 2:
                continue
            step = stride if stride > 1 else 1
            for i in range(0, n - 1, step):
                segments.append(np.vstack((pl[i], pl[i + 1])))
                seg_colors.append(c)

        if segments:
            lc = Line3DCollection(
                segments,
                colors=seg_colors,
                linewidths=adj_lw,
                antialiased=bool(antialias),
            )
            ax.add_collection3d(lc)

    # มุมกล้อง isometric
    ax.view_init(elev=elev_deg, azim=azim_deg)
    ax.set_proj_type("ortho")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_zticks([])

    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)
    plt.savefig(str(outpath), pad_inches=0, facecolor=fig.get_facecolor())
    plt.close(fig)


# ============================ 5) PLACEHOLDER ===============================

def empty_placeholder_png(text: str = "Preview unavailable", size=(1200, 900)) -> bytes:
    """สร้าง PNG ข้อความง่าย ๆ เผื่อเรนเดอร์ G-code ไม่สำเร็จ"""
    w, h = size
    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100)
    ax = fig.add_subplot(111)
    ax.set_facecolor("#141414")
    fig.patch.set_facecolor("#141414")
    ax.text(0.5, 0.5, text, color="#9aa0a6",
            ha="center", va="center", fontsize=16)
    ax.set_axis_off()
    buf = io.BytesIO()
    plt.savefig(buf, format="png",
                facecolor=fig.get_facecolor(),
                bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    return buf.getvalue()


# ============================ 6) HIGH-LEVEL API ============================

def gcode_to_preview_png(
    in_path: str | Path | io.BytesIO,
    out_path: str | Path,
    *,
    include_travel: bool = False,
    lw: float = 0.9,
    fade: float = 1.0,
    zscale: float = 1.0,
    pad: float = 0.40,
    grid: float = 10.0,
    dpi: int = 400,
    antialias: bool = True,
    bed: Tuple[float, float] | None = None,
    placement: str = "keep",                 # keep|min0|center|match_bbox
    ref_bbox: Tuple[float, float, float, float] | None = None,
    azim_deg: float = -135.0,
    elev_deg: float = 35.2643897,
) -> None:
    """
    ฟังก์ชันหลักที่ backend เรียก:
      - in_path  : path หรือ BytesIO ของ .gcode
      - out_path : path PNG ที่ต้องการให้เขียนทับ
    """
    # รองรับ BytesIO (เช่น กรณีอ่านจาก MinIO ตรง ๆ)
    if isinstance(in_path, io.BytesIO):
        tmp = Path("_tmp_render_src.gcode")
        tmp.write_bytes(in_path.getvalue())
        src = tmp
        cleanup = True
    else:
        src = Path(in_path)
        cleanup = False

    try:
        polylines, cols = parse_gcode_polylines(
            src,
            include_travel=include_travel,
            retract_tol=RETRACT_TOL,
        )
        polylines = normalize_placement(
            polylines,
            mode=placement,
            bed=bed,
            ref_bbox=ref_bbox,
        )
        render(
            polylines,
            cols,
            Path(out_path),
            fade=fade,
            lw=lw,
            zscale=zscale,
            pad_factor=pad,
            grid_step=grid,
            dpi=dpi,
            figsize=(8.0, 6.0),
            antialias=antialias,
            bed=bed,
            azim_deg=azim_deg,
            elev_deg=elev_deg,
        )
    finally:
        if cleanup:
            try:
                src.unlink()
            except Exception:
                pass


# ============================ 7) CLI =======================================

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="G-code → Isometric PNG preview (true scale, clean polylines)"
    )
    ap.add_argument("gcode", help="path to .gcode")
    ap.add_argument("--out", default=None, help="output PNG path (default: <name>_preview.png)")
    ap.add_argument("--include-travel", action="store_true", help="draw travel moves")
    ap.add_argument("--lw", type=float, default=0.9)
    ap.add_argument("--fade", type=float, default=1.0)
    ap.add_argument("--zscale", type=float, default=1.0)
    ap.add_argument("--pad", type=float, default=0.40)
    ap.add_argument("--grid", type=float, default=10.0)
    ap.add_argument("--dpi", type=int, default=400)
    ap.add_argument("--aa", action="store_true", help="enable antialiasing")
    ap.add_argument("--bed", type=str, default="", help="bed WxD mm, e.g. 220x220")
    ap.add_argument("--placement", type=str, default="keep",
                    choices=["keep", "min0", "center", "match_bbox"])
    ap.add_argument("--ref-bbox", type=str, default="",
                    help="xmin,ymin,xmax,ymax to match (no scale)")
    ap.add_argument("--azim", type=float, default=-135.0, help="camera azimuth in degrees")
    ap.add_argument("--elev", type=float, default=35.2643897, help="camera elevation in degrees")

    args = ap.parse_args()

    bed_tuple = None
    if args.bed:
        try:
            w, d = args.bed.lower().split("x")
            bed_tuple = (float(w), float(d))
        except Exception:
            bed_tuple = None

    ref_bbox = None
    if args.ref_bbox:
        try:
            parts = [float(p) for p in args.ref_bbox.split(",")]
            if len(parts) == 4:
                ref_bbox = tuple(parts)  # type: ignore[assignment]
        except Exception:
            ref_bbox = None

    out = Path(args.out or (Path(args.gcode).with_suffix("").name + "_preview.png"))
    gcode_to_preview_png(
        in_path=Path(args.gcode),
        out_path=out,
        include_travel=args.include_travel,
        lw=args.lw,
        fade=args.fade,
        zscale=args.zscale,
        pad=args.pad,
        grid=args.grid,
        dpi=args.dpi,
        antialias=args.aa,
        bed=bed_tuple,
        placement=args.placement,
        ref_bbox=ref_bbox,
        azim_deg=args.azim,
        elev_deg=args.elev,
    )
    print("saved:", out)