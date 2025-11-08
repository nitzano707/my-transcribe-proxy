from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
import requests, shutil, os

app = FastAPI()

TRANSCRIBE_API = "https://transcribe.ivrit.ai/upload"

@app.post("/upload")
async def upload_and_forward(file: UploadFile = File(...)):
    # שמירת קובץ זמנית בתיקיית tmp
    tmp_path = f"/tmp/{file.filename}"
    with open(tmp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        # שליחה ישירה לשירות התמלול
        with open(tmp_path, "rb") as f:
            response = requests.post(
                TRANSCRIBE_API,
                files={"file": f}
            )

        # מחיקת הקובץ אחרי השליחה
        os.remove(tmp_path)

        # החזרת התוצאה למשתמש
        if response.ok:
            return JSONResponse(content=response.json())
        else:
            return JSONResponse(
                content={"error": response.text},
                status_code=response.status_code
            )

    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return JSONResponse(content={"error": str(e)}, status_code=500)
