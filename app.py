from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse
import os, shutil

app = FastAPI()

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    # שמור את הקובץ בתיקייה זמנית
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # צור URL ציבורי לקובץ
    base_url = "https://my-transcribe-proxy.onrender.com"
    file_url = f"{base_url}/files/{file.filename}"

    return JSONResponse({"url": file_url})

@app.get("/files/{filename}")
async def get_file(filename: str):
    # הגש את הקובץ חזרה למשתמש לפי שם
    file_path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    return JSONResponse({"error": "File not found"}, status_code=404)
