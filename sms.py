"""SMS yuborish (Eskiz.uz). Kalitlar .env'da: ESKIZ_EMAIL, ESKIZ_PASSWORD, (ixtiyoriy) ESKIZ_FROM.
Kalitlar bo'lmasa — SMS yuborilmaydi (False qaytadi), tizim Telegram/ilova orqali ishlashda davom etadi.
SMS_MODE=log bo'lsa faqat logga yozadi (test uchun)."""
import os
import time
import httpx

_token = {"v": None, "exp": 0.0}


def sms_configured() -> bool:
    return os.getenv("SMS_MODE") == "log" or bool(os.getenv("ESKIZ_EMAIL") and os.getenv("ESKIZ_PASSWORD"))


async def _get_token(client: httpx.AsyncClient):
    if _token["v"] and _token["exp"] > time.time():
        return _token["v"]
    r = await client.post("https://notify.eskiz.uz/api/auth/login",
                          data={"email": os.getenv("ESKIZ_EMAIL"), "password": os.getenv("ESKIZ_PASSWORD")})
    if r.status_code != 200:
        return None
    _token["v"] = (r.json().get("data") or {}).get("token")
    _token["exp"] = time.time() + 25 * 24 * 3600
    return _token["v"]


async def send_sms(phone: str, text: str) -> bool:
    phone = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(phone) < 9:
        return False
    if os.getenv("SMS_MODE") == "log":
        print(f"[SMS-LOG] {phone}: {text}")
        return True
    if not (os.getenv("ESKIZ_EMAIL") and os.getenv("ESKIZ_PASSWORD")):
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            tok = await _get_token(c)
            if not tok:
                return False
            r = await c.post("https://notify.eskiz.uz/api/message/sms/send",
                             headers={"Authorization": f"Bearer {tok}"},
                             data={"mobile_phone": phone[-12:] if len(phone) >= 12 else "998" + phone[-9:], "message": text,
                                   "from": os.getenv("ESKIZ_FROM", "4546")})
            if r.status_code == 401:
                _token["v"] = None
            return r.status_code == 200
    except Exception as e:
        print(f"[SMS] xato: {e}")
        return False
