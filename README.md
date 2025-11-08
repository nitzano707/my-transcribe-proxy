# my-transcribe-proxy 🎧

שרת פשוט להעברת קובצי אודיו לשירות התמלול של [ivrit.ai](https://transcribe.ivrit.ai).

## 🚀 איך זה עובד
1. מקבל קובץ אודיו (`POST /upload`)
2. שומר אותו זמנית
3. שולח אותו ל-`https://transcribe.ivrit.ai/upload`
4. מחזיר את תשובת התמלול
5. מוחק את הקובץ

---

## 🧰 הפעלה מקומית
```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 10000
