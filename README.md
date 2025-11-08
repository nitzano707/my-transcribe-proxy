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
```

---

## 📁 מבנה הקבצים

```
my-transcribe-proxy/
│
├── app.py                # קובץ השרת הראשי (כולל API מלא)
├── requirements.txt      # ספריות נדרשות להפעלה (FastAPI, Uvicorn, python-multipart)
└── README.md             # תיעוד המערכת
```

---

## 🚀 נקודות גישה (Endpoints)

### 1. `/upload`
העלאת קובץ אודיו ושמירתו באופן זמני.

#### סוג בקשה:
`POST`

#### סוגי גוף נתמכים:
- `multipart/form-data` → שדה בשם `file`
- `binary/raw` → העלאה ישירה של קובץ

#### דוגמה ב־Postman:
| Key | Type | Value |
|-----|------|--------|
| file | File | example.ogg |

#### תגובה לדוגמה:
```json
{
  "url": "https://my-transcribe-proxy.onrender.com/files/example.ogg",
  "message": "הקובץ הועלה בהצלחה ויימחק תוך שעה."
}
```

---

### 2. `/files/{filename}`
גישה או הורדה של קובץ שהועלה.

#### סוג בקשה:
`GET`

#### דוגמה:
```
https://my-transcribe-proxy.onrender.com/files/example.ogg
```

אם הקובץ נמחק או לא קיים:
```json
{"error": "הקובץ נמחק או לא נמצא (ייתכן שחלפה שעה מאז ההעלאה)."}
```

---

### 3. `/ping`
נקודת בדיקה פשוטה שנועדה לשמור את השרת **ער** ולא לאפשר לו להירדם  
(במיוחד בשירות Render בחשבון החינמי).

#### סוגי בקשה נתמכים:
`GET`, `HEAD`

#### תגובה:
```json
{"status": "ok"}
```

---

## 💡 שירות הערת השרת (UptimeRobot)

ברירת המחדל ב־Render (בתוכנית החינמית) היא להרדים שרתים ללא פעילות לאחר כ־15 דקות.  
כדי לשמור על השרת פעיל **24/7**, ניתן להשתמש בשירות חינמי בשם [UptimeRobot](https://uptimerobot.com/).

### איך מגדירים את השירות:

1. היכנס אל [UptimeRobot](https://uptimerobot.com/).
2. צור חשבון חינמי.
3. לחץ על **"Add New Monitor"**.
4. בחר סוג: **HTTP(s)**
5. בשדה **URL to monitor**, הזן:
   ```
   https://my-transcribe-proxy.onrender.com/ping
   ```
6. בחר אינטרוול (Interval): **5 דקות** (המינימום בתוכנית החינמית)
7. סמן **Email** כהתראה.
8. לחץ על **Create Monitor** ✅

כעת UptimeRobot ישלח בקשת HEAD או GET לשרת שלך כל כמה דקות —  
מה שיבטיח שהשרת שלך **יישאר פעיל תמיד**, גם ברנדר.

#### ניתן לבדוק את הפעילות בלוגים של Render:
```
INFO: "HEAD /ping HTTP/1.1" 200 OK
```
אם אתה רואה שורה כזו – השירות עובד בהצלחה 🎯

---

## 🔁 מחיקה אוטומטית של קבצים

לאחר כל העלאה, מופעל תהליך רקע (Thread) שמוחק את הקובץ אחרי שעה.  
המערכת אינה דורשת תחזוקה – המחיקה מתבצעת אוטומטית.

---

## 🧩 טכנולוגיות

| רכיב | תפקיד |
|------|--------|
| **FastAPI** | שרת API אסינכרוני |
| **Uvicorn** | שרת HTTP להרצת FastAPI |
| **Python 3.11+** | שפת הפיתוח |
| **Render** | סביבת פריסה בענן |
| **UptimeRobot** | שירות הערת שרתים חינמי |

---

## 📬 קרדיט

פותח ע״י **ד״ר ניצן אליקים (Nitzan Elyakim)**  
לצרכי פיתוח ושילוב מודלי תמלול בעברית בשירותים מבוססי AI.  
הקוד פתוח לשימוש חופשי ולשיפור קהילתי 🌍  
למידע נוסף או לשאלות – ניתן לפנות דרך [GitHub Issues](https://github.com/<your-username>/my-transcribe-proxy/issues)
