# backend/routes_stream.py
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
import httpx
import os

router = APIRouter(prefix="/printers", tags=["stream"])

# กำหนดผ่าน .env ก็ได้: OCTO_MJPEG=http://172.20.10.3:5000/webcam/?action=stream
OCTO_MJPEG = os.getenv("OCTO_MJPEG", "http://172.20.10.3:5000/webcam/?action=stream")

@router.get("/{printer_id}/stream")
async def proxy_mjpeg(printer_id: str):
    # สำคัญ: http2=False กัน multipart ไปชน HTTP/2 framing
    transport = httpx.AsyncHTTPTransport(retries=0, http2=False)
    client = httpx.AsyncClient(transport=transport, timeout=None)

    upstream = await client.stream("GET", OCTO_MJPEG, headers={"Connection": "keep-alive"})

    async def gen():
        async for chunk in upstream.aiter_raw():
            # ส่งดิบ ไม่แปลง ไม่บัฟเฟอร์
            yield chunk

    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=--frame",
        headers=headers,
    )
