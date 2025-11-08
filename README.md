# 🧠 שרת העלאת קבצים ותמיכה בתמלול – my-transcribe-proxy

שרת פשוט המבוסס על **FastAPI**, המאפשר:
1. העלאת קובצי אודיו (`.mp3`, `.wav`, `.ogg`, וכו’)
2. שמירה זמנית של הקובץ למשך שעה
3. קבלת כתובת ציבורית (URL) לשימוש חיצוני – לדוגמה עבור שירותי תמלול ב-AI
4. מחיקה אוטומטית של הקובץ לאחר שעה
5. שירות `/ping` לשמירה על פעילות השרת ב־Render (באמצעות UptimeRobot)

---

## ⚙️ התקנה והרצה מקומית

```bash
git clone https://github.com/<your-username>/my-transcribe-proxy.git
cd my-transcribe-proxy
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 10000
