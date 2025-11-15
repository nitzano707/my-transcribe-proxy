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
RUNPOD_TOKEN = os.getenv("RUNPOD_TOKEN")          # לא חובה, אפשרי לשימוש עתידי
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")      # חובה לטוקנים אישיים
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")      # טוקן fallback גלובלי
FALLBACK_LIMIT_DEFAULT = float(os.getenv("FALLBACK_LIMIT_DEFAULT", "0.1"))
RUNPOD_RATE_PER_SEC = float(os.getenv("RUNPOD_RATE_PER_SEC", "0.0002"))

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
BASE_URL = "https://my-transcribe-proxy.onrender.com"

# חיבור ל-Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
# ───────────────────────────────────────────────


def delete_later(path, delay=3600):
    """מוחק קובץ אחרי delay שניות (ברירת מחדל: שעה)."""
    def _delete():
        time.sleep(delay)
        if os.path.exists(path):
            os.remove(path)
            print(f"[Auto Delete] נמחק הקובץ: {path}")
    threading.Thread(target=_delete, daemon=True).start()


@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    return JSONResponse({"status": "ok"})


# 🧩 פענוח AES (לטוקן אישי בלבד)
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


def get_user_token(user_email: str | None) -> tuple[str | None, bool]:
    """
    מחזיר (token_to_use, using_fallback).

    using_fallback == False → טוקן אישי מוצפן של המשתמש.
    using_fallback == True  → משתמש ב-RUNPOD_API_KEY (fallback גלובלי).
    """
    try:
        if not user_email:
            # קריאה אנונימית – לא ניצור רשומה, רק נשתמש ב-RUNPOD_API_KEY אם קיים
            if RUNPOD_API_KEY:
                return RUNPOD_API_KEY, True
            return None, True

        row = get_account(user_email)
        enc = row.get("runpod_token_encrypted") if row else None

        # קודם כל – אם יש טוקן אישי מוצפן → להשתמש בו
        if enc:
            token = decrypt_token(enc)
            if token:
                return token, False  # לא fallback

        # אחרת – אין טוקן אישי → אם יש RUNPOD_API_KEY משתמשים בו כ-fallback
        if RUNPOD_API_KEY:
            return RUNPOD_API_KEY, True

        # אין כלום
        return None, True
    except Exception as e:
        print(f"❌ שגיאה בשליפת טוקן: {e}")
        return (RUNPOD_API_KEY if RUNPOD_API_KEY else None), True


def check_fallback_allowance(user_email: str) -> tuple[bool, float, float]:
    """
    בודק אם המשתמש רשום כ-fallback ואם עדיין יש לו יתרה.
    אם המשתמש לא קיים כלל – נוצרה לו רשומה חדשה עם used_credits=0 ו-limit_credits=FALLBACK_LIMIT_DEFAULT.
    שים לב: כאן **לא** נשמר טוקן מוצפן, רק מגבלת הקרדיט.
    """
    row = get_account(user_email)

    if not row:
        # יצירת משתמש fallback חדש – בלי runpod_token_encrypted!
        payload = {
            "user_email": user_email,
            "used_credits": 0.0,
            "limit_credits": FALLBACK_LIMIT_DEFAULT,
        }
        supabase.table("accounts").insert(payload).execute()
        return True, 0.0, FALLBACK_LIMIT_DEFAULT

    used = float(row.get("used_credits") or 0.0)
    limit = float(row.get("limit_credits") or FALLBACK_LIMIT_DEFAULT)
    return (used < limit), used, limit


def add_fallback_usage(user_email: str, amount_usd: float):
    """
    מעדכן used_credits למשתמש fallback.
    """
    row = get_account(user_email)
    used = float((row or {}).get("used_credits") or 0.0)
    new_used = round(used + amount_usd, 6)
    supabase.table("accounts").update({"used_credits": new_used}).eq("user_email", user_email).execute()
    return new_used


def estimate_cost_from_response(resp_json: dict) -> float:
    """
    מעריך עלות על בסיס executionTime ממבנים שונים בתגובה של RunPod.
    מחזיר ערך מדויק גם אם זמן העיבוד קצר מאוד.
    צפוי לעבוד בעיקר על התגובה של /status כשהמשימה COMPLETED.
    """
    try:
        ms = 0

        # 1️⃣ ניסיון ב-top-level
        if "executionTime" in resp_json:
            ms = resp_json.get("executionTime") or 0

        # 2️⃣ ניסיון ב-output כאובייקט
        if not ms and isinstance(resp_json.get("output"), dict):
            ms = resp_json["output"].get("executionTime") or 0

        # 3️⃣ ניסיון ב-output כרשימה
        if not ms and isinstance(resp_json.get("output"), list) and resp_json["output"]:
            first = resp_json["output"][0]
            ms = first.get("executionTime") or 0

        seconds = float(ms) / 1000.0
        cost = seconds * RUNPOD_RATE_PER_SEC

        if cost > 0:
            print(f"⏱ זמן עיבוד כולל: {seconds:.2f} שניות → עלות מוערכת: {cost:.8f}$")
        else:
            print("⚠️ זמן עיבוד לא זוהה בתגובה של RunPod:", resp_json.keys())

        return round(cost, 8)
    except Exception as e:
        print(f"❌ שגיאה ב-estimate_cost_from_response: {e}")
        return 0.0


# 🔢 שליפת יתרה אמיתית מרנפוד באמצעות GraphQL (לטוקן אישי)
def get_real_runpod_balance(token: str) -> tuple[float, bool]:
    """
    מבצע קריאת GraphQL ל-RunPod ומחזיר:
    (clientBalance כ-float, is_valid כ-bool).

    is_valid == False → שגיאת הרשאה / טוקן לא תקין / שגיאה ב-GraphQL.
    is_valid == True  → הקריאה הצליחה; גם אם היתרה 0, זה עדיין טוקן תקין.
    """
    try:
        payload = {
            "query": "{ myself { clientBalance hostBalance } }"
        }
        r = requests.post(
            "https://api.runpod.io/graphql",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )

        if not r.ok:
            print(f"❌ GraphQL account fetch failed: status={r.status_code}, body={r.text}")
            return 0.0, False

        data = r.json() or {}

        # אם יש errors ב-GraphQL – הטוקן לא תקין / אין גישה
        if "errors" in data:
            print(f"❌ GraphQL errors: {data['errors']}")
            return 0.0, False

        myself = (data.get("data") or {}).get("myself") or None
        if not myself or "clientBalance" not in myself:
            print(f"❌ GraphQL response missing clientBalance: {data}")
            return 0.0, False

        bal = float(myself.get("clientBalance", 0.0))
        return bal, True
    except Exception as e:
        print(f"❌ Error parsing GraphQL balance: {e}")
        return 0.0, False


# ───────────────────────────────────────────────
@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(None)):
    """
    מקבל קובץ מהקליינט, שומר זמנית בשרת ומחזיר URL גישה.
    """
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


# ───────────────────────────────────────────────
# 📥 שליפת קובץ מדרייב לשרת (לתמלול)
@app.get("/fetch-and-store-audio")
async def fetch_and_store_audio(request: Request, file_id: str):
    """
    שולף קובץ מדרייב, שומר זמנית, מחזיר URL.
    תומך ב-Authorization header עם Bearer token.
    """
    try:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse({"error": "חסר access token של Google"}, status_code=400)

        token = auth_header.split("Bearer ")[1]
        headers = {"Authorization": f"Bearer {token}"}
        drive_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

        res = requests.get(drive_url, headers=headers, stream=True)
        if not res.ok:
            return JSONResponse({"error": f"שגיאה בשליפת קובץ מדרייב: {res.text}"}, status_code=res.status_code)

        content_type = res.headers.get("Content-Type", "application/octet-stream")
        ext_map = {
            "audio/mp4": ".m4a",
            "audio/x-m4a": ".m4a",
            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
            "video/mp4": ".mp4",
        }
        ext = ext_map.get(content_type, ".audio")

        filename = f"drive_{file_id}_{int(time.time())}{ext}"
        file_path = os.path.join(UPLOAD_DIR, filename)

        with open(file_path, "wb") as f:
            for chunk in res.iter_content(chunk_size=8192):
                f.write(chunk)

        delete_later(file_path)
        file_url = f"{BASE_URL}/files/{quote(filename)}"
        print(f"✅ נשמר קובץ מדרייב: {file_path} ({content_type})")
        return JSONResponse({"url": file_url})

    except Exception as e:
        print(f"❌ /fetch-and-store-audio error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ───────────────────────────────────────────────
@app.post("/transcribe")
async def transcribe(request: Request):
    """
    שליחת בקשת תמלול ל-RunPod.

    ⚠️ שים לב:
    - כאן **לא** מחויבים קרדיטים.
    - החיוב נעשה רק ב-/status כשהסטטוס COMPLETED ויש executionTime.
    """
    try:
        data = await request.json()
        user_email = data.get("user_email")
        if not user_email:
            return JSONResponse({"error": "user_email is required"}, status_code=400)

        # 🔑 שליפת טוקן (אישי או fallback)
        token_to_use, using_fallback = get_user_token(user_email)

        if not token_to_use:
            return JSONResponse(
                {
                    "error": "לא הוגדר טוקן לשימוש (אין טוקן אישי ואין RUNPOD_API_KEY בשרת).",
                    "action": "יש להזין טוקן RunPod אישי"
                },
                status_code=401,
            )

        # 🔒 בדיקת מגבלת שימוש (רק למשתמשים על fallback)
        if using_fallback:
            allowed, used, limit = check_fallback_allowance(user_email)
            if not allowed:
                return JSONResponse(
                    {
                        "error": "חריגה ממגבלת שימוש",
                        "used": used,
                        "limit": limit,
                        "action": "יש להזין טוקן RunPod אישי"
                    },
                    status_code=402,
                )

        # 🎯 בניית גוף הבקשה ל-RunPod
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

        # 🚀 שליחה ל-RunPod (asynchronous run)
        response = requests.post(
            "https://api.runpod.ai/v2/lco4rijwxicjyi/run",
            headers={"Authorization": f"Bearer {token_to_use}", "Content-Type": "application/json"},
            json=run_body,
            timeout=180,
        )

        out = response.json() if response.content else {}
        status_code = response.status_code if response.status_code else 200

        # כאן **לא** מחשבים עלות, רק מחזירים את המזהה והסטטוס הראשוני
        print(f"🚀 /transcribe → user={user_email}, using_fallback={using_fallback}, resp_keys={list(out.keys())}")
        return JSONResponse(content=out, status_code=status_code)

    except Exception as e:
        print(f"❌ /transcribe error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


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
            token_to_use, _ = get_user_token(None)
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
        # 📘 עדכון קרדיטים למשתמש fallback
        # ───────────────────────────────────────────
        if user_email and 'using_fallback' in locals() and using_fallback and out.get("status") == "COMPLETED":
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
                print("⚖️ עלות לא אותרה או אפסית בתגובה של RunPod.")

        # ───────────────────────────────────────────
        # 🗄 עדכון רשומת התמלול במסד הנתונים
        # ───────────────────────────────────────────
        if str(out.get("status", "")).lower() == "completed":
            # 1️⃣ שליפת מזהה הרשומה (record_id) לפי job_id
            rec = (
                supabase.table("transcriptions")
                .select("id")
                .eq("job_id", job_id)
                .maybe_single()
                .execute()
            )
            record = rec.data if hasattr(rec, "data") else None

            if record and record.get("id"):
                record_id = record["id"]

                # 2️⃣ זמן עיבוד בפועל
                exec_ms = out.get("executionTime", 0)
                exec_sec = float(exec_ms) / 1000.0

                # ⭐⭐ 3️⃣ שליפת אורך האודיו מתוך RunPod — Option A ⭐⭐
                audio_len = None
                try:
                    outputs = out.get("output") or []
                    if isinstance(outputs, list) and len(outputs) > 0:
                        # המקטע האחרון → משם duration אמיתי
                        final_segment = outputs[0]["result"][-1][-1]
                        audio_len = float(final_segment["extra_data"].get("duration", 0.0))
                except Exception as e:
                    print("⚠️ לא ניתן לחלץ duration:", e)

                # אם לא נמצא → נ fallback ל-0
                audio_len = audio_len or 0.0

                # ⭐⭐ 4️⃣ יחס עיבוד ⭐⭐
                ratio = exec_sec / audio_len if audio_len > 0 else None

                # ⭐⭐ 5️⃣ חיוב ⭐⭐
                billing = exec_sec * 0.00016

                # ⭐⭐ 6️⃣ זמן boot של ה-Worker ⭐⭐
                delay_ms = out.get("delayTime", 0)
                boot_sec = float(delay_ms) / 1000.0

                # ⭐⭐ 7️⃣ זמן עיבוד משוער (8%) ⭐⭐
                estimated = audio_len * 0.08 if audio_len > 0 else None

                # ⭐⭐ 8️⃣ עדכון במסד ⭐⭐
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
        # החזרת תשובת RunPod כפי שהיא
        # ───────────────────────────────────────────
        return JSONResponse(content=out, status_code=r.status_code)

    except Exception as e:
        print(f"❌ /status error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)



# ───────────────────────────────────────────────
@app.get("/effective-balance")
def effective_balance(user_email: str):
    """
    מחזיר יתרה אפקטיבית למשתמש.

    - אם המשתמש לא קיים → נוצרת רשומת fallback חדשה (used_credits=0, limit_credits=FALLBACK_LIMIT_DEFAULT).
    - אם יש טוקן מוצפן אישי → נבדקת היתרה האמיתית ב-RunPod (GraphQL account API).
      אם הטוקן האישי **לא תקין** → מוחקים אותו, עוברים ל-fallback ומחזירים need_token=True.
    - אחרת → נעשה שימוש ביתרת fallback (limit - used_credits).

    תמיד מחזירים balance כמחרוזת בפורמט עם 6 ספרות עשרוניות.
    """
    try:
        # 🟢 בדיקה אם המשתמש כבר קיים במסד
        row = get_account(user_email)

        # 🆕 אם אין רשומה – צור חדשה כ-fallback בלבד (בלי טוקן מוצפן)
        if not row:
            payload = {
                "user_email": user_email,
                "used_credits": 0.0,
                "limit_credits": FALLBACK_LIMIT_DEFAULT,
            }
            supabase.table("accounts").insert(payload).execute()
            balance_str = f"{FALLBACK_LIMIT_DEFAULT:.6f}"
            print(f"💰 יתרה נוכחית של {user_email}: {balance_str}$ (new fallback account)")
            return JSONResponse({
                "balance": balance_str,
                "need_token": False
            })

        # 🪙 אם יש טוקן מוצפן – נבדוק יתרה אמיתית בחשבון RunPod (GraphQL)
        enc = row.get("runpod_token_encrypted")
        if enc:
            token = decrypt_token(enc)
            if token:
                bal, valid = get_real_runpod_balance(token)

                if valid:
                    balance_str = f"{bal:.6f}"
                    print(f"💰 יתרה נוכחית של {user_email}: {balance_str}$ (personal token)")
                    # למשתמש עם טוקן אישי לא נבקש שוב להזין טוקן – גם אם היתרה 0
                    return JSONResponse({
                        "balance": balance_str,
                        "need_token": False
                    })
                else:
                    # 🔴 טוקן אישי לא תקין → מוחקים אותו ועוברים למצב fallback
                    print(f"⚠️ טוקן אישי לא תקין עבור {user_email} – מעבר ל-fallback ומבוקש טוקן חדש.")
                    supabase.table("accounts").update(
                        {
                            "runpod_token_encrypted": None,
                            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        }
                    ).eq("user_email", user_email).execute()
                    # נפיל למטה לחישוב fallback + need_token=True

        # 🧮 אחרת – נחשב יתרת fallback פנימית
        used = float(row.get("used_credits") or 0.0)
        limit = float(row.get("limit_credits") or FALLBACK_LIMIT_DEFAULT)
        remaining = max(limit - used, 0.0)
        balance_str = f"{remaining:.6f}"

        print(f"💰 יתרה נוכחית של {user_email}: {balance_str}$ (fallback)")
        # אם הגענו לכאן אחרי טוקן אישי לא תקין – נרצה שהלקוח ידע שצריך להזין טוקן חדש
        need_token_flag = remaining <= 0 or (enc is not None)
        return JSONResponse({
            "balance": balance_str,
            "need_token": need_token_flag
        })

    except Exception as e:
        print(f"❌ /effective-balance error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ───────────────────────────────────────────────
# 🧱 ניהול תמלולים מאובטח דרך השרת
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


# ───────────────────────────────────────────────
@app.post("/save-token")
async def save_token(request: Request):
    """
    שומר טוקן RunPod אישי מוצפן למשתמש ב-Supabase.

    מרגע שיש טוקן אישי:
    - לא משתמשים יותר ב-RUNPOD_API_KEY עבורו.
    - לא מגבילים אותו לפי FALLBACK_LIMIT_DEFAULT (החיוב ב-RunPod עליו).
    - מתבצעת בדיקת תקינות מול RunPod (GraphQL) לפני השמירה.
    """
    try:
        data = await request.json()
        user_email = data.get("user_email")
        token = data.get("token")

        if not user_email or not token:
            return JSONResponse({"error": "חסר user_email או token"}, status_code=400)
        if not ENCRYPTION_KEY:
            return JSONResponse({"error": "ENCRYPTION_KEY לא מוגדר בשרת"}, status_code=500)

        # ✔️ בדיקת תקינות טוקן מול RunPod (כולל clientBalance)
        balance, valid = get_real_runpod_balance(token)
        if not valid:
            return JSONResponse({"error": "טוקן RunPod שגוי או לא מורשה"}, status_code=400)

        # ✔️ הצפנה
        key = ENCRYPTION_KEY.encode("utf-8")
        iv = os.urandom(16)
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        padding_len = AES.block_size - len(token.encode()) % AES.block_size
        padded = token.encode() + bytes([padding_len]) * padding_len
        encrypted = base64.b64encode(iv + cipher.encrypt(padded)).decode()

        row = get_account(user_email)
        if row:
            supabase.table("accounts").update(
                {
                    "runpod_token_encrypted": encrypted,
                    "used_credits": 0.0,  # איפוס fallback – מרגע זה החיוב על המשתמש
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
            ).eq("user_email", user_email).execute()
        else:
            supabase.table("accounts").insert(
                {
                    "user_email": user_email,
                    "runpod_token_encrypted": encrypted,
                    "used_credits": 0.0,
                    "limit_credits": FALLBACK_LIMIT_DEFAULT,
                }
            ).execute()

        # מחזירים גם את היתרה האמיתית של המשתמש ב-RunPod (נוח ל־UI בעתיד)
        return JSONResponse({
            "status": "ok",
            "balance": f"{float(balance):.6f}"
        })
    except Exception as e:
        print(f"❌ /save-token error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

# ────────────────────────────────בדיקת תמלול שהושלם אם המשתמש התנתק לפני קבלת התמלול

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
        return JSONResponse({"error": str(e)}, status_code=500)
