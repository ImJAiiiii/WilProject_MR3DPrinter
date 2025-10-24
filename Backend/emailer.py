# backend/emailer.py
import os, smtplib, ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from sqlalchemy.orm import Session
from models import User

SMTP_ENABLED = os.getenv("SMTP_ENABLED", "false").lower() in {"1","true","yes","on"}
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_TLS  = os.getenv("SMTP_TLS", "true").lower() in {"1","true","yes","on"}
SMTP_SSL  = os.getenv("SMTP_SSL", "false").lower() in {"1","true","yes","on"}
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "3D Printer Bot")
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000")
TEAMS_CHANNEL_EMAIL = os.getenv("TEAMS_CHANNEL_EMAIL", "").strip()

def _smtp_client():
    if SMTP_SSL:
        context = ssl.create_default_context()
        return smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context)
    c = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
    if SMTP_TLS:
        c.starttls(context=ssl.create_default_context())
    if SMTP_USER:
        c.login(SMTP_USER, SMTP_PASS)
    return c

def send_notification_email(db: Session, employee_id: str, *, ntype: str, title: str, message: str | None = None, data: dict | None = None):
    if not SMTP_ENABLED:
        return False

    user = db.query(User).filter(User.employee_id == employee_id).first()
    to_addr = (user.email or "").strip()
    if not to_addr:
        return False

    # subject + body
    subject = f"[3D Printer] {title}"
    link = f"{FRONTEND_BASE_URL}/#/printing"  # ปุ่มเปิดหน้า Printing
    body_html = f"""
    <html><body style="font-family:Segoe UI,Arial">
      <h3>{title}</h3>
      <p>{(message or '')}</p>
      <p><a href="{link}">Open Printing Queue</a></p>
      <hr/><small>Type: {ntype}</small>
    </body></html>
    """
    body_text = f"{title}\n\n{message or ''}\n\nOpen Printing Queue: {link}\n"

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{EMAIL_FROM_NAME} <{EMAIL_FROM}>"
    msg["To"] = to_addr
    if TEAMS_CHANNEL_EMAIL:
        msg["Cc"] = TEAMS_CHANNEL_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    rcpts = [to_addr] + ([TEAMS_CHANNEL_EMAIL] if TEAMS_CHANNEL_EMAIL else [])
    with _smtp_client() as smtp:
        smtp.sendmail(EMAIL_FROM, rcpts, msg.as_string())
    return True
