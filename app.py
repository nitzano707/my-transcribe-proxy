from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os, shutil, threading, time, requests
from urllib.parse import quote, unquote

app = FastAPI()

# ✅ הוספת תמיכה ב-CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # אפשר להחליף לכתובת שלך בלבד אם תרצה לאבטח
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ───────────────────────────────────────────────
# קריאת טוקן הסביבה של RunPod
RUNPOD_TOKEN = os.getenv("RUNPOD_TOKEN")

# תיקייה זמנית לשמירת קבצים
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# כתובת הבסיס (שנה לפי הדומיין שלך ברנדר)
BASE_URL = "https://my-transcribe-proxy.onrender.com"
# ───────────────────────────────────────────────


def delete_later(path, delay=3600):
    """מוחק את הקובץ אוטומטית אחרי delay שניות (ברירת מחדל: שעה)."""
    def _delete():
        time.sleep(delay)
        if os.path.exists(path):
            os.remove(path)
            print(f"[Auto Delete] נמחק הקובץ: {path}")
    threading.Thread(target=_delete, daemon=True).start()


# ───────────────────────────────────────────────
# פינג ל-UptimeRobot או לבדיקה ידנית
@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    """
    מחזיר תשובה פשוטה כדי לשמור את השרת ער.
    תומך גם ב-HEAD (כי UptimeRobot שולח HEAD כברירת מחדל)
    """
    return JSONResponse({"status": "ok"})
# ───────────────────────────────────────────────


@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(None)):
    """
    מקבל קובץ אודיו מכל סוג של בקשה:
    - form-data (עם שדה בשם file / key / upload)
    - binary (raw)
    שומר זמנית ומחזיר URL ציבורי לגישה לקובץ.
    """
    try:
        filename = None
        content = None

        # --- מצב 1: אם נשלח כ-form-data ---
        if file:
            filename = file.filename
            content = await file.read()

        # --- מצב 2: אם לא נשלח כ-form-data, נבדוק אם זה binary/raw ---
        else:
            body = await request.body()
            if body:
                filename = f"upload_{int(time.time())}.bin"
                content = body

        # --- אם לא התקבל בכלל תוכן ---
        if not content:
            return JSONResponse({"error": "לא התקבל קובץ תקין."}, status_code=400)

        # שמירת הקובץ בתיקייה
        file_path = os.path.join(UPLOAD_DIR, filename)
        with open(file_path, "wb") as f:
            f.write(content)

        # מחיקה אוטומטית אחרי שעה
        delete_later(file_path)

        # קידוד שם הקובץ ל-URL תקין (תומך בעברית, רווחים ותווים מיוחדים)
        encoded_filename = quote(filename)

        # יצירת קישור ציבורי תקין
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
    """מאפשר להוריד או לצפות בקובץ לפי שם."""
    decoded_filename = unquote(filename)
    file_path = os.path.join(UPLOAD_DIR, decoded_filename)

    if os.path.exists(file_path):
        return FileResponse(file_path)
    else:
        return JSONResponse({
            "error": "הקובץ נמחק או לא נמצא (ייתכן שחלפה שעה מאז ההעלאה)."
        }, status_code=404)
# ───────────────────────────────────────────────


# ───────────────────────────────────────────────
# בקשה ל-RunPod דרך השרת (מוגן עם טוקן סביבתי)
@app.post("/transcribe")
async def transcribe(request: Request):
    """
    מקבל בקשת תמלול מה-Frontend ושולח אותה ל-RunPod
    בעזרת ה-Token השמור בשרת (ולא בצד הלקוח)
    """
    try:
        data = await request.json()

        response = requests.post(
            "https://api.runpod.ai/v2/lco4rijwxicjyi/run",
            headers={
                "Authorization": f"Bearer {RUNPOD_TOKEN}",
                "Content-Type": "application/json"
            },
            json=data,
            timeout=180
        )

        print("🔁 RunPod /run Response:", response.status_code)
        return JSONResponse(content=response.json())

    except Exception as e:
        print(f"❌ שגיאה ב-/transcribe: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
# ───────────────────────────────────────────────


# ───────────────────────────────────────────────
@app.get("/status/{job_id}")
async def check_status(job_id: str):
    """
    בודק את הסטטוס של משימת תמלול קיימת ב-RunPod
    """
    try:
        response = requests.get(
            f"https://api.runpod.ai/v2/lco4rijwxicjyi/status/{job_id}",
            headers={"Authorization": f"Bearer {RUNPOD_TOKEN}"},
            timeout=60
        )

        print(f"🔍 RunPod /status/{job_id} → {response.status_code}")
        return JSONResponse(content=response.json())

    except Exception as e:
        print(f"❌ שגיאה ב-/status/{job_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
# ───────────────────────────────────────────────


@app.get("/fetch-audio")
def fetch_audio(request: Request, file_id: str):
    """
    מוריד קובץ מדרייב בשם המשתמש.
    דורש שה-Frontend ישלח Header עם Authorization: Bearer <user_token>
    """
    try:
        user_token = request.headers.get("Authorization")
        if not user_token:
            return JSONResponse({"error": "חסר טוקן משתמש (Authorization header)"}, status_code=401)

        drive_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        headers = {"Authorization": user_token}

        r = requests.get(drive_url, headers=headers, stream=True)
        if not r.ok:
            return JSONResponse({"error": f"שגיאה בשליפה מדרייב ({r.status_code})"}, status_code=r.status_code)

        from fastapi.responses import StreamingResponse
        return StreamingResponse(r.iter_content(8192), media_type=r.headers.get("Content-Type", "audio/mpeg"))
    except Exception as e:
        print(f"❌ fetch-audio error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
