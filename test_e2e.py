#!/usr/bin/env python3
"""
test_e2e.py — prove the full pipeline end to end.

Reads SUPABASE_URL + the service key from /opt/toolcut/.env, creates a
pre-confirmed test user (so there's no email-confirmation hoop), signs in to
get a REAL Supabase access token, then pushes a photo through the live
/process endpoint. Watch for a 200 with signed URLs.

Run on the server:
    sudo /opt/toolcut/venv/bin/python3 test_e2e.py test.jpg
"""

import json
import sys

import httpx

ENV_PATH = "/opt/toolcut/.env"
EMAIL = "e2e-test@toolcut.local"
PASSWORD = "ToolcutTest123!"
IMG = sys.argv[1] if len(sys.argv) > 1 else "test.jpg"


def load_env(path):
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


env = load_env(ENV_PATH)
URL = env["SUPABASE_URL"].rstrip("/")
KEY = env["SUPABASE_SERVICE_ROLE_KEY"]
ADMIN = {"apikey": KEY, "Authorization": f"Bearer {KEY}",
         "Content-Type": "application/json"}

# 1) Create a pre-confirmed test user (fine if it already exists).
r = httpx.post(f"{URL}/auth/v1/admin/users", headers=ADMIN,
               json={"email": EMAIL, "password": PASSWORD, "email_confirm": True},
               timeout=30)
if r.status_code in (200, 201):
    print("created test user \u2714")
else:
    print(f"test user already exists or non-fatal ({r.status_code}) \u2014 continuing")

# 2) Sign in with that user to get a real access token.
r = httpx.post(f"{URL}/auth/v1/token?grant_type=password",
               headers={"apikey": KEY, "Content-Type": "application/json"},
               json={"email": EMAIL, "password": PASSWORD}, timeout=30)
token = r.json().get("access_token")
if not token:
    print("FAILED to sign in:", r.status_code, r.text)
    sys.exit(1)
print("signed in, got access token \u2714")

# 3) Push the photo through the live API exactly as a real client would.
try:
    with open(IMG, "rb") as f:
        r = httpx.post("http://localhost:8000/process",
                       headers={"Authorization": f"Bearer {token}"},
                       files={"image": (IMG, f, "image/jpeg")},
                       data={"finger_holes": "true", "debug": "true"},
                       timeout=120)
except FileNotFoundError:
    print(f"FAILED: image file '{IMG}' not found in this folder.")
    sys.exit(1)

print("process status:", r.status_code)
try:
    out = r.json()
    print(json.dumps(out, indent=2))
except Exception:
    print(r.text)

if r.status_code == 200:
    print("\nSUCCESS \u2714  Open the 'svg' or 'debug' URL above in a browser to see the cutout.")
    print("Then check Supabase: the 'jobs' table has a new row and 'cutouts' has files.")
elif r.status_code == 422:
    print("\nThe API worked and auth passed, but the engine couldn't find the paper")
    print("or tool in your photo. Retake it: a dark tool flat on a full sheet of white")
    print("printer paper, on a darker surface, shot from straight above. Then rerun.")
