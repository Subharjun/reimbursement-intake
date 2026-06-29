"""
Reimbursement Intake API — FastAPI backend

Receives the expense form, uploads the receipt to the UiPath storage bucket,
then starts the Maestro Case via the Orchestrator REST API (no uip CLI needed).

Local dev:
    pip install -r requirements.txt
    uvicorn api:app --reload --port 8000

Render / production:
    UIPATH_ACCESS_TOKEN=<token>  # run `uip auth token` locally to get it
    The React dist/ is built by the Dockerfile and served at / by this process.
"""

import json
import os
import re
import uuid
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

# Numeric folder id for the AgentHack team folder (bucket operations)
UIPATH_FOLDER_ID = os.getenv("UIPATH_FOLDER_ID", "3054578")

# Numeric folder id for the Maestro Case subfolder (job start)
# Shared/UiPath AgentHack_2026/ReimbursementProcessMaestro  id=3154132
UIPATH_CASE_FOLDER_NUMERIC_ID = os.getenv("UIPATH_CASE_FOLDER_NUMERIC_ID", "3154132")

BUCKET_ID = int(os.getenv("UIPATH_BUCKET_ID", "199727"))

CASE_RELEASE_KEY = os.getenv(
    "UIPATH_CASE_RELEASE_KEY",
    "8602dfb7-4304-4768-b853-544f1ef7d972",
)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_token() -> str:
    """Return UiPath bearer token — env var first, local auth file as dev fallback."""
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
            headers={"Content-Type": "application/octet-stream"},
        )
        if put_r.status_code not in (200, 201, 204):
            raise HTTPException(502, f"Bucket PUT failed: {put_r.status_code} {put_r.text[:300]}")
    return safe_name


# ── Maestro Case trigger ──────────────────────────────────────────────────────

async def _start_case(token: str, inputs: dict) -> str | None:
    """
    Start the Maestro Case via the Orchestrator OData Jobs REST API.
    No uip CLI dependency — pure httpx.
    """
    url = (
        f"{UIPATH_BASE_URL}/orchestrator_/odata/Jobs"
        "/UiPath.Server.Configuration.OData.StartJobs"
    )
    body = {
        "startInfo": {
            "ReleaseKey": CASE_RELEASE_KEY,
            "Strategy": "All",
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
    if receipt and receipt.filename:
        raw = await receipt.read()
        if raw:
            attachment_name = await _upload_to_bucket(token, receipt.filename, raw)
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
