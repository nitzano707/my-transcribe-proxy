from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import JSONResponse, FileResponse
import os, shutil, threading, time

app = FastAPI()

# תיקייה זמנית לשמירת קבצים
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def delete_later(path, delay=3600):
    """
    מוחק את הקובץ אחרי delay שניות (ברירת מחדל: שעה)
    """
    def _delete():
        time.sleep(delay)
        if os.path.exists(path):
            os.remove(path)
            print(f"[Auto Delete] נמחק הקובץ: {path}")
    threading.Thread(target=_delete, daemon=True).start()


@app.get("/ping")
async def ping():
    """
    נתיב קל לבדיקה מ-UptimeRobot כדי לשמור את השרת ער
    """
    return {"status": "ok"}


@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(None)):
    """
    מקבל קובץ אודיו (לא משנה באיזה שם שדה נשלח),
    שומר זמנית, ומחזיר URL ציבורי.
    """
    try:
        # אם לא נשלח שדה בשם 'file', ננסה למצוא כל קובץ אחר
        if file is None:
            form = await request.form()
            if len(form) > 0:
                # לוקח את הקובץ הראשון שנשלח בטופס
                first_value = list(form.values())[0]
                if isinstance(first_value, UploadFile):
                    file = first_value
                else:
                    return JSONResponse({"error": "לא התקבל קובץ תקין."}, status_code=400)
            else:
                return JSONResponse({"error": "לא התקבל קובץ."}, status_code=400)

        # שמירה זמנית של הקובץ
        file_path = os.path.join(UPLOAD_DIR, file.filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # הפעלת טיימר למחיקה אוטומטית
        delete_later(file_path)

        # יצירת URL ציבורי
        base_url = "https://my-transcribe-proxy.onrender.com"
        file_url = f"{base_url}/files/{file.filename}"

        return JSONResponse({
            "url": file_url,
            "message": "הקובץ הועלה בהצלחה ויימחק תוך שעה."
        })

    except Exception as e:
        return JSONResponse({"error": f"שגיאה בעת העלאת הקובץ: {str(e)}"}, status_code=500)


@app.get("/files/{filename}")
async def get_file(filename: str):
    """
    מציג או מחזיר את הקובץ לפי שם.
    """
    file_path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    else:
        return JSONResponse({
            "error": "הקובץ נמחק או לא נמצא (ייתכן שחלפה שעה מאז ההעלאה)."
        }, status_code=404)
