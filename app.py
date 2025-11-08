from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import JSONResponse, FileResponse
import os, shutil, threading, time
from urllib.parse import quote, unquote

app = FastAPI()

# ───────────────────────────────────────────────
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
    # פענוח שם הקובץ שקודד קודם לכן
    decoded_filename = unquote(filename)
    file_path = os.path.join(UPLOAD_DIR, decoded_filename)

    if os.path.exists(file_path):
        return FileResponse(file_path)
    else:
        return JSONResponse({
            "error": "הקובץ נמחק או לא נמצא (ייתכן שחלפה שעה מאז ההעלאה)."
        }, status_code=404)
# ───────────────────────────────────────────────
