###############################################################
#                  TAMLLELI-PRO — FULL SERVER
#            FastAPI · Supabase · RunPod · Team Billing
#                   PART 1 / 3  (DO NOT EDIT)
###############################################################

from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os, threading, time, requests, base64
from urllib.parse import quote, unquote
from supabase import create_client, Client
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

###############################################################
#                          APP INIT
###############################################################

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

###############################################################
#                     ENVIRONMENT VARIABLES
###############################################################

RUNPOD_TOKEN = os.getenv("RUNPOD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")

FALLBACK_LIMIT_DEFAULT = float(os.getenv("FALLBACK_LIMIT_DEFAULT", "0.5"))
RUNPOD_RATE_PER_SEC = float(os.getenv("RUNPOD_RATE_PER_SEC", "0.0002"))

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

BASE_URL = "https://my-transcribe-proxy.onrender.com"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

###############################################################
#                     BASIC UTILITIES
###############################################################

def delete_later(path, delay=3600):
    def _delete():
        time.sleep(delay)
        if os.path.exists(path):
            os.remove(path)
            print(f"[AutoDelete] Removed: {path}")
    threading.Thread(target=_delete, daemon=True).start()


@app.get("/ping")
def ping():
    return {"status": "ok"}


###############################################################
#                TOKEN DECRYPTION (AES-CBC)
###############################################################

def decrypt_token(enc: str) -> str | None:
    try:
        if not ENCRYPTION_KEY:
            return None
        key = ENCRYPTION_KEY.encode("utf-8")
        raw = base64.b64decode(enc)
        iv, ciphertext = raw[:16], raw[16:]
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        decrypted = unpad(cipher.decrypt(ciphertext), AES.block_size)
        return decrypted.decode()
    except Exception as e:
        print("❌ decrypt_token error:", e)
        return None


###############################################################
#                  ACCOUNT HELPERS (PERSONAL)
###############################################################

def get_account(email: str):
    res = (
        supabase.table("accounts")
        .select("*")
        .eq("user_email", email)
        .maybe_single()
        .execute()
    )
    return res.data if hasattr(res, "data") else None


def get_user_token(email: str | None):
    """
    Returns: (token_to_use, using_fallback)
    """
    try:
        if not email:
            if RUNPOD_API_KEY:
                return RUNPOD_API_KEY, True
            return None, True

        acc = get_account(email)
        enc = acc.get("runpod_token_encrypted") if acc else None

        if enc:
            t = decrypt_token(enc)
            if t:
                return t, False

        if RUNPOD_API_KEY:
            return RUNPOD_API_KEY, True

        return None, True

    except Exception as e:
        print("❌ get_user_token:", e)
        return RUNPOD_API_KEY, True


def check_fallback_allowance(email: str):
    row = get_account(email)
    if not row:
        supabase.table("accounts").insert({
            "user_email": email,
            "used_credits": 0.0,
            "limit_credits": FALLBACK_LIMIT_DEFAULT,
        }).execute()
        return True, 0.0, FALLBACK_LIMIT_DEFAULT

    used = float(row.get("used_credits") or 0.0)
    limit = float(row.get("limit_credits") or FALLBACK_LIMIT_DEFAULT)
    return used < limit, used, limit


def add_fallback_usage(email: str, usd: float):
    row = get_account(email)
    used = float(row.get("used_credits") or 0.0)
    new_used = round(used + usd, 6)
    supabase.table("accounts").update({"used_credits": new_used}).eq("user_email", email).execute()
    return new_used


###############################################################
#                     TEAM HELPERS
###############################################################

def decrypt_team_token(enc: str) -> str | None:
    try:
        return decrypt_token(enc)
    except:
        return None


def get_teams_for_member(email: str):
    res = (
        supabase.table("team_members")
        .select("team_id, is_admin, teams(name, owner_email)")
        .eq("user_email", email)
        .execute()
    )
    return res.data or []


def get_team_row(team_id: int):
    res = (
        supabase.table("teams")
        .select("*")
        .eq("id", team_id)
        .maybe_single()
        .execute()
    )
    return res.data


def get_team_members(team_id: int):
    res = (
        supabase.table("team_members")
        .select("user_email, is_admin")
        .eq("team_id", team_id)
        .execute()
    )
    return res.data or []


###############################################################
#                    TEAM API (CRUD)
###############################################################

@app.post("/team/create")
async def team_create(request: Request):
    try:
        body = await request.json()
        name = body.get("name")
        owner = body.get("owner_email")
        token = body.get("runpod_token")
        quota = body.get("base_quota_seconds", 0)

        if not all([name, owner, token]):
            return JSONResponse({"error": "name, owner_email, token required"}, 400)

        if not ENCRYPTION_KEY:
            return JSONResponse({"error": "missing ENCRYPTION_KEY"}, 500)

        key = ENCRYPTION_KEY.encode("utf-8")
        iv = os.urandom(16)
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)

        pad_len = AES.block_size - len(token.encode()) % AES.block_size
        padded = token.encode() + bytes([pad_len]) * pad_len

        encrypted = base64.b64encode(iv + cipher.encrypt(padded)).decode()

        r = supabase.table("teams").insert({
            "name": name,
            "owner_email": owner,
            "runpod_token_encrypted": encrypted,
            "base_quota_seconds": quota,
        }).execute()

        team = r.data[0]

        supabase.table("team_members").insert({
            "team_id": team["id"],
            "user_email": owner,
            "is_admin": True,
        }).execute()

        return JSONResponse({"status": "ok", "team": team})

    except Exception as e:
        print("❌ /team/create:", e)
        return JSONResponse({"error": str(e)}, 500)


@app.post("/team/add-member")
async def team_add_member(request: Request):
    try:
        body = await request.json()
        team_id = body.get("team_id")
        member = body.get("user_email")
        is_admin = body.get("is_admin", False)

        if not all([team_id, member]):
            return JSONResponse({"error": "team_id & user_email required"}, 400)

        supabase.table("team_members").insert({
            "team_id": team_id,
            "user_email": member,
            "is_admin": is_admin,
        }).execute()

        return {"status": "ok"}

    except Exception as e:
        print("❌ /team/add-member:", e)
        return JSONResponse({"error": str(e)}, 500)
###############################################################
#                    TEAM API (CONTINUED)
#                    PART 2 / 3 — DO NOT EDIT
###############################################################

@app.post("/team/remove-member")
async def team_remove_member(request: Request):
    try:
        body = await request.json()
        team_id = body.get("team_id")
        member = body.get("user_email")

        if not all([team_id, member]):
            return JSONResponse({"error": "team_id & user_email required"}, 400)

        supabase.table("team_members") \
            .delete() \
            .eq("team_id", team_id) \
            .eq("user_email", member) \
            .execute()

        return {"status": "deleted"}

    except Exception as e:
        print("❌ /team/remove-member:", e)
        return JSONResponse({"error": str(e)}, 500)


@app.get("/team/info")
async def team_info(team_id: int):
    """
    Returns:
      - team data
      - members
      - usage seconds
    """
    try:
        team = get_team_row(team_id)
        if not team:
            return JSONResponse({"error": "team not found"}, 404)

        members = get_team_members(team_id)

        usage = (
            supabase.table("team_usage")
            .select("*")
            .eq("team_id", team_id)
            .execute()
        ).data or []

        return JSONResponse({
            "team": team,
            "members": members,
            "usage": usage,
        })

    except Exception as e:
        print("❌ /team/info:", e)
        return JSONResponse({"error": str(e)}, 500)


@app.post("/team/update-quota")
async def team_update_quota(request: Request):
    try:
        body = await request.json()
        team_id = body.get("team_id")
        quota = body.get("base_quota_seconds")

        if not all([team_id, quota]):
            return JSONResponse({"error": "team_id & base_quota_seconds required"}, 400)

        supabase.table("teams") \
            .update({"base_quota_seconds": quota}) \
            .eq("id", team_id) \
            .execute()

        return {"status": "ok"}

    except Exception as e:
        print("❌ /team/update-quota:", e)
        return JSONResponse({"error": str(e)}, 500)


###############################################################
#                     PROJECT API
###############################################################

@app.post("/project/create")
async def project_create(request: Request):
    try:
        body = await request.json()
        team_id = body.get("team_id")
        name = body.get("name")
        quota = body.get("quota_seconds", 0)

        if not all([team_id, name]):
            return JSONResponse({"error": "team_id & name required"}, 400)

        r = supabase.table("project").insert({
            "team_id": team_id,
            "name": name,
            "quota_seconds": quota,
        }).execute()

        return {"status": "ok", "project": r.data[0]}

    except Exception as e:
        print("❌ /project/create:", e)
        return JSONResponse({"error": str(e)}, 500)


@app.post("/project/update-quota")
async def project_update_quota(request: Request):
    try:
        body = await request.json()
        project_id = body.get("project_id")
        quota = body.get("quota_seconds")

        if not all([project_id, quota]):
            return JSONResponse({"error": "project_id & quota_seconds required"}, 400)

        supabase.table("project") \
            .update({"quota_seconds": quota}) \
            .eq("id", project_id) \
            .execute()

        return {"status": "ok"}

    except Exception as e:
        print("❌ /project/update-quota:", e)
        return JSONResponse({"error": str(e)}, 500)


@app.get("/project/info")
async def project_info(project_id: int):
    try:
        proj = (
            supabase.table("project")
            .select("*")
            .eq("id", project_id)
            .maybe_single()
            .execute()
        ).data

        if not proj:
            return JSONResponse({"error": "project not found"}, 404)

        usage = (
            supabase.table("project_usage")
            .select("*")
            .eq("project_id", project_id)
            .execute()
        ).data or []

        return {
            "project": proj,
            "usage": usage,
        }

    except Exception as e:
        print("❌ /project/info:", e)
        return JSONResponse({"error": str(e)}, 500)


###############################################################
#                       BILLING RESOLVER
###############################################################

def resolve_billing_mode(email: str) -> dict:
    """
    Determines:
    - personal
    - guest (fallback)
    - team

    Returns dictionary with:
      mode, team_id, project_id, token_to_use, using_fallback
    """

    pref = (
        supabase.table("user_modes")
        .select("*")
        .eq("user_email", email)
        .maybe_single()
        .execute()
    ).data

    if not pref:
        # NEW USER = GUEST
        return {
            "mode": "guest",
            "team_id": None,
            "project_id": None,
            "token_to_use": RUNPOD_API_KEY,
            "using_fallback": True,
            "limit_seconds": FALLBACK_LIMIT_DEFAULT,
        }

    mode = pref.get("preferred_mode", "guest")

    ############################################################
    # PERSONAL MODE
    ############################################################
    if mode == "personal":
        token, fallback = get_user_token(email)
        return {
            "mode": "personal",
            "team_id": None,
            "project_id": None,
            "token_to_use": token,
            "using_fallback": fallback,
            "limit_seconds": None,
        }

    ############################################################
    # GUEST (0.5$ free trial)
    ############################################################
    if mode == "guest":
        return {
            "mode": "guest",
            "team_id": None,
            "project_id": None,
            "token_to_use": RUNPOD_API_KEY,
            "using_fallback": True,
            "limit_seconds": FALLBACK_LIMIT_DEFAULT,
        }

    ############################################################
    # TEAM MODE
    ############################################################
    if mode == "team":
        team_id = pref.get("active_team_id")
        project_id = pref.get("active_project_id")

        if not team_id:
            # fallback → guest
            return {
                "mode": "guest",
                "team_id": None,
                "project_id": None,
                "token_to_use": RUNPOD_API_KEY,
                "using_fallback": True,
                "limit_seconds": FALLBACK_LIMIT_DEFAULT,
            }

        trow = (
            supabase.table("teams")
            .select("*")
            .eq("id", team_id)
            .maybe_single()
            .execute()
        ).data

        if not trow:
            return {
                "mode": "guest",
                "team_id": None,
                "project_id": None,
                "token_to_use": RUNPOD_API_KEY,
                "using_fallback": True,
                "limit_seconds": FALLBACK_LIMIT_DEFAULT,
            }

        enc = trow.get("runpod_token_encrypted")

        if not enc:
            # no team token → fallback
            return {
                "mode": "team",
                "team_id": team_id,
                "project_id": project_id,
                "token_to_use": RUNPOD_API_KEY,
                "using_fallback": True,
                "limit_seconds": None,
            }

        token = decrypt_token(enc)

        return {
            "mode": "team",
            "team_id": team_id,
            "project_id": project_id,
            "token_to_use": token,
            "using_fallback": False,
            "limit_seconds": None,
        }

    ############################################################
    # Default = guest
    ############################################################
    return {
        "mode": "guest",
        "team_id": None,
        "project_id": None,
        "token_to_use": RUNPOD_API_KEY,
        "using_fallback": True,
        "limit_seconds": FALLBACK_LIMIT_DEFAULT,
    }
###############################################################
#                   TRANSCRIPTION API
###############################################################

@app.post("/transcribe")
async def transcribe(request: Request):
    """
    Unified transcription endpoint using:
    - personal token
    - team token
    - guest fallback token
    """
    try:
        body = await request.json()
        user_email = body.get("user_email")
        file_url = body.get("file_url")

        if not user_email:
            return JSONResponse({"error": "user_email is required"}, 400)
        if not file_url:
            return JSONResponse({"error": "file_url is required"}, 400)

        billing = resolve_billing_mode(user_email)
        token_to_use = billing["token_to_use"]

        if not token_to_use:
            return JSONResponse({"error": "No RunPod token available"}, 401)

        # Guest limit check
        if billing["mode"] == "guest":
            allowed, used, limit = check_fallback_allowance(user_email)
            if not allowed:
                return JSONResponse({
                    "error": "Guest limit exceeded",
                    "used": used,
                    "limit": limit
                }, 402)

        # Build request to RunPod
        run_body = {
            "input": {
                "engine": "stable-whisper",
                "model": "ivrit-ai/whisper-large-v3-turbo-ct2",
                "transcribe_args": {
                    "url": file_url,
                    "language": "he",
                    "word_timestamps": True,
                    "diarize": True,
                    "vad": True
                }
            }
        }

        print(f"🚀 Running job on RunPod for user={user_email}, mode={billing['mode']}")

        r = requests.post(
            "https://api.runpod.ai/v2/lco4rijwxicjyi/run",
            json=run_body,
            headers={"Authorization": f"Bearer {token_to_use}"},
            timeout=180,
        )

        out = r.json() if r.content else {}
        out["_billing"] = billing

        return JSONResponse(out, r.status_code)

    except Exception as e:
        print("❌ /transcribe error:", e)
        return JSONResponse({"error": str(e)}, 500)


###############################################################
#                 RUNPOD STATUS + BILLING
###############################################################

def estimate_cost_from_response(out: dict) -> float:
    """
    Extracts USD cost from RunPod COMPLETED job response.
    """
    try:
        usage = out.get("executionTime") or out.get("metrics", {})
        if isinstance(usage, (int, float)):
            # assume seconds
            seconds = usage
        else:
            seconds = usage.get("executionTime", 0)

        return float(seconds) * RUNPOD_RATE_PER_SEC

    except Exception:
        return 0.0


@app.get("/status/{job_id}")
def get_status(job_id: str, user_email: str | None = None):
    """
    Checks job status AND applies billing according to mode.
    """
    try:
        # token selection
        if not user_email:
            token, _ = get_user_token(None)
            billing = None
        else:
            billing = resolve_billing_mode(user_email)
            token = billing["token_to_use"]

        if not token:
            return JSONResponse({"error": "Missing token"}, 401)

        # Fetch from RunPod
        r = requests.get(
            f"https://api.runpod.ai/v2/lco4rijwxicjyi/status/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if not r.ok:
            return JSONResponse({"error": "RunPod error"}, r.status_code)

        out = r.json()

        # Not completed → no billing yet
        if not user_email or out.get("status") != "COMPLETED":
            return JSONResponse(out)

        # Cost calculation
        cost_usd = estimate_cost_from_response(out)
        seconds = cost_usd / RUNPOD_RATE_PER_SEC
        out["_cost_usd"] = cost_usd
        out["_seconds"] = seconds

        ####################################################
        # BILLING HANDLING
        ####################################################

        # 1️⃣ Guest billing
        if billing and billing["mode"] == "guest":
            new_used = add_fallback_usage(user_email, cost_usd)
            out["_billing_guest"] = {"used_now": new_used}

        # 2️⃣ Team billing
        elif billing and billing["mode"] == "team":
            # team usage
            supabase.table("team_usage").insert({
                "team_id": billing["team_id"],
                "user_email": user_email,
                "job_id": job_id,
                "seconds_used": seconds,
            }).execute()

            # project usage (optional)
            if billing["project_id"]:
                supabase.table("project_usage").insert({
                    "project_id": billing["project_id"],
                    "user_email": user_email,
                    "seconds_used": seconds,
                }).execute()

        # 3️⃣ Personal has no internal billing. RunPod charges the user directly.

        out["_mode"] = billing["mode"] if billing else None
        return JSONResponse(out)

    except Exception as e:
        print("❌ /status ERROR:", e)
        return JSONResponse({"error": str(e)}, 500)


###############################################################
#                     USER MODE API
###############################################################

@app.post("/user/set-mode")
async def user_set_mode(request: Request):
    """
    Allows user to select:
      guest | personal | team
    """
    try:
        body = await request.json()
        user_email = body.get("user_email")
        mode = body.get("mode")

        if not user_email or not mode:
            return JSONResponse({"error": "user_email & mode required"}, 400)

        if mode not in ["guest", "personal", "team"]:
            return JSONResponse({"error": "Invalid mode"}, 400)

        row = (
            supabase.table("user_modes")
            .select("*")
            .eq("user_email", user_email)
            .maybe_single()
            .execute()
        ).data

        now = time.strftime("%Y-%m-%dT%H:%M:%S")

        if row:
            supabase.table("user_modes").update({
                "preferred_mode": mode,
                "updated_at": now
            }).eq("user_email", user_email).execute()
        else:
            supabase.table("user_modes").insert({
                "user_email": user_email,
                "preferred_mode": mode,
                "created_at": now,
                "updated_at": now,
            }).execute()

        return {"status": "ok", "mode": mode}

    except Exception as e:
        print("❌ /user/set-mode:", e)
        return JSONResponse({"error": str(e)}, 500)


@app.post("/user/set-team")
async def user_set_team(request: Request):
    """
    Select team + optional project
    """
    try:
        body = await request.json()
        email = body.get("user_email")
        team_id = body.get("team_id")
        project_id = body.get("project_id")

        if not email:
            return JSONResponse({"error": "user_email required"}, 400)

        row = (
            supabase.table("user_modes")
            .select("*")
            .eq("user_email", email)
            .maybe_single()
            .execute()
        ).data

        updates = {
            "active_team_id": team_id,
            "active_project_id": project_id,
            "preferred_mode": "team",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        if row:
            supabase.table("user_modes").update(updates).eq("user_email", email).execute()
        else:
            supabase.table("user_modes").insert({
                "user_email": email,
                **updates,
            }).execute()

        return {"status": "ok"}

    except Exception as e:
        print("❌ /user/set-team:", e)
        return JSONResponse({"error": str(e)}, 500)


@app.get("/user/mode")
async def user_get_mode(user_email: str):
    """
    Returns full current mode with optional team/project info
    """
    try:
        row = (
            supabase.table("user_modes")
            .select("*")
            .eq("user_email", user_email)
            .maybe_single()
            .execute()
        ).data

        if not row:
            return {
                "preferred_mode": "guest",
                "active_team_id": None,
                "active_project_id": None,
            }

        resp = {
            "preferred_mode": row.get("preferred_mode"),
            "active_team_id": row.get("active_team_id"),
            "active_project_id": row.get("active_project_id"),
        }

        if row.get("active_team_id"):
            resp["team"] = (
                supabase.table("teams")
                .select("id, name, owner_email")
                .eq("id", row["active_team_id"])
                .maybe_single()
                .execute()
            ).data

        if row.get("active_project_id"):
            resp["project"] = (
                supabase.table("project")
                .select("id, name, quota_seconds")
                .eq("id", row["active_project_id"])
                .maybe_single()
                .execute()
            ).data

        return resp

    except Exception as e:
        print("❌ /user/mode:", e)
        return JSONResponse({"error": str(e)}, 500)


###############################################################
#                       END OF FILE
###############################################################
