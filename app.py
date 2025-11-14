# ───────────────────────────────────────────────
# TEAM & BILLING RESOLUTION MODULE
# ───────────────────────────────────────────────

def get_team_by_id(team_id: str):
    """שליפת קבוצה לפי team_id"""
    try:
        res = supabase.table("teams").select("*").eq("id", team_id).maybe_single().execute()
        return res.data if hasattr(res, "data") else None
    except:
        return None


def get_teams_for_member(user_email: str):
    """בדיקה אם המשתמש חבר בקבוצה כלשהי"""
    try:
        rows = (
            supabase.table("team_members")
            .select("team_id")
            .eq("member_email", user_email)
            .execute()
        )
        ids = [r["team_id"] for r in (rows.data or [])]
        if not ids:
            return []
        # שליפת פרטי הקבוצות
        teams = (
            supabase.table("teams").select("*").in_("id", ids).execute()
        )
        return teams.data or []
    except:
        return []


def get_team_member(team_id: str, user_email: str):
    """שליפת פרטי משתמש בקבוצה (quota, used_seconds)"""
    try:
        res = (
            supabase.table("team_members")
            .select("*")
            .eq("team_id", team_id)
            .eq("member_email", user_email)
            .maybe_single()
            .execute()
        )
        return res.data if hasattr(res, "data") else None
    except:
        return None


def decrypt_team_token(enc: str):
    """פענוח טוקן קבוצתי"""
    try:
        if not ENCRYPTION_KEY:
            return None
        key = ENCRYPTION_KEY.encode("utf-8")
        data = base64.b64decode(enc)
        iv, ciphertext = data[:16], data[16:]
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        decrypted = unpad(cipher.decrypt(ciphertext), AES.block_size)
        return decrypted.decode("utf-8")
    except:
        return None


def add_team_usage(team_id: str, member_email: str, transcription_id: str, seconds_used: float):
    """רישום שימוש בטבלת team_usage"""
    try:
        supabase.table("team_usage").insert({
            "team_id": team_id,
            "member_email": member_email,
            "transcription_id": transcription_id,
            "seconds_used": seconds_used
        }).execute()
    except Exception as e:
        print("❌ add_team_usage:", e)


def update_team_member_usage(team_id: str, member_email: str, seconds: float):
    """עדכון סכום השימוש של חבר בקבוצה"""
    try:
        member = get_team_member(team_id, member_email)
        current = float(member.get("used_seconds") or 0.0)
        new_val = round(current + seconds, 4)
        supabase.table("team_members").update({"used_seconds": new_val}).eq("id", member["id"]).execute()
    except Exception as e:
        print("❌ update_team_member_usage:", e)


# ───────────────────────────────────────────────
# BILLING RESOLVER
# ───────────────────────────────────────────────

def resolve_billing_source(user_email: str, requested_mode: str = None, team_id: str = None):
    """
    קובע מMode חיוב:
    guest | personal | team

    החזרת:
    {
      "mode": ...,
      "token": ...,
      "team_id": ...,
      "allowed": True/False,
      "reason": "...",
      "guest_remaining": float,
      "team_remaining": float
    }
    """

    # --- בסיס החזרה ---
    out = {
        "mode": None,
        "token": None,
        "team_id": None,
        "allowed": True,
        "reason": "",
        "guest_remaining": 0.0,
        "team_remaining": None
    }

    # 🟦 שליפת חשבון
    account = get_account(user_email)

    # 🟦 מצב Guest (fallback)
    guest_used = float(account.get("used_credits") if account else 0.0)
    guest_limit = float(account.get("limit_credits") if account else FALLBACK_LIMIT_DEFAULT)
    guest_remaining = max(guest_limit - guest_used, 0.0)
    out["guest_remaining"] = guest_remaining

    # 🟦 בדיקת טוקן אישי
    personal_token_enc = account.get("runpod_token_encrypted") if account else None
    personal_token = decrypt_token(personal_token_enc) if personal_token_enc else None

    # 🟦 קבוצות
    teams = get_teams_for_member(user_email)
    team_mode_allowed = False
    selected_team = None
    team_member_row = None

    if team_id:
        selected_team = get_team_by_id(team_id)
        if selected_team:
            team_member_row = get_team_member(team_id, user_email)
            if team_member_row:
                team_mode_allowed = True

    # 🟦 לוגיקה של בחירה לפי requested_mode
    # ------------------------------------------------
    if requested_mode == "personal":
        if personal_token:
            out["mode"] = "personal"
            out["token"] = personal_token
            return out
        else:
            out["allowed"] = False
            out["reason"] = "אין טוקן אישי תקין"
            return out

    if requested_mode == "guest":
        if guest_remaining > 0:
            out["mode"] = "guest"
            out["token"] = RUNPOD_API_KEY
            return out
        else:
            out["allowed"] = False
            out["reason"] = "אין יתרת אורח"
            return out

    if requested_mode == "team":
        if team_mode_allowed and selected_team:
            enc_team_token = selected_team.get("runpod_token_encrypted")
            team_token = decrypt_team_token(enc_team_token)
            if not team_token:
                out["allowed"] = False
                out["reason"] = "טוקן קבוצה לא תקין"
                return out

            # בדיקת מכסת משתמש בקבוצה
            quota = team_member_row.get("quota_seconds")
            used = float(team_member_row.get("used_seconds") or 0.0)
            if quota and used >= quota:
                out["allowed"] = False
                out["reason"] = "חריגה ממכסת קבוצתית"
                return out

            out["mode"] = "team"
            out["token"] = team_token
            out["team_id"] = team_id
            out["team_remaining"] = (quota - used) if quota else None
            return out

        out["allowed"] = False
        out["reason"] = "המשתמש אינו חבר בקבוצה"
        return out

    # 🟦 fallback בחירה אוטומטית: אם לא צוין requested_mode
    # סדר עדיפויות:
    # 1. personal
    # 2. team (אם יש)
    # 3. guest

    if personal_token:
        out["mode"] = "personal"
        out["token"] = personal_token
        return out

    if teams:
        # קבוצה ראשונה ברשימה כבר תקפה
        t = teams[0]
        member = get_team_member(t["id"], user_email)
        enc_team_token = t["runpod_token_encrypted"]
        team_token = decrypt_team_token(enc_team_token)

        out["mode"] = "team"
        out["token"] = team_token
        out["team_id"] = t["id"]
        out["team_remaining"] = None
        return out

    # otherwise guest
    if guest_remaining > 0:
        out["mode"] = "guest"
        out["token"] = RUNPOD_API_KEY
        return out

    out["allowed"] = False
    out["reason"] = "אין שום מקור חיוב זמין"
    return out
# ───────────────────────────────────────────────
# TEAM MANAGEMENT API
# ───────────────────────────────────────────────

@app.post("/team/create")
async def team_create(request: Request):
    """
    יצירת קבוצה חדשה:
    - הצפנת טוקן קבוצתי
    - שמירת בעלים כראש הקבוצה
    - הוספת הבעלים כחבר קבוצה ללא מכסה
    """

    try:
        data = await request.json()
        owner_email = data.get("owner_email")
        team_name = data.get("team_name", "Untitled Team")
        token = data.get("token")

        if not owner_email or not token:
            return JSONResponse(
                {"error": "חסר owner_email או token"},
                status_code=400
            )

        # בדיקת תקינות טוקן RunPod
        balance, valid = get_real_runpod_balance(token)
        if not valid:
            return JSONResponse({"error": "טוקן RunPod לא תקין"}, status_code=400)

        # הצפנה
        key = ENCRYPTION_KEY.encode("utf-8")
        iv = os.urandom(16)
        cipher = AES.new(key[:32], AES.MODE_CBC, iv)
        padding_len = AES.block_size - len(token.encode()) % AES.block_size
        padded = token.encode() + bytes([padding_len]) * padding_len
        encrypted = base64.b64encode(iv + cipher.encrypt(padded)).decode()

        # יצירת הקבוצה
        team_row = (
            supabase.table("teams")
            .insert({
                "owner_email": owner_email,
                "team_name": team_name,
                "runpod_token_encrypted": encrypted
            })
            .execute()
        )

        team = team_row.data[0]

        # הוספת הבעלים כחבר הקבוצה
        supabase.table("team_members").insert({
            "team_id": team["id"],
            "member_email": owner_email,
            "quota_seconds": None,
            "used_seconds": 0
        }).execute()

        return JSONResponse({"status": "ok", "team": team})

    except Exception as e:
        print("❌ /team/create:", e)
        return JSONResponse({"error": str(e)}, status_code=500)



@app.post("/team/add-member")
async def team_add_member(request: Request):
    """
    הוספת חבר לקבוצה:
    - בדיקת בעלות
    - בדיקה שהמשתמש לא חבר כבר
    """

    try:
        data = await request.json()
        team_id = data.get("team_id")
        member_email = data.get("member_email")
        quota_seconds = data.get("quota_seconds", None)
        requester = data.get("requester_email")  # בעלים בלבד

        if not team_id or not member_email or not requester:
            return JSONResponse({"error": "Missing parameters"}, status_code=400)

        # בדיקה שהRequester הוא הבעלים
        team = get_team_by_id(team_id)
        if not team:
            return JSONResponse({"error": "Team not found"}, status_code=404)

        if team["owner_email"] != requester:
            return JSONResponse({"error": "Not authorized"}, status_code=403)

        # בדיקה שהמשתמש לא כבר חבר
        existing = get_team_member(team_id, member_email)
        if existing:
            return JSONResponse({"error": "Already a member"}, status_code=409)

        # הוספה
        row = (
            supabase.table("team_members")
            .insert({
                "team_id": team_id,
                "member_email": member_email,
                "quota_seconds": quota_seconds,
                "used_seconds": 0
            })
            .execute()
        )

        return JSONResponse({"status": "ok", "member": row.data})

    except Exception as e:
        print("❌ /team/add-member:", e)
        return JSONResponse({"error": str(e)}, status_code=500)



@app.post("/team/remove-member")
async def team_remove_member(request: Request):
    """הסרת חבר מקבוצה"""

    try:
        data = await request.json()
        team_id = data.get("team_id")
        member_email = data.get("member_email")
        requester = data.get("requester_email")

        if not team_id or not member_email or not requester:
            return JSONResponse({"error": "Missing parameters"}, status_code=400)

        team = get_team_by_id(team_id)
        if not team:
            return JSONResponse({"error": "Team not found"}, status_code=404)

        if team["owner_email"] != requester:
            return JSONResponse({"error": "Not authorized"}, status_code=403)

        supabase.table("team_members").delete().eq("team_id", team_id).eq("member_email", member_email).execute()

        return JSONResponse({"status": "removed"})

    except Exception as e:
        print("❌ /team/remove-member:", e)
        return JSONResponse({"error": str(e)}, status_code=500)



@app.get("/team/info")
async def team_info(team_id: str, requester_email: str):
    """
    מחזיר:
    - פרטי קבוצה
    - רשימת חברים
    - שימושים מצטברים (team_usage)
    """

    try:
        team = get_team_by_id(team_id)
        if not team:
            return JSONResponse({"error": "Team not found"}, status_code=404)

        if team["owner_email"] != requester_email:
            return JSONResponse({"error": "Not authorized"}, status_code=403)

        members = (
            supabase.table("team_members")
            .select("*")
            .eq("team_id", team_id)
            .execute()
        ).data or []

        usage = (
            supabase.table("team_usage")
            .select("*")
            .eq("team_id", team_id)
            .order("created_at", desc=True)
            .execute()
        ).data or []

        return JSONResponse({
            "team": team,
            "members": members,
            "usage": usage
        })

    except Exception as e:
        print("❌ /team/info:", e)
        return JSONResponse({"error": str(e)}, status_code=500)



@app.post("/team/update-quota")
async def team_update_quota(request: Request):
    """עדכון מכסה של חבר בקבוצה"""

    try:
        data = await request.json()
        team_id = data.get("team_id")
        member_email = data.get("member_email")
        quota_seconds = data.get("quota_seconds")
        requester = data.get("requester_email")

        team = get_team_by_id(team_id)
        if not team:
            return JSONResponse({"error": "Team not found"}, status_code=404)

        if team["owner_email"] != requester:
            return JSONResponse({"error": "Not authorized"}, status_code=403)

        supabase.table("team_members").update(
            {"quota_seconds": quota_seconds}
        ).eq("team_id", team_id).eq("member_email", member_email).execute()

        return JSONResponse({"status": "ok"})

    except Exception as e:
        print("❌ /team/update-quota:", e)
        return JSONResponse({"error": str(e)}, status_code=500)
# ───────────────────────────────────────────────
# /transcribe — גרסה חדשה מלאה
# ───────────────────────────────────────────────

@app.post("/transcribe")
async def transcribe(request: Request):
    """
    שליחת בקשת תמלול ל-RunPod.
    עכשיו כולל:
    - Billing Resolver (guest / personal / team)
    - בדיקות quota
    - בדיקות הרשאה
    - שליחה לטוקן הנכון
    """

    try:
        data = await request.json()
        user_email = data.get("user_email")
        requested_mode = data.get("source")         # "guest" | "personal" | "team" | None
        requested_team_id = data.get("team_id")     # אם NEEDED ע"י המשתמש

        if not user_email:
            return JSONResponse({"error": "user_email is required"}, status_code=400)

        # 🎯 קריאה לבילינג ריזולבר
        billing = resolve_billing_source(
            user_email=user_email,
            requested_mode=requested_mode,
            team_id=requested_team_id
        )

        if not billing["allowed"]:
            return JSONResponse(
                {"error": billing["reason"], "mode": billing["mode"]},
                status_code=402
            )

        # הטוקן לשימוש בפועל
        token_to_use = billing["token"]
        if not token_to_use:
            return JSONResponse(
                {"error": "אין טוקן לשימוש במקור חיוב שנבחר"},
                status_code=401
            )

        # בניית גוף הבקשה ל־RunPod בדיוק כמו קודם
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

        # 🚀 שליחת התמלול ל-RunPod
        response = requests.post(
            "https://api.runpod.ai/v2/lco4rijwxicjyi/run",
            headers={"Authorization": f"Bearer {token_to_use}", "Content-Type": "application/json"},
            json=run_body,
            timeout=180,
        )

        out = response.json() if response.content else {}
        status_code = response.status_code if response.status_code else 200

        print(f"🚀 /transcribe → user={user_email}, mode={billing['mode']}, team={billing.get('team_id')}")

        # החזרה ללקוח
        return JSONResponse(
            content={
                **out,
                "billing_mode": billing["mode"],
                "team_id": billing.get("team_id"),
            },
            status_code=status_code
        )

    except Exception as e:
        print(f"❌ /transcribe error:", e)
        return JSONResponse({"error": str(e)}, status_code=500)




# ───────────────────────────────────────────────
# /status — גרסה חדשה מלאה
# ───────────────────────────────────────────────

@app.get("/status/{job_id}")
def get_job_status(job_id: str, user_email: str | None = None):
    """
    סטטוס משימה + ניהול חיוב:
    guest → accounts.used_credits
    personal → לא מעדכנים DB
    team → team_members.used_seconds + team_usage
    """

    try:
        # אם אין user_email → לא מעדכנים כלום
        if not user_email:
            token_to_use, _ = get_user_token(None)
        else:
            # שליפת account כדי לדעת איזה mode השתמש בפועל
            acc = get_account(user_email)

            # שליפת מצב תמלול מה-db
            # (נערוך התאמה: הלקוח צריך להעביר transcription_id בהמשך)
            # אבל כדי לשמור על תאימות אחורה —
            # ננסה לפחות לקבוע fallback לטוקן
            token_to_use, _ = get_user_token(user_email)

        if not token_to_use:
            return JSONResponse({"error": "Missing token"}, status_code=401)

        # שליפה מ-RunPod
        r = requests.get(
            f"https://api.runpod.ai/v2/lco4rijwxicjyi/status/{job_id}",
            headers={"Authorization": f"Bearer {token_to_use}"},
            timeout=30,
        )
        if not r.ok:
            return JSONResponse({"error": "שגיאה בשליפת סטטוס מ-RunPod"}, status_code=r.status_code)

        out = r.json() if r.content else {}

        # רק אם COMPLETED יש חיוב
        if user_email and out.get("status") == "COMPLETED":
            cost = estimate_cost_from_response(out)
            seconds_used = out.get("executionTime", 0) / 1000.0

            # ננסה לברר את "mode" הידוע מהבקשה
            mode = out.get("billing_mode") or None  # אם הפרונט יחזיר בעתיד
            team_id = out.get("team_id") or None

            # 🔵 Personal Mode
            if mode == "personal":
                # לא מעדכנים DB כלל
                pass

            # 🟣 Team Mode
            elif mode == "team" and team_id:
                # 1) הוספת רישום לשימוש
                add_team_usage(team_id, user_email, job_id, seconds_used)

                # 2) עדכון used_seconds להצבת quota
                update_team_member_usage(team_id, user_email, seconds_used)

            # 🟪 Guest Mode
            else:
                # fallback user — עלות בדולרים
                if cost > 0:
                    new_used = add_fallback_usage(user_email, cost)
                    remaining = max(FALLBACK_LIMIT_DEFAULT - new_used, 0.0)
                    out["_usage"] = {
                        "estimated_cost_usd": cost,
                        "used_credits": new_used,
                        "remaining": remaining
                    }

        return JSONResponse(content=out, status_code=r.status_code)

    except Exception as e:
        print("❌ /status error:", e)
        return JSONResponse({"error": str(e)}, status_code=500)




# ───────────────────────────────────────────────
# שינוי preferred_mode של משתמש
# ───────────────────────────────────────────────

@app.post("/user/update-preferred-mode")
async def update_preferred_mode(request: Request):
    """
    שינוי מצב ברירת המחדל של המשתמש:
    guest / personal / team
    """

    try:
        data = await request.json()
        user_email = data.get("user_email")
        preferred = data.get("preferred_mode")

        if not user_email or not preferred:
            return JSONResponse({"error": "Missing user_email or preferred_mode"}, status_code=400)

        supabase.table("accounts").update(
            {"preferred_mode": preferred}
        ).eq("user_email", user_email).execute()

        return JSONResponse({"status": "ok", "preferred_mode": preferred})

    except Exception as e:
        print("❌ /user/update-preferred-mode:", e)
        return JSONResponse({"error": str(e)}, status_code=500)
