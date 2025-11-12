from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import os, threading, time, requests
from urllib.parse import quote, unquote

# ✅ ספריות חדשות
from supabase import create_client, Client
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import base64

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


# 🧩 פונקציה לפענוח AES (תואם CryptoJS)
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


# 🧠 פונקציה לשליפת טוקן המשתמש
def get_user_token(user_email: str) -> str:
    """
    מחזיר טוקן רנפוד תקף למשתמש:
    - אם יש לו טוקן מוצפן ב-Supabase → מפוענח ומוחזר
    - אחרת → מוחזר טוקן ברירת מחדל (RUNPOD_API_KEY)
    """
    try:
        result = (
            supabase.table("accounts")
            .select("runpod_token_encrypted")
            .eq("owner_email", user_email)
            .execute()
        )
        if not result.data:
            print("⚠️ משתמש ללא טוקן — שימוש בברירת מחדל.")
            return RUNPOD_API_KEY

        encrypted = result.data[0].get("runpod_token_encrypted")
        if not encrypted:
            print("⚠️ רשומה ללא טוקן מוצפן — שימוש בברירת מחדל.")
            return RUNPOD_API_KEY

        token = decrypt_token(encrypted)
        if token:
            print(f"🔐 נטען טוקן משתמש עבור {user_email[:3]}***")
            return token
        else:
            print("⚠️ שגיאה בפענוח — שימוש בברירת מחדל.")
            return RUNPOD_API_KEY

    except Exception as e:
        print(f"❌ שגיאה בשליפת טוקן: {e}")
        return RUNPOD_API_KEY


# ───────────────────────────────────────────────
@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(None)):
    """מקבל קובץ אודיו/וידאו ושומר זמנית"""
    try:
        filename = None
        content = None

        if file:
            filename = file.filename
            content = await file.read()
        else:
            body = await request.body()
            if body:
                filename = f"upload_{int(time.time())}.bin"
                content = body

        if not content:
            return JSONResponse({"error": "לא התקבל קובץ תקין."}, status_code=400)

        file_path = os.path.join(UPLOAD_DIR, filename)
        with open(file_path, "wb") as f:
            f.write(content)
        delete_later(file_path)

        encoded_filename = quote(filename)
        file_url = f"{BASE_URL}/files/{encoded_filename}"

        return JSONResponse({
            "url": file_url,
            "message": "הקובץ הועלה בהצלחה ויימחק תוך שעה."
        })
    except Exception as e:
        return JSONResponse({"error": f"שגיאה בעת העלאת הקובץ: {str(e)}"}, status_code=500)
# ───────────────────────────────────────────────


@app.get("/files/{filename}")
async def get_file(filename: str):
    """מאפשר הורדה או צפייה בקובץ לפי שם"""
    decoded_filename = unquote(filename)
    file_path = os.path.join(UPLOAD_DIR, decoded_filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    else:
        return JSONResponse(
            {"error": "הקובץ נמחק או לא נמצא (ייתכן שחלפה שעה מאז ההעלאה)."},
            status_code=404,
        )
# ───────────────────────────────────────────────


@app.post("/transcribe")
async def transcribe(request: Request):
    """שליחת בקשת תמלול ל-RunPod"""
    try:
        data = await request.json()
        user_email = data.get("user_email")

        # קבלת טוקן לפי משתמש
        token_to_use = get_user_token(user_email)

        response = requests.post(
            "https://api.runpod.ai/v2/lco4rijwxicjyi/run",
            headers={
                "Authorization": f"Bearer {token_to_use}",
                "Content-Type": "application/json",
            },
            json=data,
            timeout=180,
        )
        print("🔁 RunPod /run Response:", response.status_code)
        return JSONResponse(content=response.json())
    except Exception as e:
        print(f"❌ שגיאה ב-/transcribe: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
# ───────────────────────────────────────────────


@app.get("/status/{job_id}")
async def check_status(job_id: str):
    """בדיקת סטטוס משימה ב-RunPod"""
    try:
        response = requests.get(
            f"https://api.runpod.ai/v2/lco4rijwxicjyi/status/{job_id}",
            headers={"Authorization": f"Bearer {RUNPOD_TOKEN}"},
            timeout=60,
        )
        print(f"🔍 RunPod /status/{job_id} → {response.status_code}")
        return JSONResponse(content=response.json())
    except Exception as e:
        print(f"❌ שגיאה ב-/status/{job_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
# ───────────────────────────────────────────────


@app.get("/fetch-audio")
def fetch_audio(request: Request, file_id: str):
    """מוריד קובץ מדרייב בשם המשתמש לפי טוקן שנשלח מהלקוח"""
    try:
        user_token = request.headers.get("Authorization")
        if not user_token:
            return JSONResponse({"error": "חסר טוקן משתמש (Authorization header)"}, status_code=401)

        drive_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        headers = {"Authorization": user_token}

        r = requests.get(drive_url, headers=headers, stream=True)
        if not r.ok:
            return JSONResponse({"error": f"שגיאה בשליפה מדרייב ({r.status_code})"}, status_code=r.status_code)

        return StreamingResponse(r.iter_content(8192), media_type=r.headers.get("Content-Type", "audio/mpeg"))
    except Exception as e:
        print(f"❌ fetch-audio error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
# ───────────────────────────────────────────────


@app.get("/fetch-and-store-audio")
def fetch_and_store_audio(request: Request, file_id: str, format_hint: str = "mp3"):
    """
    מוריד קובץ שמע מדרייב בעזרת הטוקן של המשתמש (מה-Header),
    שומר אותו זמנית בשרת ומחזיר URL ציבורי לגישה ישירה ל-RunPod.
    """
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
        print(f"✅ קובץ הורד מדרייב ונשמר בשרת: {file_url}")

        return JSONResponse({"url": file_url})
    except Exception as e:
        print(f"❌ fetch-and-store-audio error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
# ───────────────────────────────────────────────
