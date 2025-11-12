from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import os, threading, time, requests
from urllib.parse import quote, unquote

import base64, hashlib, json
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
RUNPOD_TOKEN = os.getenv("RUNPOD_TOKEN")  # לא בשימוש לשליחת תמלול; נשאר ל/status
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")  # טוקן ברירת מחדל (fallback)
FALLBACK_LIMIT_DEFAULT = float(os.getenv("FALLBACK_LIMIT_DEFAULT", "0.5"))  # $
RUNPOD_RATE_PER_SEC = float(os.getenv("RUNPOD_RATE_PER_SEC", "0.0002"))     # $/sec

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

BASE_URL = "https://my-transcribe-proxy.onrender.com"

# חיבור ל-Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
# ───────────────────────────────────────────────

def delete_later(path, delay=3600):
    """מוחק קובץ אוטומטית לאחר שעה (ברירת מחדל)."""
    def _delete():
        time.sleep(delay)
        if os.path.exists(path):
            os.remove(path)
            print(f"[Auto Delete] נמחק הקובץ: {path}")
    threading.Thread(target=_delete, daemon=True).start()

# ───────────────────────────────────────────────
@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    """בדיקת חיים ל-UptimeRobot"""
    return JSONResponse({"status": "ok"})
# ───────────────────────────────────────────────

# 🧩 פענוח AES (תואם CryptoJS)
def decrypt_token(encrypted_token: str) -> str:
    """פענוח טוקן מוצפן (תואם AES של CryptoJS ב-Frontend)."""
    try:
        key = bytes.fromhex(ENCRYPTION_KEY)
        data = base64.b64decode(encrypted_token)
        iv, ciphertext = data[:16], data[16:]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = unpad(cipher.decrypt(ciphertext), AES.block_size)
        return decrypted.decode("utf-8")
    except Exception as e:
        print(f"❌ שגיאה בפענוח טוקן: {e}")
        return None

# 🔎 שליפת רשומת חשבון
def get_account(user_email: str):
    res = (
        supabase.table("accounts")
        .select("owner_email, runpod_token_encrypted, used_credits, limit_credits")
        .eq("owner_email", user_email)
        .maybe_single()
        .execute()
    )
    return res.data if hasattr(res, "data") else None

# 🧠 טוקן לשימוש (משתמש/ברירת מחדל)
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

# ⛔ בדיקת מגבלה למשתמש על טוקן ברירת המחדל
def check_fallback_allowance(user_email: str) -> tuple[bool, float, float]:
    row = get_account(user_email)
    if not row:
        supabase.table("accounts").insert({
            "owner_email": user_email,
            "used_credits": 0.0,
            "limit_credits": FALLBACK_LIMIT_DEFAULT
        }).execute()
        return True, 0.0, FALLBACK_LIMIT_DEFAULT

    used = float(row.get("used_credits") or 0.0)
    limit = float(row.get("limit_credits") or FALLBACK_LIMIT_DEFAULT)
    return (used < limit), used, limit

# 💾 עדכון שימוש (דולרים)
def add_fallback_usage(user_email: str, amount_usd: float):
    row = get_account(user_email)
    used = float((row or {}).get("used_credits") or 0.0)
    new_used = round(used + amount_usd, 6)
    supabase.table("accounts").update({"used_credits": new_used}).eq("owner_email", user_email).execute()
    return new_used

# 💵 הערכת עלות מריצה (אם קיבלנו executionTime במילישניות)
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
    """מקבל קובץ אודיו/וידאו ושומר זמנית"""
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

# ───────────────────────────────────────────────
@app.get("/files/{filename}")
async def get_file(filename: str):
    decoded_filename = unquote(filename)
    file_path = os.path.join(UPLOAD_DIR, decoded_filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    return JSONResponse({"error": "הקובץ נמחק או לא נמצא."}, status_code=404)

# ───────────────────────────────────────────────
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
@app.get("/status/{job_id}")
async def check_status(job_id: str):
    try:
        response = requests.get(
            f"https://api.runpod.ai/v2/lco4rijwxicjyi/status/{job_id}",
            headers={"Authorization": f"Bearer {RUNPOD_TOKEN}"},
            timeout=60,
        )
        return JSONResponse(content=response.json())
    except Exception as e:
        print(f"❌ /status error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

# ───────────────────────────────────────────────
@app.get("/fetch-audio")
def fetch_audio(request: Request, file_id: str):
    try:
        user_token = request.headers.get("Authorization")
        if not user_token:
            return JSONResponse({"error": "Missing Authorization header"}, status_code=401)
        drive_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        headers = {"Authorization": user_token}
        r = requests.get(drive_url, headers=headers, stream=True)
        if not r.ok:
            return JSONResponse({"error": f"שגיאה בשליפה מדרייב ({r.status_code})"}, status_code=r.status_code)
        return StreamingResponse(r.iter_content(8192), media_type=r.headers.get("Content-Type", "audio/mpeg"))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ───────────────────────────────────────────────
@app.get("/fetch-and-store-audio")
def fetch_and_store_audio(request: Request, file_id: str, format_hint: str = "mp3"):
    try:
        user_token = request.headers.get("Authorization")
        if not user_token:
            return JSONResponse({"error": "Missing Authorization header"}, status_code=401)
        drive_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        headers = {"Authorization": user_token}
        r = requests.get(drive_url, headers=headers, stream=True)
        if not r.ok:
            return JSONResponse({"error": f"Drive fetch failed ({r.status_code})"}, status_code=r.status_code)
        temp_filename = f"{file_id}.{format_hint}"
        file_path = os.path.join(UPLOAD_DIR, temp_filename)
        with open(file_path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        delete_later(file_path)
        file_url = f"{BASE_URL}/files/{temp_filename}"
        print(f"✅ נשמר: {file_url}")
        return JSONResponse({"url": file_url})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ───────────────────────────────────────────────
@app.get("/balance")
def get_balance(user_email: str):
    try:
        token, _ = get_user_token(user_email)
        gql = {"query": "query { myself { clientBalance } }"}
        r = requests.post(
            "https://api.runpod.io/graphql",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=gql,
            timeout=20,
        )
        data = r.json()
        balance = ((data or {}).get("data") or {}).get("myself", {}).get("clientBalance")
        return JSONResponse({"balance": balance})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ───────────────────────────────────────────────
@app.get("/effective-balance")
def effective_balance(user_email: str):
    """בודק יתרה אפקטיבית: אמיתית אם יש טוקן אישי, אחרת יתרת fallback."""
    try:
        row = get_account(user_email)
        if not row:
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
