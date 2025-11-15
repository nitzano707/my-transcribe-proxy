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

# ✅ CORS – פתוח לכל, אפשר לצמצם בהמשך אם תרצה
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
FALLBACK_LIMIT_DEFAULT = float(os.getenv("FALLBACK_LIMIT_DEFAULT", "0.1"))
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
    except:
        return None


def get_account(user_email: str):
    res = (
        supabase.table("accounts")
        .select("user_email, runpod_token_encrypted, used_credits, limit_credits")
        .eq("user_email", user_email)
        .maybe_single()
        .execute()
    )
    return res.data if hasattr(res, "data") else None


def get_user_token(user_email: str | None):
    try:
        if not user_email:
            if RUNPOD_API_KEY:
                return RUNPOD_API_KEY, True
            return None, True

        row = get_account(user_email)
        enc = row.get("runpod_token_encrypted") if row else None

        if enc:
            token = decrypt_token(enc)
            if token:
                return token, False

        if RUNPOD_API_KEY:
            return RUNPOD_API_KEY, True

        return None, True
    except:
        return (RUNPOD_API_KEY if RUNPOD_API_KEY else None), True


def check_fallback_allowance(user_email: str):
    row = get_account(user_email)
    if not row:
        supabase.table("accounts").insert({
            "user_email": user_email,
            "used_credits": 0.0,
            "limit_credits": FALLBACK_LIMIT_DEFAULT,
        }).execute()
        return True, 0.0, FALLBACK_LIMIT_DEFAULT

    used = float(row.get("used_credits") or 0.0)
    limit = float(row.get("limit_credits") or FALLBACK_LIMIT_DEFAULT)
    return (used < limit), used, limit


def add_fallback_usage(user_email: str, amount: float):
    row = get_account(user_email)
    used = float((row or {}).get("used_credits") or 0.0)
    new_used = round(used + amount, 6)
    supabase.table("accounts").update({"used_credits": new_used}).eq("user_email", user_email).execute()
    return new_used


def estimate_cost_from_response(resp_json: dict) -> float:
    try:
        ms = 0
        if "executionTime" in resp_json:
            ms = resp_json.get("executionTime") or 0
        seconds = float(ms) / 1000.0
        return round(seconds * RUNPOD_RATE_PER_SEC, 8)
    except:
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
                filename = f"upload_{int(time.time())}.bin"
                content = body

        if not content:
            return JSONResponse({"error": "לא התקבל קובץ תקין."}, status_code=400)

        path = os.path.join(UPLOAD_DIR, filename)
        with open(path, "wb") as f:
            f.write(content)

        delete_later(path)

        return JSONResponse({
            "url": f"{BASE_URL}/files/{quote(filename)}",
            "message": "הקובץ הועלה בהצלחה ויימחק תוך שעה."
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/files/{filename}")
async def get_file(filename: str):
    decoded = unquote(filename)
    path = os.path.join(UPLOAD_DIR, decoded)
    if os.path.exists(path):
        return FileResponse(path)
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
                return JSONResponse({
                    "error": "חריגה ממגבלת שימוש",
                    "used": used,
                    "limit": limit
                }, status_code=402)

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

        r = requests.post(
            "https://api.runpod.ai/v2/lco4rijwxicjyi/run",
            headers={"Authorization": f"Bearer {token_to_use}"},
            json=run_body,
            timeout=180,
        )

        out = r.json() if r.content else {}
        print(f"🚀 /transcribe → user={user_email}, using_fallback={using_fallback}")

        return JSONResponse(content=out, status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500})
# ───────────────────────────────────────────────
@app.get("/status/{job_id}")
def get_job_status(job_id: str, user_email: str | None = None):
    """
    בודק סטטוס מ-RunPod, מחייב (אם fallback), ושומר נתוני עיבוד במסד הנתונים.
    """
    try:
        # ───────────────────────────────────────────
        # 🔑 שליפת טוקן לשימוש
        # ───────────────────────────────────────────
        if not user_email:
            token_to_use, using_fallback = get_user_token(None)
        else:
            token_to_use, using_fallback = get_user_token(user_email)

        if not token_to_use:
            return JSONResponse({"error": "Missing token"}, status_code=401)

        # ───────────────────────────────────────────
        # 📡 שליפת סטטוס מ-RunPod
        # ───────────────────────────────────────────
        r = requests.get(
            f"https://api.runpod.ai/v2/lco4rijwxicjyi/status/{job_id}",
            headers={"Authorization": f"Bearer {token_to_use}"},
            timeout=30,
        )
        if not r.ok:
            return JSONResponse({"error": "שגיאה בשליפת סטטוס מ-RunPod"}, status_code=r.status_code)

        out = r.json() if r.content else {}
        print("🔍 RAW RunPod response:", out)

        # ───────────────────────────────────────────
        # 🧩 נרמול הסטטוס כדי לתפוס את כל הצורות של COMPLETED
        # ───────────────────────────────────────────
        status_raw = str(out.get("status", "")).strip().lower()

        valid_completed_values = {
            "completed",     # תקין
            "compleded",     # שגיאת כתיב של RunPod
            "complete",      # לפעמים בלי d
            "done"           # חלק מהמודלים
        }

        is_completed = status_raw in valid_completed_values

        # ───────────────────────────────────────────
        # 📘 עדכון קרדיטים fallback
        # ───────────────────────────────────────────
        if user_email and using_fallback and is_completed:

            cost = estimate_cost_from_response(out)
            if cost > 0:
                new_used = add_fallback_usage(user_email, cost)
                remaining = max(FALLBACK_LIMIT_DEFAULT - new_used, 0.0)

                out["_usage"] = {
                    "estimated_cost_usd": cost,
                    "used_credits": new_used,
                    "remaining": remaining,
                }

                print(
                    f"💰 fallback user {user_email} used {cost:.8f}$ "
                    f"(total {new_used:.6f}$, remaining {remaining:.6f}$)"
                )
            else:
                print("⚖️ עלות לא אותרה / 0 בתגובה של RunPod.")

        # ───────────────────────────────────────────
        # 🗄 עדכון רשומת תמלול ב-DB
        # ───────────────────────────────────────────
        if is_completed:

            # 1️⃣ שליפת רשומה לפי job_id
            rec = (
                supabase.table("transcriptions")
                .select("id, audio_length_seconds")
                .eq("job_id", job_id)
                .maybe_single()
                .execute()
            )
            record = rec.data if hasattr(rec, "data") else None
            if not record:
                print("⚠️ לא נמצאה רשומת DB עבור job_id:", job_id)
                return JSONResponse(content=out, status_code=200)

            record_id = record["id"]

            # 2️⃣ זמן עיבוד בפועל
            exec_ms = out.get("executionTime", 0)
            exec_sec = float(exec_ms) / 1000.0

            # 3️⃣ חילוץ אורך אודיו מתוך result
            audio_len = None
            try:
                outputs = out.get("output") or []
                if isinstance(outputs, list) and len(outputs) > 0:
                    last_segment = outputs[0]["result"][-1][-1]
                    audio_len = float(last_segment.get("end", 0.0))
                    print(f"📏 אורך אודיו מ-RunPod: {audio_len:.2f} שניות")
            except Exception as e:
                print("⚠️ שגיאה בחילוץ אורך אודיו:", e)

            # fallback → DB
            if not audio_len or audio_len == 0:
                audio_len = float(record.get("audio_length_seconds") or 0.0)
                print(f"📏 אורך אודיו נשלף מה-DB: {audio_len}")

            # יחס עיבוד
            ratio = exec_sec / audio_len if audio_len > 0 else None

            # חיוב (0.00016)
            billing = exec_sec * 0.00016

            # זמן boot
            delay_ms = out.get("delayTime", 0)
            boot_sec = float(delay_ms) / 1000.0

            # משוער (8%)
            estimated = audio_len * 0.08 if audio_len > 0 else None

            updates = {
                "audio_length_seconds": audio_len,
                "estimated_processing_seconds": estimated,
                "actual_processing_seconds": exec_sec,
                "billing_usd": billing,
                "processing_ratio": ratio,
                "worker_boot_time_seconds": boot_sec,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }

            supabase.table("transcriptions").update(updates).eq("id", record_id).execute()
            print(f"🗄 נתוני תמלול עודכנו ב-DB עבור הרשומה {record_id}")

        # ───────────────────────────────────────────
        # החזרת תגובת RunPod
        # ───────────────────────────────────────────
        return JSONResponse(content=out, status_code=r.status_code)

    except Exception as e:
        print(f"❌ /status error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500})
# ───────────────────────────────────────────────
@app.get("/effective-balance")
def effective_balance(user_email: str):
    """
    מחזיר יתרה אפקטיבית למשתמש.
    - אם המשתמש לא קיים → נוצרת רשומה fallback חדשה.
    - אם יש טוקן מוצפן אישי → נבדקת היתרה האמיתית ב-RunPod.
    - אם הטוקן האישי שגוי → נמחק ועוברים ל-fallback.
    """
    try:
        row = get_account(user_email)

        # 🆕 אין רשומה למשתמש – יצירה כ-fallback
        if not row:
            payload = {
                "user_email": user_email,
                "used_credits": 0.0,
                "limit_credits": FALLBACK_LIMIT_DEFAULT,
            }
            supabase.table("accounts").insert(payload).execute()
            bal = f"{FALLBACK_LIMIT_DEFAULT:.6f}"
            print(f"💰 יתרה נוכחית של {user_email}: {bal}$ (new fallback)")
            return JSONResponse({"balance": bal, "need_token": False})

        # 🔐 יש טוקן אישי מוצפן
        enc = row.get("runpod_token_encrypted")
        if enc:
            token = decrypt_token(enc)
            if token:
                bal, valid = get_real_runpod_balance(token)
                if valid:
                    bal_str = f"{bal:.6f}"
                    print(f"💰 יתרה נוכחית של {user_email}: {bal_str}$ (personal token)")
                    return JSONResponse({"balance": bal_str, "need_token": False})
                else:
                    # טוקן אישי לא תקין → מחיקה וחזרה ל-fallback
                    print(f"⚠️ טוקן אישי לא תקין עבור {user_email} – מעבר ל-fallback.")
                    supabase.table("accounts").update({
                        "runpod_token_encrypted": None,
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }).eq("user_email", user_email).execute()

        # 🪙 fallback
        used = float(row.get("used_credits") or 0.0)
        limit = float(row.get("limit_credits") or FALLBACK_LIMIT_DEFAULT)
        remaining = max(limit - used, 0.0)
        bal_str = f"{remaining:.6f}"

        print(f"💰 יתרה נוכחית של {user_email}: {bal_str}$ (fallback)")
        need_token_flag = remaining <= 0 or (enc is not None)

        return JSONResponse({
            "balance": bal_str,
            "need_token": need_token_flag,
        })

    except Exception as e:
        print(f"❌ /effective-balance error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)



# ───────────────────────────────────────────────
# ניהול תמלולים מאובטח
@app.post("/db/transcriptions/create")
async def create_transcription(request: Request):
    try:
        body = await request.json()
        res = supabase.table("transcriptions").insert({
            "user_email": body.get("user_email"),
            "alias": body.get("alias"),
            "folder_id": body.get("folder_id"),
            "audio_id": body.get("audio_id"),
            "media_type": body.get("media_type", "audio")
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

        if "audio_length_seconds" in updates:
            try: updates["audio_length_seconds"] = float(updates["audio_length_seconds"])
            except: pass

        if "estimated_processing_seconds" in updates:
            try: updates["estimated_processing_seconds"] = float(updates["estimated_processing_seconds"])
            except: pass

        if "file_size_bytes" in updates:
            try: updates["file_size_bytes"] = int(updates["file_size_bytes"])
            except: pass

        if "job_id" in updates:
            updates["job_id"] = updates["job_id"]

        res = supabase.table("transcriptions").update(updates).eq("id", id).execute()
        return JSONResponse({"status": "ok", "data": res.data})

    except Exception as e:
        print("❌ /db/transcriptions/update:", e)
        return JSONResponse({"error": str(e)}, status_code=500)



@app.get("/db/transcriptions/get")
def get_transcription(id: str):
    try:
        result = (
            supabase.table("transcriptions")
            .select("*")
            .eq("id", id)
            .maybe_single()
            .execute()
        )
        if result.data:
            return JSONResponse(result.data)
        return JSONResponse({"error": "רשומה לא נמצאה"}, status_code=404)

    except Exception as e:
        print(f"❌ /db/transcriptions/get: {e}")
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



# ───────────────────────────────────────────────
@app.post("/save-token")
async def save_token(request: Request):
    """
    שומר טוקן RunPod אישי מוצפן למשתמש ב-Supabase.
    """
    try:
        data = await request.json()
        user_email = data.get("user_email")
        token = data.get("token")

        if not user_email or not token:
            return JSONResponse({"error": "חסר user_email או token"}, status_code=400)

        if not ENCRYPTION_KEY:
            return JSONResponse({"error": "ENCRYPTION_KEY לא מוגדר בשרת"}, status_code=500)

        # בדיקת תקינות מול RunPod
        bal, valid = get_real_runpod_balance(token)
        if not valid:
            return JSONResponse({"error": "טוקן RunPod שגוי"}, status_code=400)

        # הצפנה
        key = ENCRYPTION_KEY.encode("utf-8")
        iv = os.urandom(16)
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        pad = AES.block_size - len(token.encode()) % AES.block_size
        padded = token.encode() + bytes([pad]) * pad
        encrypted = base64.b64encode(iv + cipher.encrypt(padded)).decode()

        row = get_account(user_email)
        if row:
            supabase.table("accounts").update({
                "runpod_token_encrypted": encrypted,
                "used_credits": 0.0,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }).eq("user_email", user_email).execute()
        else:
            supabase.table("accounts").insert({
                "user_email": user_email,
                "runpod_token_encrypted": encrypted,
                "used_credits": 0.0,
                "limit_credits": FALLBACK_LIMIT_DEFAULT,
            }).execute()

        return JSONResponse({
            "status": "ok",
            "balance": f"{float(bal):.6f}"
        })

    except Exception as e:
        print(f"❌ /save-token error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500})
# ───────────────────────────────────────────────
# 🧱 עדכון job_id (למקרה שהמשתמש התנתק לפני סיום התמלול)
@app.post("/db/transcriptions/update-job")
async def update_job(request: Request):
    try:
        body = await request.json()
        record_id = body.get("record_id")
        job_id = body.get("job_id")

        if not record_id or not job_id:
            return JSONResponse({"error": "Missing record_id or job_id"}, status_code=400)

        res = (
            supabase.table("transcriptions")
            .update({
                "job_id": job_id,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")
            })
            .eq("id", record_id)
            .execute()
        )

        return JSONResponse({"status": "ok", "data": res.data})

    except Exception as e:
        print("❌ /db/transcriptions/update-job:", e)
        return JSONResponse({"error": str(e)}, status_code=500})
