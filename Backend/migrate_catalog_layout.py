# backend/migrate_catalog_layout.py
from s3util import list_objects, head_object, copy_object, delete_objects, normalize_s3_prefix
import re

MODELS = ["Delta", "Hontech"]  # โฟลเดอร์บน S3 ที่ใช้อยู่

def base_noext(key: str) -> str:
    name = key.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]

def sibling(key: str, ext: str) -> str:
    head, tail = key.rsplit("/", 1)
    stem = tail.rsplit(".", 1)[0]
    return f"{head}/{stem}.{ext.lstrip('.')}"

def run():
    moved = 0
    dels  = []
    for model in MODELS:
        prefix = normalize_s3_prefix(f"catalog/{model}/")
        for obj in list_objects(prefix):
            k = obj["Key"]
            if not re.search(r"\.gcode$", k, re.I):
                continue
            # ข้ามถ้าอยู่ในโฟลเดอร์ย่อยแล้ว (…/<Base>/<Base>.gcode)
            parts = k[len(prefix):].split("/")
            if len(parts) >= 2:
                continue
            stem = base_noext(k)
            dst_prefix = f"{prefix}{stem}/"
            gcode_dst  = f"{dst_prefix}{stem}.gcode"
            json_src   = sibling(k, "json")
            png_src    = sibling(k, "png")
            json_dst   = f"{dst_prefix}{stem}.json"
            png_dst    = f"{dst_prefix}{stem}.png"

            # ย้าย .gcode
            copy_object(k, gcode_dst)
            dels.append(k)
            # ย้าย .json ถ้ามี
            try:
                head_object(json_src)
                copy_object(json_src, json_dst)
                dels.append(json_src)
            except Exception:
                pass
            # ย้าย .png ถ้ามี
            try:
                head_object(png_src)
                copy_object(png_src, png_dst)
                dels.append(png_src)
            except Exception:
                pass

            moved += 1
            print(f"moved: {k} -> {gcode_dst}")

    if dels:
        delete_objects(dels)
    print(f"Done. moved {moved} items.")

if __name__ == "__main__":
    run()
