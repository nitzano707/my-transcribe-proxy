from fastapi import FastAPI, UploadFile, File
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


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """
    מקבל קובץ אודיו, שומר זמנית, ומחזיר URL ציבורי.
    """
    file_path = os.path.join(UPLOAD_DIR, file.filename)

    # שמירה זמנית של הקובץ
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # הפעלת טיימר למחיקה אוטומטית אחרי שעה
    delete_later(file_path)

    # יצירת URL ציבורי לקובץ
    base_url = "https://my-transcribe-proxy.onrender.com"
    file_url = f"{base_url}/files/{file.filename}"

    return JSONResponse({
        "url": file_url,
        "message": "הקובץ הועלה בהצלחה ויימחק תוך שעה."
    })


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
            "error": "הקובץ נמחק או לא נמצא. (ייתכן שחלפה שעה מאז ההעלאה)"
        }, status_code=404)
