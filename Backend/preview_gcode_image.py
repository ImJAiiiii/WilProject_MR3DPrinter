#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# G-code → Isometric Preview (true scale, 4:3, clean polylines)

from __future__ import annotations
import io
import re
from pathlib import Path
from typing import List, Tuple, Iterable, Optional

import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Line3DCollection
from matplotlib.colors import to_rgba

NUM     = r"[-+]?(?:\d+\.?\d*|\.\d+)"
TOK_RE  = re.compile(rf"\b([XYZE])\s*({NUM})", re.I)
Z_RE    = re.compile(rf"\bZ\s*({NUM})", re.I)
TYPE_RE = re.compile(r";\s*TYPE\s*:\s*([\w /-]+)", re.I)

# ---------- เกณฑ์กรอง extrusion ----------
E_MIN_ABS     = 1e-4      # ΔE ขั้นต่ำที่ถือว่า "ฉีด"
E_PER_MM_MIN  = 2e-4      # อัตรา ΔE ต่อระยะ XY ขั้นต่ำ
RETRACT_TOL   = -1e-9     # ตรวจดึงเส้น

# ---------- สีตาม TYPE ----------
TYPE_COLORS = {
    "External perimeter":"#ff9900",
    "Perimeter":"#ffcc00",
    "Overhang perimeter":"#ffbb55",
    "Solid infill":"#e23e3e",
    "Top solid infill":"#ff5555",
    "Internal infill":"#e23e3e",
    "Infill":"#e23e3e",
    "Bridge infill":"#ff7777",
    "Gap fill":"#f0a2a2",
    "Skirt/Brim":"#2fbec3",
    "Skirt":"#2fbec3",
    "Brim":"#2fbec3",
    "Support material":"#9aa3ff",
    "Support interface":"#b7c0ff",
    "Generic":"#8fa6d8",
    "default":"#ffcc00",
    "travel":"#5e626b",
}

__all__ = [
    "parse_gcode_polylines",
    "normalize_placement",
    "render",
    "gcode_to_preview_png",
    "empty_placeholder_png",
]

# =====================================================================
# 1) PARSER → polylines (Nx3) + สี + ชนิด + เลเยอร์  (no fake joins)
# =====================================================================
def parse_gcode_polylines(
    path: Path,
    include_travel: bool = False,
    retract_tol: float = RETRACT_TOL,
) -> Tuple[List[np.ndarray], List[str], List[str], List[int]]:
    """
    อ่าน G-code เป็นเส้นโพลีไลน์ 3D พร้อมสี/ชนิด/เลเยอร์
    - รองรับ M82/M83/G92/G10/G11
    - กรอง prime/wipe ด้วยเกณฑ์คู่ ΔE และ ΔE/ระยะ
    - include_travel=True จะวาดเส้นวิ่งเป็นสี travel แยกจาก TYPE
    - กันเส้นทแยงหลอนด้วยแฟล็ก gap_open (ไม่เชื่อมข้ามช่วงที่เราไม่วาด)
    """
    polylines: List[np.ndarray] = []
    colors: List[str]           = []
    types: List[str]            = []
    layers: List[int]           = []

    x = y = z = e = 0.0
    last_e = 0.0
    curr_type = "default"
    line_pts: List[Tuple[float,float,float]] = []
    line_type = curr_type
    line_z: float | None = None
    absolute_e = True
    layer_id = 0

    gap_open = True  # เริ่มต้นถือว่าเพิ่งมี gap เพื่อไม่ให้เชื่อมกับ (0,0)

    def flush():
        nonlocal line_pts, line_type, layer_id
        if len(line_pts) >= 2:
            arr = np.array(line_pts, dtype=float)
            polylines.append(arr)
            colors.append(TYPE_COLORS.get(line_type, TYPE_COLORS["default"]))
            types.append(line_type)
            layers.append(layer_id)
        line_pts = []

    with path.open("r", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            uline = line.upper()

            # โหมด E
            if uline.startswith("M82"): absolute_e = True;  gap_open=True;  flush();  continue
            if uline.startswith("M83"): absolute_e = False; gap_open=True;  flush();  continue

            # reset E
            if uline.startswith("G92"):
                m = re.search(r"\bE\s*("+NUM+")", line, flags=re.I)
                if m:
                    e = float(m.group(1)); last_e = e
                gap_open = True
                flush()
                continue

            # retract/unretract macros
            if uline.startswith(("G10", "G11")):
                gap_open = True
                flush()
                continue

            # ;TYPE:
            mtype = TYPE_RE.search(line)
            if mtype:
                new_type = mtype.group(1).strip()
                if new_type != curr_type:
                    flush()
                    curr_type = new_type
                    line_type = curr_type
                    gap_open = True  # เปลี่ยน TYPE ถือว่าเริ่มกลุ่มใหม่

            # เคลื่อนที่
            if uline.startswith(("G0", "G1")):
                # เปลี่ยน Z => layer ใหม่
                mZ = Z_RE.search(line)
                if mZ:
                    nz = float(mZ.group(1))
                    if line_z is not None and abs(nz - line_z) > 1e-9:
                        flush(); layer_id += 1; gap_open = True
                    z = nz; line_z = z

                coords = dict(TOK_RE.findall(line))
                coords = {k.upper(): v for k, v in coords.items()}

                has_xy = ("X" in coords) and ("Y" in coords)
                nx = float(coords["X"]) if "X" in coords else x
                ny = float(coords["Y"]) if "Y" in coords else y
                L = ((nx - x)**2 + (ny - y)**2)**0.5 if has_xy else None

                # คำนวณ extrusion
                dE = None
                is_extrude = False
                is_retract = False
                if "E" in coords:
                    new_e = float(coords["E"])
                    if absolute_e:
                        dE = new_e - last_e; e = new_e
                    else:
                        dE = new_e; e += new_e
                    is_retract = (dE is not None and dE < RETRACT_TOL)

                    # เกณฑ์คู่: ΔE และ ΔE/ระยะ
                    if dE is not None:
                        if L is None or L < 1e-6:
                            is_extrude = (dE > E_MIN_ABS)
                        else:
                            is_extrude = (dE > E_MIN_ABS) and ((dE / max(L,1e-6)) > E_PER_MM_MIN)

                # เพิ่มจุด
                if has_xy:
                    if is_extrude:
                        if not line_pts:
                            line_type = curr_type
                            if gap_open:
                                line_pts = [(nx, ny, z)]
                            else:
                                line_pts = [(x, y, z), (nx, ny, z)]
                        else:
                            line_pts.append((nx, ny, z))
                        gap_open = False
                    else:
                        if include_travel:
                            flush()
                            keep = curr_type
                            line_type = "travel"
                            polylines.append(np.array([(x,y,z),(nx,ny,z)], dtype=float))
                            colors.append(TYPE_COLORS["travel"])
                            types.append("travel"); layers.append(layer_id)
                            line_type = keep
                        gap_open = True
                        flush()
                    x, y = nx, ny

                if is_retract:
                    gap_open = True
                    flush()
                else:
                    if (dE is not None and dE > 0.0 and L is not None and L < 0.15):
                        gap_open = True
                        flush()

                last_e = e

    flush()

    # Fallback: ไม่มีเส้นเลย → ลองรวม travel
    if not polylines and not include_travel:
        return parse_gcode_polylines(path, include_travel=True, retract_tol=retract_tol)

    # TYPE ว่างทั้งหมด → Generic
    if types and all((t is None) or (t.lower() == "default") for t in types):
        for i in range(len(types)):
            types[i] = "Generic"; colors[i] = TYPE_COLORS["Generic"]

    return polylines, colors, types, layers


# =====================================================================
# 2) ปรับตำแหน่งชิ้นงานก่อนเรนเดอร์ (เพื่อ “จัดวางให้เหมือน”)
# =====================================================================
def _bbox(polylines: Iterable[np.ndarray]) -> Optional[Tuple[float,float,float,float]]:
    pts = [pl[:, :2] for pl in polylines if pl.size]
    if not pts: return None
    xy = np.vstack(pts)
    return float(xy[:,0].min()), float(xy[:,1].min()), float(xy[:,0].max()), float(xy[:,1].max())

def normalize_placement(
    polylines: List[np.ndarray],
    *,
    mode: str = "keep",       # keep | min0 | center | match_bbox
    bed: Tuple[float,float] | None = None,
    ref_bbox: Tuple[float,float,float,float] | None = None,
) -> List[np.ndarray]:
    """
    mode:
      - keep        : ไม่เปลี่ยนตำแหน่ง (ดีฟอลต์)
      - min0        : ขยับให้ minX,minY = 0,0
      - center      : จัดกลางเตียง (ต้องระบุ bed=(W,D))
      - match_bbox  : scale=1, จัดให้อยู่ตำแหน่ง bbox เท่ากับ ref_bbox (ใช้ทำให้วางเหมือนไฟล์อ้างอิง)
    """
    if not polylines:
        return polylines
    bb = _bbox(polylines)
    if not bb: return polylines
    xmin,ymin,xmax,ymax = bb
    dx = dy = 0.0

    if mode == "min0":
        dx, dy = -xmin, -ymin
    elif mode == "center" and bed:
        w, d = bed
        dx = (w - (xmax - xmin))/2 - xmin
        dy = (d - (ymax - ymin))/2 - ymin
    elif mode == "match_bbox" and ref_bbox:
        rxmin, rymin, rxmax, rymax = ref_bbox
        dx = rxmin - xmin
        dy = rymin - ymin
    else:
        return polylines

    shifted = []
    for pl in polylines:
        p = pl.copy()
        p[:,0] += dx; p[:,1] += dy
        shifted.append(p)
    return shifted


# =====================================================================
# 3) วาดกริดพื้น + ขอบเตียง
# =====================================================================
def add_floor_grid(ax, xmin, xmax, ymin, ymax, step=10.0, z0=0.0):
    step = float(step) if step and step > 0 else 10.0
    x0 = np.floor(xmin/step)*step; x1 = np.ceil(xmax/step)*step
    y0 = np.floor(ymin/step)*step; y1 = np.ceil(ymax/step)*step
    if x1 <= x0: x1 = x0 + step
    if y1 <= y0: y1 = y0 + step
    xs = np.arange(x0, x1 + 0.5*step, step)
    ys = np.arange(y0, y1 + 0.5*step, step)
    z = float(z0)
    for x in xs: ax.plot([x,x],[ys[0],ys[-1]],[z,z], lw=0.35, color="#2a2a2a")
    for y in ys: ax.plot([xs[0],xs[-1]],[y,y],[z,z], lw=0.35, color="#2a2a2a")
    ax.plot([x0,x1,x1,x0,x0],[y0,y0,y1,y1,y0],[z,z,z,z,z], lw=0.8, color="#3a8d91")

def add_bed_outline(ax, bed_w=220.0, bed_d=220.0, z0=0.0):
    try:
        w = float(bed_w); d = float(bed_d)
        x0, x1 = 0.0, w
        y0, y1 = 0.0, d
        ax.plot([x0,x1,x1,x0,x0],[y0,y0,y1,y1,y0],[z0,z0,z0,z0,z0], lw=1.0, color="#295c5f")
    except Exception:
        pass


# =====================================================================
# 4) RENDER (isometric orthographic + depth fade)
# =====================================================================
def render(
    polylines: List[np.ndarray],
    cols: List[str],
    outpath: Path | str,
    *,
    fade: float = 0.75,              # เดิม 0.9 → ลดลงให้เข้มขึ้น
    lw: float = 0.9,                 # เดิม 0.7 → เส้นหนาขึ้น
    zscale: float = 1.0,
    pad_factor: float = 0.40,
    grid_step: float = 10.0,
    dpi: int = 400,
    figsize: Tuple[float, float] = (8.0, 6.0),
    antialias: bool = False,
    bed: Tuple[float, float] | None = None,
    alpha_floor: float = 0.60,       # ความทึบขั้นต่ำของเส้น (0–1)
    darken: float = 0.90,            # คูณ RGB เพื่อลดความสว่าง (<1 = ดาร์กขึ้น)
) -> None:
    fig = plt.figure(figsize=figsize, dpi=dpi)
    ax  = fig.add_subplot(111, projection="3d")

    ax.set_facecolor("#141414"); fig.patch.set_facecolor("#141414")
    for a in (ax.xaxis, ax.yaxis, ax.zaxis):
        a.pane.set_visible(False); a.line.set_color((0,0,0,0))

    if polylines and zscale != 1.0:
        polylines = [np.column_stack((pl[:,0], pl[:,1], pl[:,2]*zscale)) for pl in polylines]

    if polylines:
        all_pts = np.vstack(polylines)
        X, Y, Z = all_pts[:,0], all_pts[:,1], all_pts[:,2]

        xr, yr, zr = np.ptp(X), np.ptp(Y), np.ptp(Z)
        xr = xr if xr > 1e-9 else 1.0
        yr = yr if yr > 1e-9 else 1.0
        zr = zr if zr > 1e-9 else 1.0
        xpad, ypad, zpad = xr*pad_factor, yr*pad_factor, max(zr*0.12, 0.25)
        xmin, xmax = X.min()-xpad, X.max()+xpad
        ymin, ymax = Y.min()-ypad, Y.max()+ypad
        zmin, zmax = Z.min()-zpad, Z.max()+zpad
        ax.set_xlim(xmin, xmax); ax.set_ylim(ymin, ymax); ax.set_zlim(zmin, zmax)
        try: ax.set_box_aspect((xmax-xmin, ymax-ymin, zmax-zmin))
        except Exception: pass

        grid_z = Z.min() - max(0.02*(Z.max()-Z.min()), 0.2)
        add_floor_grid(ax, xmin, xmax, ymin, ymax, step=grid_step, z0=grid_z)
        if bed: add_bed_outline(ax, bed_w=bed[0], bed_d=bed[1], z0=grid_z)

        z_min, z_max = Z.min(), Z.max(); z_range = max(z_max-z_min, 1e-6)
        mean_z = [np.mean(pl[:,2]) for pl in polylines]
        order  = np.argsort(mean_z)  # วาดจากต่ำ→สูง
        polylines = [polylines[i] for i in order]
        cols      = [cols[i]      for i in order]

        # คำนวณ alpha ตามความสูง + บังคับขั้นต่ำให้เข้มขึ้น
        alphas = []
        for mz in mean_z:
            norm = (mz - z_min)/z_range
            a = fade ** (1.0 - norm)
            a = max(alpha_floor, min(1.0, a))
            alphas.append(a)
        alphas = [alphas[i] for i in order]

        rgba_cols = [to_rgba(c, a) for c, a in zip(cols, alphas)]

        # ดาร์กโทนสีด้วยตัวคูณ
        if darken is not None and 0.0 < darken < 1.0:
            tmp = []
            for r, g, b, a in rgba_cols:
                tmp.append((r*darken, g*darken, b*darken, a))
            rgba_cols = tmp

        dpi_scale = fig.dpi / 110.0
        adj_lw = max(0.35, lw / dpi_scale)

        segments, seg_colors = [], []
        for pl, c in zip(polylines, rgba_cols):
            if pl.shape[0] < 2: continue
            segments.extend([[pl[i], pl[i+1]] for i in range(pl.shape[0]-1)])
            seg_colors.extend([c]*(pl.shape[0]-1))

        if segments:
            lc = Line3DCollection(
                segments,
                colors=seg_colors,
                linewidths=adj_lw,
                antialiased=bool(antialias),
            )
            ax.add_collection3d(lc)

    ax.view_init(elev=35.2643897, azim=45)
    ax.set_proj_type("ortho")
    ax.set_xticks([]); ax.set_yticks([]); ax.set_zticks([])
    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)
    plt.savefig(str(outpath), pad_inches=0, facecolor=fig.get_facecolor())
    plt.close(fig)


# =====================================================================
# 5) PNG placeholder
# =====================================================================
def empty_placeholder_png(text: str = "Preview unavailable", size=(1200, 900)) -> bytes:
    w, h = size
    fig = plt.figure(figsize=(w/100, h/100), dpi=100)
    ax = fig.add_subplot(111)
    ax.set_facecolor("#141414"); fig.patch.set_facecolor("#141414")
    ax.text(0.5, 0.5, text, color="#9aa0a6", ha="center", va="center", fontsize=16)
    ax.set_axis_off()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    return buf.getvalue()


# =====================================================================
# 6) API: gcode → PNG  (pipeline: parse → normalize → render)
# =====================================================================
def gcode_to_preview_png(
    in_path: str | Path | io.BytesIO,
    out_path: str | Path,
    *,
    include_travel: bool = False,
    lw: float = 0.6,
    fade: float = 0.9,
    zscale: float = 1.0,
    pad: float = 0.40,
    grid: float = 10.0,
    dpi: int = 400,
    antialias: bool = False,
    bed: Tuple[float, float] | None = None,
    placement: str = "keep",                 # keep|min0|center|match_bbox
    ref_bbox: Tuple[float,float,float,float] | None = None,
) -> None:
    # รองรับ BytesIO
    if isinstance(in_path, io.BytesIO):
        tmp = Path("_tmp_render_src.gcode")
        tmp.write_bytes(in_path.getvalue())
        src = tmp; cleanup = True
    else:
        src = Path(in_path); cleanup = False

    try:
        polylines, cols, *_ = parse_gcode_polylines(src, include_travel=include_travel)
        polylines = normalize_placement(polylines, mode=placement, bed=bed, ref_bbox=ref_bbox)
        render(
            polylines, cols, Path(out_path),
            fade=fade, lw=lw, zscale=zscale,
            pad_factor=pad, grid_step=grid,
            dpi=dpi, figsize=(8.0, 6.0),
            antialias=antialias, bed=bed,
        )
    finally:
        if cleanup:
            try: src.unlink()
            except Exception: pass


# =====================================================================
# 7) CLI
# =====================================================================
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="G-code → Isometric PNG preview (true scale, clean polylines)")
    ap.add_argument("gcode", help="path to .gcode")
    ap.add_argument("--out", default=None, help="output PNG path (default: <name>_preview.png)")
    ap.add_argument("--include-travel", action="store_true", help="draw travel moves (thin grey)")
    ap.add_argument("--lw", type=float, default=0.6)
    ap.add_argument("--fade", type=float, default=0.9)
    ap.add_argument("--zscale", type=float, default=1.0)
    ap.add_argument("--pad", type=float, default=0.40)
    ap.add_argument("--grid", type=float, default=10.0)
    ap.add_argument("--dpi", type=int, default=400)
    ap.add_argument("--aa", action="store_true", help="enable antialiasing")
    ap.add_argument("--bed", type=str, default="", help="bed WxD in mm, e.g., 220x220")
    ap.add_argument("--placement", type=str, default="keep", choices=["keep","min0","center","match_bbox"])
    ap.add_argument("--ref-bbox", type=str, default="", help="xmin,ymin,xmax,ymax to match (no scale)")

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
            if len(parts) == 4: ref_bbox = tuple(parts)  # type: ignore[arg-type]
        except Exception:
            ref_bbox = None

    out = Path(args.out or (Path(args.gcode).with_suffix("").name + "_preview.png"))
    gcode_to_preview_png(
        in_path=Path(args.gcode),
        out_path=out,
        include_travel=args.include_travel,
        lw=args.lw, fade=args.fade, zscale=args.zscale,
        pad=args.pad, grid=args.grid, dpi=args.dpi,
        antialias=args.aa, bed=bed_tuple,
        placement=args.placement, ref_bbox=ref_bbox,
    )
    print("saved:", out)
