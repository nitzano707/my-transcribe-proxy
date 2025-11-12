from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import os, threading, time, requests
from urllib.parse import quote, unquote
import base64, json
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
def decrypt_token(encrypted_token: str) -> str:
    try:
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


def get_user_token(user_email: str) -> tuple[str, bool]:
    try:
        row = get_account(user_email)
        enc = row.get("runpod_token_encrypted") if row else None
        if enc:
            token = decrypt_token(enc)
            if token:
                return token, False
        return RUNPOD_API_KEY, True
    except Exception as e:
        print(f"❌ שגיאה בשליפת טוקן: {e}")
        return RUNPOD_API_KEY, True


def encrypt_default_token(token: str) -> str:
    """הצפנה של טוקן ברירת מחדל כך שיישמר כמו טוקן רגיל."""
    try:
        key = ENCRYPTION_KEY.encode("utf-8")
        iv = os.urandom(16)
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        ciphertext = cipher.encrypt(
            (token + (AES.block_size - len(token) % AES.block_size) * chr(AES.block_size - len(token) % AES.block_size)).encode("utf-8")
        )
        return base64.b64encode(iv + ciphertext).decode("utf-8")
    except Exception as e:
        print(f"❌ שגיאה בהצפנה של טוקן ברירת מחדל: {e}")
        return None


def check_fallback_allowance(user_email: str) -> tuple[bool, float, float]:
    """בודק אם המשתמש רשום; אם לא — יוצר רשומה עם טוקן fallback מוצפן."""
    row = get_account(user_email)
    if not row:
        encrypted_default = encrypt_default_token(RUNPOD_API_KEY)
        supabase.table("accounts").insert({
            "user_email": user_email,
            "runpod_token_encrypted": encrypted_default,
            "used_credits": 0.0,
            "limit_credits": FALLBACK_LIMIT_DEFAULT
        }).execute()
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
    ms = (
        resp_json.get("executionTime")
        or (resp_json.get("output", {}) or {}).get("executionTime")
        or 0
    )
    try:
        seconds = float(ms) / 1000.0
    except Exception:
        seconds = 0.0
    return round(seconds * RUNPOD_RATE_PER_SEC, 6)

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


@app.post("/transcribe")
async def transcribe(request: Request):
    try:
        data = await request.json()
        user_email = data.get("user_email")
        if not user_email:
            return JSONResponse({"error": "user_email is required"}, status_code=400)

        token_to_use, using_fallback = get_user_token(user_email)
        if using_fallback:
            allowed, used, limit = check_fallback_allowance(user_email)
            if not allowed:
                return JSONResponse(
                    {"error": "חריגה ממגבלת שימוש", "used": used, "limit": limit, "action": "יש להזין טוקן RunPod אישי"},
                    status_code=402,
                )

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
        out = response.json()
        if using_fallback:
            cost = estimate_cost_from_response(out)
            if cost > 0:
                new_used = add_fallback_usage(user_email, cost)
                out["_usage"] = {"estimated_cost_usd": cost, "used_credits": new_used}
        return JSONResponse(content=out)
    except Exception as e:
        print(f"❌ /transcribe error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

# ───────────────────────────────────────────────
@app.get("/effective-balance")
def effective_balance(user_email: str):
    try:
        row = get_account(user_email)
        if not row:
            supabase.table("accounts").insert({
                "user_email": user_email,
                "used_credits": 0.0,
                "limit_credits": FALLBACK_LIMIT_DEFAULT
            }).execute()
            return JSONResponse({"balance": FALLBACK_LIMIT_DEFAULT, "need_token": True})

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
                    bal = float(r.json().get("balance", 0.0))
                    return JSONResponse({"balance": bal, "need_token": False})

        used = float(row.get("used_credits") or 0)
        limit = float(row.get("limit_credits") or FALLBACK_LIMIT_DEFAULT)
        remaining = max(limit - used, 0)
        return JSONResponse({"balance": remaining, "need_token": remaining <= 0})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ───────────────────────────────────────────────
# 🧱  ניהול תמלולים מאובטח דרך השרת (במקום דרך ה-Frontend)
# ───────────────────────────────────────────────

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
    """
    שומר טוקן חדש למשתמש ב-Supabase לאחר הצפנה בצד שרת.
    """
    try:
        data = await request.json()
        user_email = data.get("user_email")
        token = data.get("token")

        if not user_email or not token:
            return JSONResponse({"error": "חסר user_email או token"}, status_code=400)

        # הצפנה עם מפתח השרת
        key = ENCRYPTION_KEY.encode("utf-8")
        iv = os.urandom(16)
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        padding_len = AES.block_size - len(token.encode()) % AES.block_size
        padded = token.encode() + bytes([padding_len]) * padding_len
        encrypted = base64.b64encode(iv + cipher.encrypt(padded)).decode()

        # עדכון או יצירה
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

