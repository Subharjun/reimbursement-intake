"""
Reimbursement Intake API — FastAPI backend

Receives the expense form, uploads the receipt to the UiPath storage bucket,
then starts the Maestro Case via the Orchestrator REST API (no uip CLI needed).
After a successful submission it sends a notification email to the finance team.

Local dev:
    pip install -r requirements.txt
    uvicorn api:app --reload --port 8000

Render / production env vars:
    UIPATH_ACCESS_TOKEN      — UiPath PAT (Orchestrator scope)
    SMTP_USER                — Gmail address to send FROM  e.g. you@gmail.com
    SMTP_APP_PASSWORD        — Gmail App Password (16-char, not your login password)
"""

import asyncio
import json
import os
import re
import smtplib
import uuid
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

app = FastAPI(title="Reimbursement Intake API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Config (all overridable via env / .env) ───────────────────────────────────

UIPATH_BASE_URL = os.getenv(
    "UIPATH_BASE_URL",
    "https://staging.uipath.com/hackathon26_332/DefaultTenant",
)

UIPATH_FOLDER_ID = os.getenv("UIPATH_FOLDER_ID", "3054578")
UIPATH_CASE_FOLDER_NUMERIC_ID = os.getenv("UIPATH_CASE_FOLDER_NUMERIC_ID", "3154132")
BUCKET_ID = int(os.getenv("UIPATH_BUCKET_ID", "199727"))
CASE_RELEASE_KEY = os.getenv(
    "UIPATH_CASE_RELEASE_KEY",
    "8602dfb7-4304-4768-b853-544f1ef7d972",
)

# ── Email config ──────────────────────────────────────────────────────────────

NOTIFY_TO   = "i.am.mir.jasim@gmail.com"
SMTP_USER   = os.getenv("SMTP_USER", "")
SMTP_PASS   = os.getenv("SMTP_APP_PASSWORD", "")


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_token() -> str:
    token = os.getenv("UIPATH_ACCESS_TOKEN", "").strip()
    if token:
        return token
    auth_path = Path.home() / ".uipath" / ".auth"
    if auth_path.exists():
        for line in auth_path.read_text().splitlines():
            if line.startswith("UIPATH_ACCESS_TOKEN="):
                t = line.split("=", 1)[1].strip()
                if t:
                    return t
    raise HTTPException(
        status_code=500,
        detail=(
            "No UiPath token. Set UIPATH_ACCESS_TOKEN env var. "
            "Get the value by running `uip auth token` locally after `uip login`."
        ),
    )


def _hdrs(token: str, folder_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-UIPATH-OrganizationUnitId": folder_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ── Bucket upload ─────────────────────────────────────────────────────────────

async def _upload_to_bucket(token: str, filename: str, content: bytes) -> str:
    safe_name = re.sub(r"[^\w.\-]", "_", filename)
    uri_url = (
        f"{UIPATH_BASE_URL}/orchestrator_/odata/Buckets({BUCKET_ID})"
        f"/UiPath.Server.Configuration.OData.GetWriteUri"
        f"?path={safe_name}&expiryInMinutes=30"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(uri_url, headers=_hdrs(token, UIPATH_FOLDER_ID))
        if r.status_code != 200:
            raise HTTPException(502, f"Bucket write URI failed: {r.status_code} {r.text[:300]}")
        put_r = await client.put(
            r.json()["Uri"],
            content=content,
            headers={
                "Content-Type": "application/octet-stream",
                "x-ms-blob-type": "BlockBlob",
            },
        )
        if put_r.status_code not in (200, 201, 204):
            raise HTTPException(502, f"Bucket PUT failed: {put_r.status_code} {put_r.text[:300]}")
    return safe_name


# ── Maestro Case trigger ──────────────────────────────────────────────────────

async def _start_case(token: str, inputs: dict) -> str | None:
    url = (
        f"{UIPATH_BASE_URL}/orchestrator_/odata/Jobs"
        "/UiPath.Server.Configuration.OData.StartJobs"
    )
    body = {
        "startInfo": {
            "ReleaseKey": CASE_RELEASE_KEY,
            "Strategy": "ModernJobsCount",
            "JobsCount": 1,
            "InputArguments": json.dumps(inputs),
        }
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, json=body, headers=_hdrs(token, UIPATH_CASE_FOLDER_NUMERIC_ID))
    if r.status_code not in (200, 201):
        hint = " — token expired, update UIPATH_ACCESS_TOKEN" if r.status_code == 401 else ""
        raise HTTPException(502, f"Case start failed ({r.status_code}){hint}: {r.text[:400]}")
    data = r.json()
    jobs = data.get("value", [data])
    key = jobs[0].get("Key") or jobs[0].get("Id") if jobs else None
    return str(key) if key else None


# ── Notification email ────────────────────────────────────────────────────────

def _build_email(
    employee_name: str,
    employee_email: str,
    expense_type: str,
    currency: str,
    amount: float,
    date: str,
    purpose: str,
    vendor: str,
    receipt_name: str | None,
    receipt_bytes: bytes | None,
) -> MIMEMultipart:
    msg = MIMEMultipart()
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFY_TO
    msg["Subject"] = f"{expense_type} Reimbursement Request"

    body = (
        f"Dear Finance Team,\n\n"
        f"I am submitting my {expense_type.lower()} reimbursement request for the recent official travel. "
        f"Please find the attached {expense_type.lower()} bills for your reference.\n\n"
        f"Total amount: {currency} {amount:.2f} "
        f"Date of expense: {date} "
        f"Purpose: {purpose} "
        f"employe name {employee_name} and "
        f"Employe emai is {employee_email}\n\n"
        f"Kindly process the reimbursement at the earliest convenience. "
        f"Please let me know if any additional details or documents are required.\n\n"
        f"Thank you for your support.\n\n"
        f"Best regards,\n"
        f"{employee_name}"
    )
    msg.attach(MIMEText(body, "plain"))

    if receipt_bytes and receipt_name:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(receipt_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{receipt_name}"')
        msg.attach(part)

    return msg


def _smtp_send(msg: MIMEMultipart) -> None:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, NOTIFY_TO, msg.as_string())


async def _send_notification_email(**kwargs) -> None:
    """Send intake notification email — best-effort, never raises."""
    if not SMTP_USER or not SMTP_PASS:
        return
    try:
        msg = _build_email(**kwargs)
        await asyncio.to_thread(_smtp_send, msg)
    except Exception as exc:
        print(f"[email] notification failed (non-fatal): {exc}")


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/submit")
async def submit(
    employeeName: str = Form(...),
    employeeEmail: str = Form(...),
    managerEmail: str = Form(""),
    expenseType: str = Form(...),
    vendor: str = Form(...),
    amount: str = Form(...),
    currency: str = Form("INR"),
    date: str = Form(...),
    purpose: str = Form(...),
    receipt: UploadFile | None = File(None),
):
    try:
        amt = float(amount)
    except ValueError:
        raise HTTPException(422, "amount must be a number.")
    if amt <= 0:
        raise HTTPException(422, "amount must be greater than 0.")

    token = _get_token()

    document_attached = False
    attachment_name: str | None = None
    receipt_raw: bytes | None = None
    receipt_original_name: str | None = None

    if receipt and receipt.filename:
        receipt_raw = await receipt.read()
        if receipt_raw:
            receipt_original_name = receipt.filename
            attachment_name = await _upload_to_bucket(token, receipt.filename, receipt_raw)
            document_attached = True

    reason = (
        f"Reimbursement request from {employeeName} ({employeeEmail}).\n"
        f"Business purpose: {purpose}\n"
        f"Vendor: {vendor} | Amount: {currency} {amt:.2f} | Date: {date}"
    )

    case_inputs = {
        "employeeEmail": employeeEmail.strip(),
        "employeeManagerEmail": managerEmail.strip(),
        "expenseVendor": vendor.strip(),
        "expenseDate": date,
        "expenseAmount": amt,
        "expenseCurrency": currency,
        "expenseReason": reason,
        "expenseTypeConfirmed": expenseType,
        "documentAttached": document_attached,
        "ocrConfidence": 1.0,
        "duplicateDetected": False,
        "businessPurposeValid": True,
    }

    job_id = await _start_case(token, case_inputs)

    # Fire notification email — non-blocking, never fails the submission
    asyncio.create_task(_send_notification_email(
        employee_name=employeeName,
        employee_email=employeeEmail,
        expense_type=expenseType,
        currency=currency,
        amount=amt,
        date=date,
        purpose=purpose,
        vendor=vendor,
        receipt_name=receipt_original_name,
        receipt_bytes=receipt_raw,
    ))

    return {
        "case_id": str(uuid.uuid4()),
        "job_id": job_id,
        "attachment": attachment_name,
        "employee": employeeName,
        "amount": amt,
        "currency": currency,
    }


# ── Serve React SPA (dist/ built by Dockerfile / `npm run build`) ─────────────

_DIST = Path(__file__).parent / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="spa")
