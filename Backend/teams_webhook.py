# backend/teams_webhook.py
import os, json, http.client, urllib.parse

WEBHOOK = os.getenv("TEAMS_WEBHOOK_URL", "").strip()
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000")

def _color_of(severity: str) -> str:
    return {"info":"#2563eb","success":"#16a34a","warning":"#f59e0b","error":"#dc2626"}.get(severity, "#2563eb")

def send_teams_notification(
    title: str | None = None,
    message: str | None = None,
    severity: str | None = None,
    url: str | None = None,
    # รองรับรูปแบบใหม่ที่เรียกมาเป็น kwargs:
    ntype: str | None = None,
    data: dict | None = None,
):
    """
    รองรับทั้ง 2 รูปแบบการเรียก:
      send_teams_notification(title, message, severity, url)
    หรือ
      send_teams_notification(ntype=..., title=..., message=..., data=...)
    """
    if not WEBHOOK:
        return False

    # ถ้าผู้เรียกส่ง severity ไม่มา ลองเดาจาก ntype
    if not severity and ntype:
        if ".completed" in ntype or ntype.endswith(".ok"):
            severity = "success"
        elif ".failed" in ntype or ".error" in ntype:
            severity = "error"
        elif ".warning" in ntype:
            severity = "warning"
        else:
            severity = "info"
    if not severity:
        severity = "info"

    if not url:
        url = f"{FRONTEND_BASE_URL}/#/printing"

    color = _color_of(severity)
    card = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": color,
        "summary": title or (ntype or "Notification"),
        "title": title or (ntype or "Notification"),
        "text": message or "",
    }
    if data:
        # แนบข้อมูลเสริมเป็น code block เล็กๆ
        try:
            pretty = "```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```"
            card["text"] = (card["text"] + "\n\n" + pretty).strip()
        except Exception:
            pass

    if url:
        card["potentialAction"] = [{
            "@type": "OpenUri",
            "name": "Open in app",
            "targets": [{"os": "default", "uri": url}]
        }]

    u = urllib.parse.urlsplit(WEBHOOK)
    conn = http.client.HTTPSConnection(u.netloc) if u.scheme == "https" else http.client.HTTPConnection(u.netloc)
    path = u.path + (("?" + u.query) if u.query else "")
    body = json.dumps(card).encode("utf-8")
    headers = {"Content-Type":"application/json"}
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    ok = (200 <= resp.status < 300)
    try: resp.read()
    except: pass
    conn.close()
    return ok
