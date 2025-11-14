from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os, threading, time, requests
from urllib.parse import quote, unquote
import base64
from supabase import create_client, Client
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# ───────────────────────────────────────────────
app = FastAPI()

# ✅ CORS – פתוח לכל
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ───────────────────────────────────────────────
# 🔧 משתני סביבה
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

# חיבור ל-Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ───────────────────────────────────────────────
def delete_later(path, delay=3600):
    """מוחק קובץ אחרי delay שניות."""
    def _delete():
        time.sleep(delay)
        if os.path.exists(path):
            os.remove(path)
            print(f"[Auto Delete] נמחק הקובץ: {path}")
    threading.Thread(target=_delete, daemon=True).start()

@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    return JSONResponse({"status": "ok"})

# ───────────────────────────────────────────────
# 🧩 פענוח AES (לטוקן אישי בלבד)
def decrypt_token(encrypted_token: str) -> str | None:
    try:
        if not ENCRYPTION_KEY:
            return None
        key = ENCRYPTION_KEY.encode("utf-8")
        data = base64.b64decode(encrypted_token)
        iv, ciphertext = data[:16], data[16:]
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        decrypted = unpad(cipher.decrypt(ciphertext), AES.block_size)
        return decrypted.decode("utf-8")
    except Exception as e:
        print(f"❌ שגיאה בפענוח טוקן: {e}")
        return None

# ───────────────────────────────────────────────
# ⭐ פונקציות עזר לחשבונות
def get_account(user_email: str):
    res = (
        supabase.table("accounts")
        .select("user_email, runpod_token_encrypted, used_credits, limit_credits")
        .eq("user_email", user_email)
        .maybe_single()
        .execute()
    )
    return res.data if hasattr(res, "data") else None

def get_user_token(user_email: str | None) -> tuple[str | None, bool]:
    """
    מחזיר (token_to_use, using_fallback).
    """
    try:
        if not user_email:
            if RUNPOD_API_KEY:
                return RUNPOD_API_KEY, True
            return None, True

        row = get_account(user_email)
        enc = row.get("runpod_token_encrypted") if row else None

        if enc:
            token = decrypt_token(enc)
            if token:
                return token, False

        if RUNPOD_API_KEY:
            return RUNPOD_API_KEY, True

        return None, True
    except Exception as e:
        print(f"❌ שגיאה בשליפת טוקן: {e}")
        return (RUNPOD_API_KEY if RUNPOD_API_KEY else None), True

def check_fallback_allowance(user_email: str):
    """
    בודק יתרת fallback.
    """
    row = get_account(user_email)
    if not row:
        payload = {
            "user_email": user_email,
            "used_credits": 0.0,
            "limit_credits": FALLBACK_LIMIT_DEFAULT,
        }
        supabase.table("accounts").insert(payload).execute()
        return True, 0.0, FALLBACK_LIMIT_DEFAULT

    used = float(row.get("used_credits") or 0.0)
    limit = float(row.get("limit_credits") or FALLBACK_LIMIT_DEFAULT)
    return (used < limit), used, limit

def add_fallback_usage(user_email: str, amount_usd: float):
    row = get_account(user_email)
    used = float((row or {}).get("used_credits") or 0.0)
    new_used = round(used + amount_usd, 6)
    supabase.table("accounts").update({"used_credits": new_used}).eq("user_email", user_email).execute()
    return new_used

# ───────────────────────────────────────────────
# ⭐ פונקציות עזר לקבוצות
def decrypt_team_token(enc: str) -> str | None:
    try:
        if not ENCRYPTION_KEY:
            return None
        key = ENCRYPTION_KEY.encode("utf-8")
        data = base64.b64decode(enc)
        iv, ciphertext = data[:16], data[16:]
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        decrypted = unpad(cipher.decrypt(ciphertext), AES.block_size)
        return decrypted.decode("utf-8")
    except Exception as e:
        print("❌ שגיאה בפענוח טוקן קבוצה:", e)
        return None

def get_teams_for_member(user_email: str):
    res = (
        supabase.table("team_members")
        .select("team_id, is_admin, teams(name, owner_email)")
        .eq("user_email", user_email)
        .execute()
    )
    return res.data or []

def get_team_by_id(team_id: int):
    res = (
        supabase.table("teams")
        .select("*")
        .eq("id", team_id)
        .maybe_single()
        .execute()
    )
    return res.data if hasattr(res, "data") else None

def get_team_members(team_id: int):
    res = (
        supabase.table("team_members")
        .select("user_email, is_admin")
        .eq("team_id", team_id)
        .execute()
    )
    return res.data or []
# ───────────────────────────────────────────────
# 🟦 TEAM API — יצירת קבוצה, חברים, טוקן, מכסות
# ───────────────────────────────────────────────

@app.post("/team/create")
async def team_create(request: Request):
    """
    יצירת קבוצה חדשה:
    - name
    - owner_email
    - runpod_token (לא מוצפן מהקליינט)
    - base_quota_seconds
    """
    try:
        body = await request.json()
        name = body.get("name")
        owner = body.get("owner_email")
        token = body.get("runpod_token")
        quota = body.get("base_quota_seconds", 0)

        if not all([name, owner, token]):
            return JSONResponse({"error": "חובה לספק name, owner_email ו-runpod_token"}, status_code=400)

        if not ENCRYPTION_KEY:
            return JSONResponse({"error": "שרת ללא ENCRYPTION_KEY – לא ניתן להצפין טוקן"}, status_code=500)

        # הצפנה
        key = ENCRYPTION_KEY.encode("utf-8")
        iv = os.urandom(16)
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        padding_len = AES.block_size - len(token.encode()) % AES.block_size
        padded = token.encode() + bytes([padding_len]) * padding_len
        encrypted = base64.b64encode(iv + cipher.encrypt(padded)).decode()

        # יצירת הקבוצה
        r1 = supabase.table("teams").insert({
            "name": name,
            "owner_email": owner,
            "runpod_token_encrypted": encrypted,
            "base_quota_seconds": quota
        }).execute()

        team = r1.data[0]
        team_id = team["id"]

        # הכנסת הבעלים כחבר־על
        supabase.table("team_members").insert({
            "team_id": team_id,
            "user_email": owner,
            "is_admin": True
        }).execute()

        return JSONResponse({"status": "ok", "team": team})
    except Exception as e:
        print("❌ /team/create:", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/team/add-member")
async def team_add_member(request: Request):
    """
    הוספת משתמש לקבוצה:
    - team_id
    - user_email
    - is_admin
    """
    try:
        body = await request.json()
        team_id = body.get("team_id")
        member = body.get("user_email")
        is_admin = body.get("is_admin", False)

        if not all([team_id, member]):
            return JSONResponse({"error": "חסר team_id או user_email"}, status_code=400)

        supabase.table("team_members").insert({
            "team_id": team_id,
            "user_email": member,
            "is_admin": is_admin
        }).execute()

        return JSONResponse({"status": "ok"})
    except Exception as e:
        print("❌ /team/add-member:", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/team/remove-member")
async def team_remove_member(request: Request):
    """
    הסרת משתמש מקבוצה
    """
    try:
        body = await request.json()
        team_id = body.get("team_id")
        member = body.get("user_email")

        if not all([team_id, member]):
            return JSONResponse({"error": "חסר team_id או user_email"}, status_code=400)

        supabase.table("team_members").delete().eq("team_id", team_id).eq("user_email", member).execute()

        return JSONResponse({"status": "deleted"})
    except Exception as e:
        print("❌ /team/remove-member:", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/team/info")
async def team_info(team_id: int):
    """
    מחזיר:
    - פרטי קבוצה
    - חברים
    - שימושים (team_usage)
    """
    try:
        team = get_team_by_id(team_id)
        if not team:
            return JSONResponse({"error": "קבוצה לא קיימת"}, status_code=404)

        members = get_team_members(team_id)

        usage_res = (
            supabase.table("team_usage")
            .select("user_email, seconds_used")
            .eq("team_id", team_id)
            .execute()
        )
        usage = usage_res.data or []

        return JSONResponse({
            "team": team,
            "members": members,
            "usage": usage
        })
    except Exception as e:
        print("❌ /team/info:", e)
        return JSONResponse({"error": str(e)}, status_code=500)



@app.post("/team/update-quota")
async def team_update_quota(request: Request):
    """
    עדכון מכסת שניות בסיס בקבוצה (base_quota_seconds)
    """
    try:
        body = await request.json()
        team_id = body.get("team_id")
        quota = body.get("base_quota_seconds")

        if not all([team_id, quota]):
            return JSONResponse({"error": "חסר team_id או base_quota_seconds"}, status_code=400)

        supabase.table("teams").update({
            "base_quota_seconds": quota
        }).eq("id", team_id).execute()

        return JSONResponse({"status": "ok"})
    except Exception as e:
        print("❌ /team/update-quota:", e)
        return JSONResponse({"error": str(e)}, status_code=500)

# ───────────────────────────────────────────────
# 🟩 PROJECT API — ניהול פרויקטים בקבוצות
# ───────────────────────────────────────────────

@app.post("/project/create")
async def project_create(request: Request):
    """
    יצירת פרויקט חדש בתוך קבוצה:
    - team_id
    - name
    - quota_seconds (כמה שניות מוקצבות לפרויקט)
    """
    try:
        body = await request.json()
        team_id = body.get("team_id")
        name = body.get("name")
        quota = body.get("quota_seconds", 0)

        if not all([team_id, name]):
            return JSONResponse({"error": "חסר team_id או name"}, status_code=400)

        res = supabase.table("project").insert({
            "team_id": team_id,
            "name": name,
            "quota_seconds": quota
        }).execute()

        return JSONResponse({"status": "ok", "project": res.data[0]})
    except Exception as e:
        print("❌ /project/create:", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/project/update-quota")
async def project_update_quota(request: Request):
    """
    עדכון מכסת שניות לפרויקט
    """
    try:
        body = await request.json()
        project_id = body.get("project_id")
        quota = body.get("quota_seconds")

        if not all([project_id, quota]):
            return JSONResponse({"error": "חסר project_id או quota_seconds"}, status_code=400)

        supabase.table("project").update({
            "quota_seconds": quota
        }).eq("id", project_id).execute()

        return JSONResponse({"status": "ok"})
    except Exception as e:
        print("❌ /project/update-quota:", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/project/info")
async def project_info(project_id: int):
    """
    מחזיר:
    - פרטי פרויקט
    - שימוש לפי משתמשים
    """
    try:
        proj = (
            supabase.table("project")
            .select("*")
            .eq("id", project_id)
            .maybe_single()
            .execute()
        ).data

        if not proj:
            return JSONResponse({"error": "פרויקט לא נמצא"}, status_code=404)

        usage = (
            supabase.table("project_usage")
            .select("user_email, seconds_used")
            .eq("project_id", project_id)
            .execute()
        ).data or []

        return JSONResponse({
            "project": proj,
            "usage": usage
        })

    except Exception as e:
        print("❌ /project/info:", e)
        return JSONResponse({"error": str(e)}, status_code=500})
# ───────────────────────────────────────────────
# 🟥 BILLING RESOLVER — לוגיקת חיוב משולבת
# ───────────────────────────────────────────────

def resolve_billing_mode(user_email: str) -> dict:
    """
    קובע האם המשתמש מחויב כ:
    - personal   (טוקן אישי שלו)
    - guest      (0.5$ fallback)
    - team       (טוקן של ראש הקבוצה)

    מחזיר:
    {
      "mode": "personal" | "guest" | "team",
      "team_id": int | None,
      "project_id": int | None,
      "token_to_use": str | None,
      "using_fallback": bool,
      "limit_seconds": float | None
    }
    """

    pref = (
        supabase.table("user_modes")
        .select("*")
        .eq("user_email", user_email)
        .maybe_single()
        .execute()
    ).data

    # ברירת מחדל: אורח
    if not pref:
        return {
            "mode": "guest",
            "team_id": None,
            "project_id": None,
            "token_to_use": RUNPOD_API_KEY,
            "using_fallback": True,
            "limit_seconds": FALLBACK_LIMIT_DEFAULT,
        }

    mode = pref.get("preferred_mode", "guest")

    # 1️⃣ מצב אישי
    if mode == "personal":
        token, use_fallback = get_user_token(user_email)
        return {
            "mode": "personal",
            "team_id": None,
            "project_id": None,
            "token_to_use": token,
            "using_fallback": use_fallback,
            "limit_seconds": None,
        }

    # 2️⃣ מצב אורח
    if mode == "guest":
        return {
            "mode": "guest",
            "team_id": None,
            "project_id": None,
            "token_to_use": RUNPOD_API_KEY,
            "using_fallback": True,
            "limit_seconds": FALLBACK_LIMIT_DEFAULT,
        }

    # 3️⃣ מצב קבוצה
    if mode == "team":
        team_id = pref.get("active_team_id")
        project_id = pref.get("active_project_id")

        if not team_id:
            # אין קבוצה → אורח
            return {
                "mode": "guest",
                "team_id": None,
                "project_id": None,
                "token_to_use": RUNPOD_API_KEY,
                "using_fallback": True,
                "limit_seconds": FALLBACK_LIMIT_DEFAULT,
            }

        trow = (
            supabase.table("team")
            .select("owner_email, runpod_token_encrypted")
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
            # לראש הקבוצה אין טוקן → נופל ל־fallback
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

    # הגנה
    return {
        "mode": "guest",
        "team_id": None,
        "project_id": None,
        "token_to_use": RUNPOD_API_KEY,
        "using_fallback": True,
        "limit_seconds": FALLBACK_LIMIT_DEFAULT,
    }


# ───────────────────────────────────────────────
# 📌 תמלול — כעת לפי מצב המשתמש (פרטי / קבוצה / אורח)
# ───────────────────────────────────────────────

@app.post("/transcribe")
async def transcribe(request: Request):
    try:
        data = await request.json()
        user_email = data.get("user_email")

        if not user_email:
            return JSONResponse({"error": "user_email is required"}, status_code=400)

        billing = resolve_billing_mode(user_email)
        token_to_use = billing["token_to_use"]
        using_fallback = billing["using_fallback"]

        if not token_to_use:
            return JSONResponse({
                "error": "לא מוגדר טוקן לשימוש",
                "mode": billing["mode"]
            }, status_code=401)

        # מגבלת אורח
        if billing["mode"] == "guest":
            allowed, used, limit = check_fallback_allowance(user_email)
            if not allowed:
                return JSONResponse({
                    "error": "חריגה ממגבלת אורח (0.5$)",
                    "used": used,
                    "limit": limit
                }, status_code=402)

        # הכנת בקשה לתמלול
        run_body = data
        if "input" not in data and data.get("file_url"):
            run_body = {
                "input": {
                    "engine": "stable-whisper",
                    "model": "ivrit-ai/whisper-large-v3-turbo-ct2",
                    "transcribe_args": {
                        "url": data["file_url"],
                        "language": "he",
                        "diarize": True,
                        "vad": True,
                        "word_timestamps": True,
                    },
                }
            }

        response = requests.post(
            "https://api.runpod.ai/v2/lco4rijwxicjyi/run",
            headers={"Authorization": f"Bearer {token_to_use}", "Content-Type": "application/json"},
            json=run_body,
            timeout=180,
        )

        out = response.json() if response.content else {}
        status_code = response.status_code or 200

        out["_billing"] = billing

        print(f"🚀 /transcribe: user={user_email} mode={billing['mode']}")
        return JSONResponse(out, status_code=status_code)

    except Exception as e:
        print("❌ /transcribe:", e)
        return JSONResponse({"error": str(e)}, status_code=500)


# ───────────────────────────────────────────────
# 📌 סטטוס — מעדכן חיוב לפי Personal / Guest / Team
# ───────────────────────────────────────────────

@app.get("/status/{job_id}")
def get_job_status(job_id: str, user_email: str | None = None):
    try:
        if not user_email:
            token, _ = get_user_token(None)
            if not token:
                return JSONResponse({"error": "Missing token"}, status_code=401)
            billing = None
        else:
            billing = resolve_billing_mode(user_email)
            token = billing["token_to_use"]

        r = requests.get(
            f"https://api.runpod.ai/v2/lco4rijwxicjyi/status/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

        if not r.ok:
            return JSONResponse({"error": "שגיאה בשליפת סטטוס מ-RunPod"}, status_code=r.status_code)

        out = r.json()

        # רק אם הושלם — מבצעים חיוב
        if not user_email or out.get("status") != "COMPLETED":
            return JSONResponse(out)

        cost = estimate_cost_from_response(out)
        seconds = cost / RUNPOD_RATE_PER_SEC

        # אורח — fallback
        if billing["mode"] == "guest":
            new_used = add_fallback_usage(user_email, cost)
            out["_usage"] = {"fallback_used": new_used}

        # קבוצה
        elif billing["mode"] == "team":
            supabase.table("team_usage").insert({
                "team_id": billing["team_id"],
                "user_email": user_email,
                "job_id": job_id,
                "seconds_used": seconds,
            }).execute()

            # אם חלק מפרויקט
            if billing["project_id"]:
                supabase.table("project_usage").insert({
                    "project_id": billing["project_id"],
                    "user_email": user_email,
                    "seconds_used": seconds,
                }).execute()

        # מצב אישי → אין לנו חיוב פנימי, RunPod מחייב ישירות

        out["_cost_usd"] = cost
        out["_seconds"] = seconds
        out["_mode"] = billing["mode"] if billing else None

        return JSONResponse(out)

    except Exception as e:
        print("❌ /status error:", e)
        return JSONResponse({"error": str(e)}, status_code=500)
# ───────────────────────────────────────────────
# 🟦 USER MODE API — שינוי מצב המשתמש
# ───────────────────────────────────────────────

@app.post("/user/set-mode")
async def user_set_mode(request: Request):
    """
    עדכון preferred_mode למשתמש:
    "personal" | "guest" | "team"

    בקשה:
    {
        "user_email": "...",
        "mode": "guest" | "personal" | "team"
    }
    """
    try:
        body = await request.json()
        user_email = body.get("user_email")
        mode = body.get("mode")

        if not user_email or not mode:
            return JSONResponse({"error": "user_email או mode חסרים"}, status_code=400)

        if mode not in ["guest", "personal", "team"]:
            return JSONResponse({"error": "Mode לא חוקי"}, status_code=400)

        # בדיקה אם יש רשומה קיימת
        row = (
            supabase.table("user_modes")
            .select("*")
            .eq("user_email", user_email)
            .maybe_single()
            .execute()
        ).data

        if row:
            supabase.table("user_modes").update({
                "preferred_mode": mode,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")
            }).eq("user_email", user_email).execute()

        else:
            supabase.table("user_modes").insert({
                "user_email": user_email,
                "preferred_mode": mode
            }).execute()

        return JSONResponse({"status": "ok", "mode": mode})

    except Exception as e:
        print("❌ /user/set-mode:", e)
        return JSONResponse({"error": str(e)}, status_code=500)



# ───────────────────────────────────────────────
# 🟦 USER: עדכון קבוצה ופרויקט פעילים למשתמש
# ───────────────────────────────────────────────

@app.post("/user/set-team")
async def user_set_team(request: Request):
    """
    שינוי הקבוצה הפעילה של המשתמש.
    
    בקשה:
    {
        "user_email": "...",
        "team_id": 123 | null,
        "project_id": 456 | null
    }
    """
    try:
        body = await request.json()
        user_email = body.get("user_email")
        team_id = body.get("team_id")
        project_id = body.get("project_id")

        if not user_email:
            return JSONResponse({"error": "user_email חסר"}, status_code=400)

        # שולפים user_modes
        row = (
            supabase.table("user_modes")
            .select("*")
            .eq("user_email", user_email)
            .maybe_single()
            .execute()
        ).data

        updates = {
            "active_team_id": team_id,
            "active_project_id": project_id,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")
        }

        if row:
            supabase.table("user_modes").update(updates).eq("user_email", user_email).execute()
        else:
            supabase.table("user_modes").insert({
                "user_email": user_email,
                "preferred_mode": "team",   # אם בוחר קבוצה → מצב user הופך team
                "active_team_id": team_id,
                "active_project_id": project_id
            }).execute()

        return JSONResponse({"status": "ok"})

    except Exception as e:
        print("❌ /user/set-team error:", e)
        return JSONResponse({"error": str(e)}, status_code=500)



# ───────────────────────────────────────────────
# 🟦 USER: שליפת מצב משתמש מלא (לקליינט)
# ───────────────────────────────────────────────

@app.get("/user/mode")
async def user_get_mode(user_email: str):
    """
    מחזיר:
    {
        preferred_mode,
        active_team_id,
        active_project_id,
        team_info?,
        project_info?
    }
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
            return JSONResponse({
                "preferred_mode": "guest",
                "active_team_id": None,
                "active_project_id": None
            })

        resp = {
            "preferred_mode": row.get("preferred_mode"),
            "active_team_id": row.get("active_team_id"),
            "active_project_id": row.get("active_project_id")
        }

        # מוסיפים מידע על קבוצה
        if row.get("active_team_id"):
            team = (
                supabase.table("team")
                .select("id, name, owner_email")
                .eq("id", row["active_team_id"])
                .maybe_single()
                .execute()
            ).data
            resp["team"] = team

        # מוסיפים מידע על פרויקט
        if row.get("active_project_id"):
            proj = (
                supabase.table("project")
                .select("id, name, quota_seconds")
                .eq("id", row["active_project_id"])
                .maybe_single()
                .execute()
            ).data
            resp["project"] = proj

        return JSONResponse(resp)

    except Exception as e:
        print("❌ /user/mode error:", e)
        return JSONResponse({"error": str(e)}, status_code=500)



# ───────────────────────────────────────────────
# ✨ END OF FILE
# ───────────────────────────────────────────────
