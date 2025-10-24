# ws_probe.py  —  Minimal WS to probe handshake/auth
import os, logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query

log = logging.getLogger("ws-probe")
logging.basicConfig(level=logging.DEBUG)

app = FastAPI()
EXPECTED = os.getenv("PRINTER_WS_TOKEN", "dev123").strip()

@app.get("/")  # sanity check HTTP
def root():
    return {"ok": True, "expected": EXPECTED}

@app.websocket("/ws/printer")
async def ws_printer(ws: WebSocket, token: str = Query(default="")):
    # ----- diag logging -----
    try:
        # log token we received (ระวังอย่า log ในโปรดักชัน)
        log.warning("WS handshake: token='%s' EXPECTED='%s'", token, EXPECTED)
    except Exception:
        pass

    # แนะนำ: "รับ" ก่อน แล้วค่อยตรวจ เพื่อให้เห็นว่า handshake ผ่านจริง
    await ws.accept()
    if token.strip() != EXPECTED:
        # บอกเหตุผลฝั่ง client ชัดเจน
        await ws.send_text("AUTH_FAIL")
        await ws.close(code=4403)  # policy violation-ish
        return

    try:
        await ws.send_text("AUTH_OK")
        while True:
            msg = await ws.receive_text()
            await ws.send_text(f"echo:{msg}")
    except WebSocketDisconnect:
        log.info("client disconnected")
