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

# ✅ CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ───────────────────────────────────────────────
# משתני סביבה
RUNPOD_TOKEN = os.getenv("RUNPOD_TOKEN")  # אופציונלי
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")  # חובה
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")  # טוקן fallback
FALLBACK_LIMIT_DEFAULT = float(os.getenv("FALLBACK_LIMIT_DEFAULT", "0.5"))
RUNPOD_RATE_PER_SEC = float(os.getenv("RUNPOD_RATE_PER_SEC", "0.0002"))

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
BASE_URL = "https://my-transcribe-proxy.onrender.com"

# חיבור ל-Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
# ───────────────────────────────────────────────


def delete_later(path, delay=3600):
    """מוחק קובץ אחרי delay שניות (ברירת מחדל: שעה)."""
    def _delete():
        time.sleep(delay)
        if os.path.exists(path):
            os.remove(path)
            print(f"[Auto Delete] נמחק הקובץ: {path}")
    threading.Thread(target=_delete, daemon=True).start()


@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    return JSONResponse({"status": "ok"})


# 🧩 פענוח AES
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


# 🔎 שליפת חשבון
def get_account(user_email: str):
    res = (
        supabase.table("accounts")
        .select("user_email, runpod_token_encrypted, used_credits, limit_credits")
        .eq("user_email", user_email)
        .maybe_single()
        .execute()
    )
    return res.data if hasattr(res, "data") else None


def get_user_token(user_email: str) -> tuple[str | None, bool]:
    """מחזיר (token_to_use, using_fallback)."""
    try:
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


def encrypt_default_token(token: str) -> str | None:
    """הצפנה של טוקן ברירת מחדל כך שיישמר כמו טוקן רגיל."""
    try:
        if not ENCRYPTION_KEY:
            return None
        key = ENCRYPTION_KEY.encode("utf-8")
        iv = os.urandom(16)
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        padding_len = AES.block_size - len(token.encode()) % AES.block_size
        padded = token.encode() + bytes([padding_len]) * padding_len
        ciphertext = cipher.encrypt(padded)
        return base64.b64encode(iv + ciphertext).decode("utf-8")
    except Exception as e:
        print(f"❌ שגיאה בהצפנת טוקן ברירת מחדל: {e}")
        return None


def check_fallback_allowance(user_email: str) -> tuple[bool, float, float]:
    """בודק אם המשתמש רשום; אם לא — יוצר רשומה עם טוקן fallback."""
    row = get_account(user_email)
    if not row:
        encrypted_default = encrypt_default_token(RUNPOD_API_KEY) if RUNPOD_API_KEY else None
        payload = {
            "user_email": user_email,
            "used_credits": 0.0,
            "limit_credits": FALLBACK_LIMIT_DEFAULT
        }
        if encrypted_default:
            payload["runpod_token_encrypted"] = encrypted_default
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


def estimate_cost_from_response(resp_json: dict) -> float:
    """
    מעריך עלות על בסיס executionTime ממבנים שונים בתגובה של RunPod.
    מחזיר ערך מדויק גם אם זמן העיבוד קצר מאוד.
    """
    try:
        # ננסה כמה אפשרויות למציאת זמן העיבוד
        ms = (
            resp_json.get("executionTime")
            or (resp_json.get("output", {}) or {}).get("executionTime")
            or (resp_json.get("output", [{}])[0].get("executionTime") if isinstance(resp_json.get("output"), list) else 0)
            or 0
        )
        seconds = float(ms) / 1000.0
        cost = seconds * RUNPOD_RATE_PER_SEC

        if cost > 0:
            print(f"⏱ זמן עיבוד כולל: {seconds:.2f} שניות → עלות מוערכת: {cost:.8f}$")
        else:
            print("⚠️ זמן עיבוד לא זוהה בתגובה של RunPod:", resp_json.keys())

        return round(cost, 8)
    except Exception as e:
        print(f"❌ שגיאה ב-estimate_cost_from_response: {e}")
        return 0.0



# ───────────────────────────────────────────────
@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(None)):
    try:
        filename, content = None, None
        if file:
            filename, content = file.filename, await file.read()
        else:
            body = await request.body()
            if body:
                filename, content = f"upload_{int(time.time())}.bin", body
        if not content:
            return JSONResponse({"error": "לא התקבל קובץ תקין."}, status_code=400)

        file_path = os.path.join(UPLOAD_DIR, filename)
        with open(file_path, "wb") as f:
            f.write(content)
        delete_later(file_path)
        encoded_filename = quote(filename)
        file_url = f"{BASE_URL}/files/{encoded_filename}"
        return JSONResponse({"url": file_url, "message": "הקובץ הועלה בהצלחה ויימחק תוך שעה."})
    except Exception as e:
        return JSONResponse({"error": f"שגיאה בעת העלאת הקובץ: {str(e)}"}, status_code=500)


@app.get("/files/{filename}")
async def get_file(filename: str):
    decoded_filename = unquote(filename)
    file_path = os.path.join(UPLOAD_DIR, decoded_filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    return JSONResponse({"error": "הקובץ נמחק או לא נמצא."}, status_code=404)


# ───────────────────────────────────────────────
# 📥 שליפת קובץ מדרייב לשרת (לתמלול)
@app.get("/fetch-and-store-audio")
async def fetch_and_store_audio(request: Request, file_id: str):
    """
    שולף קובץ מדרייב, שומר זמנית, מחזיר URL.
    תומך ב-Authorization header עם Bearer token.
    """
    try:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse({"error": "חסר access token של Google"}, status_code=400)

        token = auth_header.split("Bearer ")[1]
        headers = {"Authorization": f"Bearer {token}"}
        drive_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

        res = requests.get(drive_url, headers=headers, stream=True)
        if not res.ok:
            return JSONResponse({"error": f"שגיאה בשליפת קובץ מדרייב: {res.text}"}, status_code=res.status_code)

        content_type = res.headers.get("Content-Type", "application/octet-stream")
        ext_map = {
            "audio/mp4": ".m4a",
            "audio/x-m4a": ".m4a",
            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
            "video/mp4": ".mp4",
        }
        ext = ext_map.get(content_type, ".audio")

        filename = f"drive_{file_id}_{int(time.time())}{ext}"
        file_path = os.path.join(UPLOAD_DIR, filename)

        with open(file_path, "wb") as f:
            for chunk in res.iter_content(chunk_size=8192):
                f.write(chunk)

        delete_later(file_path)
        file_url = f"{BASE_URL}/files/{quote(filename)}"
        print(f"✅ נשמר קובץ מדרייב: {file_path} ({content_type})")
        return JSONResponse({"url": file_url})

    except Exception as e:
        print(f"❌ /fetch-and-store-audio error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)




# ───────────────────────────────────────────────
# ───────────────────────────────────────────────
@app.post("/transcribe")
async def transcribe(request: Request):
    """
    שליחת בקשת תמלול ל-RunPod והערכת עלות לפי זמן העיבוד.
    אם המשתמש פועל על fallback (טוקן ברירת מחדל), המערכת תעדכן את השימוש (used_credits).
    """
    try:
        data = await request.json()
        user_email = data.get("user_email")
        if not user_email:
            return JSONResponse({"error": "user_email is required"}, status_code=400)

        # 🔑 שליפת טוקן
        token_to_use, using_fallback = get_user_token(user_email)

        if not token_to_use:
            return JSONResponse(
                {
                    "error": "לא הוגדר טוקן לשימוש (אין טוקן אישי ואין RUNPOD_API_KEY בשרת).",
                    "action": "יש להזין טוקן RunPod אישי"
                },
                status_code=401,
            )

        # 🔒 בדיקת מגבלת שימוש (רק למשתמשים על fallback)
        if using_fallback:
            allowed, used, limit = check_fallback_allowance(user_email)
            if not allowed:
                return JSONResponse(
                    {
                        "error": "חריגה ממגבלת שימוש",
                        "used": used,
                        "limit": limit,
                        "action": "יש להזין טוקן RunPod אישי"
                    },
                    status_code=402,
                )

        # 🎯 בניית גוף הבקשה ל-RunPod
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

        # 🚀 שליחה ל-RunPod
        response = requests.post(
            "https://api.runpod.ai/v2/lco4rijwxicjyi/run",
            headers={"Authorization": f"Bearer {token_to_use}", "Content-Type": "application/json"},
            json=run_body,
            timeout=180,
        )

        out = response.json() if response.content else {}
        status_code = response.status_code if response.status_code else 200

        # 💰 חישוב עלות לפי זמן עיבוד
        cost = estimate_cost_from_response(out)
        usage_info = {"estimated_cost_usd": cost}

        if cost > 0:
            if using_fallback:
                new_used = add_fallback_usage(user_email, cost)
                remaining = round(FALLBACK_LIMIT_DEFAULT - new_used, 6)
                usage_info.update({
                    "used_credits": new_used,
                    "limit_credits": FALLBACK_LIMIT_DEFAULT,
                    "remaining": remaining
                })
                print(f"💰 fallback user {user_email} used {cost}$ (total {new_used}$, remaining {remaining}$)")
            else:
                print(f"💳 personal token used by {user_email}: {cost}$ (not tracked in DB)")

        out["_usage"] = usage_info
        return JSONResponse(content=out, status_code=status_code)

    except Exception as e:
        print(f"❌ /transcribe error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)



# ───────────────────────────────────────────────
@app.get("/status/{job_id}")
def get_job_status(job_id: str, user_email: str = None):
    """
    בודק את סטטוס המשימה ב-RunPod לפי מזהה job_id.
    אם הסתיים בהצלחה (COMPLETED) — מחשב עלות, מעדכן יתרה ומחזיר מידע כולל שימוש.
    """
    try:
        token_to_use, using_fallback = get_user_token(user_email or "anonymous@example.com")
        if not token_to_use:
            return JSONResponse({"error": "Missing token"}, status_code=401)

        r = requests.get(
            f"https://api.runpod.ai/v2/lco4rijwxicjyi/status/{job_id}",
            headers={"Authorization": f"Bearer {token_to_use}"},
            timeout=30,
        )
        if not r.ok:
            return JSONResponse({"error": "שגיאה בשליפת סטטוס מ-RunPod"}, status_code=r.status_code)

        out = r.json() if r.content else {}

        # ✅ רק אם המשימה הושלמה נחשב עלות ונעדכן יתרה
        if using_fallback and out.get("status") == "COMPLETED":
            cost = estimate_cost_from_response(out)
            if cost > 0:
                new_used = add_fallback_usage(user_email, cost)
                remaining = max(FALLBACK_LIMIT_DEFAULT - new_used, 0)
                print(
                    f"💰 fallback user {user_email} used {cost:.8f}$ "
                    f"(total {new_used:.6f}$, remaining {remaining:.6f}$)"
                )
                out["_usage"] = {
                    "estimated_cost_usd": cost,
                    "used_credits": new_used,
                    "remaining": remaining,
                }
            else:
                print("⚖️ עלות לא אותרה או אפסית בתגובה של RunPod.")

        return JSONResponse(content=out, status_code=r.status_code)
    except Exception as e:
        print(f"❌ /status error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)



# ───────────────────────────────────────────────
@app.get("/effective-balance")
def effective_balance(user_email: str):
    """מחזיר יתרה אפקטיבית ומוסיף fallback אם אין רשומה."""
    try:
        row = get_account(user_email)
        if not row:
            encrypted_default = encrypt_default_token(RUNPOD_API_KEY) if RUNPOD_API_KEY else None
            payload = {
                "user_email": user_email,
                "used_credits": 0.0,
                "limit_credits": FALLBACK_LIMIT_DEFAULT
            }
            if encrypted_default:
                payload["runpod_token_encrypted"] = encrypted_default
            supabase.table("accounts").insert(payload).execute()
            need_token = encrypted_default is None
            return JSONResponse({"balance": FALLBACK_LIMIT_DEFAULT, "need_token": need_token})

        enc = row.get("runpod_token_encrypted")
        if enc:
            token = decrypt_token(enc)
            if token:
                r = requests.get(
                    "https://api.runpod.io/v2/account",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=12,
                )
                if r.ok:
                    try:
                        bal = float(r.json().get("balance", 0.0))
                    except Exception:
                        bal = 0.0
                    return JSONResponse({"balance": bal, "need_token": False})

        used = float(row.get("used_credits") or 0)
        limit = float(row.get("limit_credits") or FALLBACK_LIMIT_DEFAULT)
        remaining = max(limit - used, 0)
        return JSONResponse({"balance": remaining, "need_token": remaining <= 0})
    except Exception as e:
        print(f"❌ /effective-balance error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ───────────────────────────────────────────────
# 🧱 ניהול תמלולים מאובטח דרך השרת
@app.post("/db/transcriptions/create")
async def create_transcription(request: Request):
    try:
        body = await request.json()
        user_email = body.get("user_email")
        alias = body.get("alias")
        folder_id = body.get("folder_id")
        audio_id = body.get("audio_id")
        media_type = body.get("media_type", "audio")

        res = supabase.table("transcriptions").insert({
            "user_email": user_email,
            "alias": alias,
            "folder_id": folder_id,
            "audio_id": audio_id,
            "media_type": media_type
        }).execute()
        return JSONResponse({"status": "ok", "data": res.data})
    except Exception as e:
        print("❌ /db/transcriptions/create:", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/db/transcriptions/update")
async def update_transcription(request: Request):
    try:
        body = await request.json()
        id = body.get("id")
        updates = body.get("updates", {})
        updates["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        res = supabase.table("transcriptions").update(updates).eq("id", id).execute()
        return JSONResponse({"status": "ok", "data": res.data})
    except Exception as e:
        print("❌ /db/transcriptions/update:", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/db/transcriptions/delete")
async def delete_transcription(request: Request):
    try:
        body = await request.json()
        id = body.get("id")
        supabase.table("transcriptions").delete().eq("id", id).execute()
        return JSONResponse({"status": "deleted", "id": id})
    except Exception as e:
        print("❌ /db/transcriptions/delete:", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/save-token")
async def save_token(request: Request):
    """שומר טוקן מוצפן חדש למשתמש ב-Supabase."""
    try:
        data = await request.json()
        user_email = data.get("user_email")
        token = data.get("token")

        if not user_email or not token:
            return JSONResponse({"error": "חסר user_email או token"}, status_code=400)
        if not ENCRYPTION_KEY:
            return JSONResponse({"error": "ENCRYPTION_KEY לא מוגדר בשרת"}, status_code=500)

        key = ENCRYPTION_KEY.encode("utf-8")
        iv = os.urandom(16)
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        padding_len = AES.block_size - len(token.encode()) % AES.block_size
        padded = token.encode() + bytes([padding_len]) * padding_len
        encrypted = base64.b64encode(iv + cipher.encrypt(padded)).decode()

        row = get_account(user_email)
        if row:
            supabase.table("accounts").update(
                {"runpod_token_encrypted": encrypted, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            ).eq("user_email", user_email).execute()
        else:
            supabase.table("accounts").insert(
                {"user_email": user_email, "runpod_token_encrypted": encrypted, "used_credits": 0.0, "limit_credits": FALLBACK_LIMIT_DEFAULT}
            ).execute()

        return JSONResponse({"status": "ok"})
    except Exception as e:
        print(f"❌ /save-token error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
