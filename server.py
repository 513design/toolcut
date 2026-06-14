#!/usr/bin/env python3
"""
server.py — multi-tenant toolcut API (Supabase-only: auth + database + storage).

Auth:    Supabase JWT in the `Authorization: Bearer <token>` header.
         Verifies ES256/RS256 against the project JWKS endpoint, and falls
         back to the legacy HS256 shared secret if SUPABASE_JWT_SECRET is set.
Storage: outputs go to Supabase Storage under per-user folders; clients get
         time-limited signed URLs.
Records: every run is written to the Supabase `jobs` table (see schema.sql).

Required env (see .env.example):
  SUPABASE_URL                e.g. https://abcd1234.supabase.co
  SUPABASE_SERVICE_ROLE_KEY   service role / secret key (backend-only)
  SUPABASE_BUCKET             storage bucket name (default: cutouts)
  SUPABASE_JWT_SECRET         optional — only for legacy HS256 projects
"""

import os
import uuid
import tempfile

import cv2
import httpx
import jwt
import numpy as np
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from jwt import PyJWKClient

import toolcut

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")          # legacy HS256, optional
BUCKET = os.environ.get("SUPABASE_BUCKET", "cutouts")

MAX_BYTES = 25 * 1024 * 1024
URL_TTL = 3600                                              # signed URL lifetime (s)
MONTHLY_QUOTA = int(os.environ.get("MONTHLY_QUOTA", "0"))   # 0 = unlimited

CONTENT_TYPES = {".svg": "image/svg+xml", ".dxf": "application/dxf",
                 ".png": "image/png"}

app = FastAPI(title="toolcut", version="2.1")

# Allow browser-based clients (the web app) to call this API.
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

_jwk_client = PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json") \
    if SUPABASE_URL else None

_auth_headers = {"apikey": SERVICE_ROLE_KEY,
                 "Authorization": f"Bearer {SERVICE_ROLE_KEY}"}
_rest = httpx.Client(base_url=f"{SUPABASE_URL}/rest/v1",
                     headers={**_auth_headers, "Content-Type": "application/json"},
                     timeout=10) if SUPABASE_URL else None
_storage = httpx.Client(base_url=f"{SUPABASE_URL}/storage/v1",
                        headers=_auth_headers, timeout=30) if SUPABASE_URL else None


# --------------------------------------------------------------------------
# Auth — verify a Supabase access token and return the user id (sub)
# --------------------------------------------------------------------------
def verify_jwt(token: str) -> dict:
    try:
        alg = jwt.get_unverified_header(token).get("alg")
        if alg in ("ES256", "RS256", "EdDSA"):
            key = _jwk_client.get_signing_key_from_jwt(token).key
            return jwt.decode(token, key, algorithms=[alg], audience="authenticated")
        if alg == "HS256":
            if not JWT_SECRET:
                raise HTTPException(401, "HS256 token but no SUPABASE_JWT_SECRET set")
            return jwt.decode(token, JWT_SECRET, algorithms=["HS256"],
                              audience="authenticated")
        raise HTTPException(401, f"unsupported token alg: {alg}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(401, f"invalid token: {e}")


def current_user(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    claims = verify_jwt(authorization.split(" ", 1)[1])
    uid = claims.get("sub")
    if not uid:
        raise HTTPException(401, "token has no subject")
    return uid


# --------------------------------------------------------------------------
# Supabase database (jobs table). Service role bypasses RLS; we set user_id.
# --------------------------------------------------------------------------
def create_job(user_id: str, paper: str) -> str:
    job_id = str(uuid.uuid4())
    r = _rest.post("/jobs", json={"id": job_id, "user_id": user_id,
                                  "paper": paper, "status": "processing"})
    r.raise_for_status()
    return job_id


def update_job(job_id: str, fields: dict):
    r = _rest.patch(f"/jobs?id=eq.{job_id}", json=fields)
    r.raise_for_status()


def month_count(user_id: str) -> int:
    r = _rest.get(f"/jobs?user_id=eq.{user_id}&status=eq.done"
                  "&created_at=gte.now()-interval'30 days'",
                  headers={"Prefer": "count=exact", "Range": "0-0"})
    r.raise_for_status()
    return int(r.headers.get("content-range", "*/0").split("/")[-1])


# --------------------------------------------------------------------------
# Supabase Storage under per-user folders
# --------------------------------------------------------------------------
def store_and_sign(user_id: str, job_id: str, files):
    """files = {label: local_path}. Returns {label: (object_path, signed_url)}."""
    out = {}
    for label, path in files.items():
        ext = os.path.splitext(path)[1]
        object_path = f"{user_id}/{job_id}{ext}"
        with open(path, "rb") as fh:
            data = fh.read()
        up = _storage.post(
            f"/object/{BUCKET}/{object_path}", content=data,
            headers={"Content-Type": CONTENT_TYPES.get(ext, "application/octet-stream"),
                     "x-upsert": "true"})
        up.raise_for_status()
        sg = _storage.post(f"/object/sign/{BUCKET}/{object_path}",
                           json={"expiresIn": URL_TTL})
        sg.raise_for_status()
        url = f"{SUPABASE_URL}/storage/v1{sg.json()['signedURL']}"
        out[label] = (object_path, url)
    return out


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True, "service": "toolcut", "version": "2.1"}


@app.get("/jobs")
def list_jobs(user_id: str = Depends(current_user)):
    r = _rest.get(f"/jobs?user_id=eq.{user_id}&order=created_at.desc&limit=50")
    r.raise_for_status()
    return r.json()


@app.post("/process")
def process(
    image: UploadFile = File(...),
    paper: str = Form("letter"),
    clearance_mm: float = Form(1.5),
    simplify_mm: float = Form(0.6),
    finger_holes: bool = Form(False),
    finger_dia_mm: float = Form(35.0),
    px_per_mm: float = Form(8.0),
    debug: bool = Form(False),
    user_id: str = Depends(current_user),
):
    if paper not in toolcut.PAPER:
        raise HTTPException(400, f"paper must be one of {list(toolcut.PAPER)}")
    if MONTHLY_QUOTA and month_count(user_id) >= MONTHLY_QUOTA:
        raise HTTPException(429, f"monthly quota of {MONTHLY_QUOTA} reached")

    raw = image.file.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "image too large (max 25 MB)")
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "could not decode image")

    job_id = create_job(user_id, paper)
    try:
        with tempfile.TemporaryDirectory() as td:
            dbg = os.path.join(td, "debug.png") if debug else None
            poly, rawb = toolcut.run(img, paper, px_per_mm, clearance_mm,
                                     simplify_mm, finger_holes, finger_dia_mm,
                                     debug_path=dbg)
            svg_p = os.path.join(td, "cutout.svg")
            dxf_p = os.path.join(td, "cutout.dxf")
            toolcut.export_svg(poly, svg_p)
            toolcut.export_dxf(poly, dxf_p)
            files = {"svg": svg_p, "dxf": dxf_p}
            if dbg:
                files["debug"] = dbg
            stored = store_and_sign(user_id, job_id, files)

        minx, miny, maxx, maxy = poly.bounds
        fields = {
            "status": "done",
            "tool_w_mm": round(rawb[2] - rawb[0], 1),
            "tool_h_mm": round(rawb[3] - rawb[1], 1),
            "cutout_w_mm": round(maxx - minx, 1),
            "cutout_h_mm": round(maxy - miny, 1),
            "svg_key": stored["svg"][0],
            "dxf_key": stored["dxf"][0],
            "debug_key": stored.get("debug", (None,))[0],
        }
        update_job(job_id, fields)
        return {
            "job_id": job_id,
            "tool_mm": [fields["tool_w_mm"], fields["tool_h_mm"]],
            "cutout_mm": [fields["cutout_w_mm"], fields["cutout_h_mm"]],
            "urls": {k: v[1] for k, v in stored.items()},
            "url_expires_in": URL_TTL,
        }
    except cv2.error as e:
        update_job(job_id, {"status": "error", "error": str(e)[:300]})
        raise HTTPException(500, "processing failed")
    except RuntimeError as e:
        update_job(job_id, {"status": "error", "error": str(e)[:300]})
        raise HTTPException(422, str(e))
