@echo off
REM เปลี่ยนมาที่โฟลเดอร์ Backend (เครื่อง Adi_m)
cd /d "C:\Users\Adi_m\OneDrive\Desktop\MR3DPrinter\WilProject_MR3DPrinter\Backend"

REM เปิด virtualenv
call .venv\Scripts\activate.bat

REM ลบ notifications เก่ากว่า KEEP_DAYS ใน cleanup_notifications.py
python scripts\cleanup_notifications.py

