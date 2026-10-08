"""
ZooMo Delivery & J-Global Tijorat Tizimi
Telegram Bot va Mini App Integratsiyasi (Production-Ready Versiya)

Ushbu modul yuqori yuklama (high-load) sharoitida buyurtmalarni uzilishlarsiz 
qabul qilish, mijoz/kuryer bildirishnomalarini tezkor yuborish va 
Telegram Mini App initData xavfsizligini ta'minlash uchun mo'ljallangan.
"""

import os
import re
import time
import hashlib
import hmac
import json as _json
from urllib.parse import parse_qsl
import httpx

# Ekologik o'zgaruvchilardan tokenni olish
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else None

# Yuqori yuklamaga mo'ljallangan global HTTP Async Ulanishlar Hovuzi (Connection Pool)
# Limits: bir vaqtning o'zida 100 ta ulanish va 20 ta doimiy ochiq liniya (keep-alive)
http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(10.0, connect=5.0),
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
)


_cached_bot_username: str | None = None


async def get_bot_username() -> str:
    """Botning @username'ini qaytaradi — bu referal havolasini
    (https://t.me/BOTUSERNAME?start=...) tuzish uchun kerak.

    Avval .env'dagi TELEGRAM_BOT_USERNAME'ni tekshiradi (agar owner qo'lda
    kiritgan bo'lsa); bo'lmasa, Telegram'ning o'zidan (getMe orqali)
    AVTOMATIK so'raydi va keshlaydi — shunda owner buni qo'lda
    sozlashni birinchi safar oldini olishi mumkin, xatolik ehtimoli
    kamayadi."""
    global _cached_bot_username

    env_username = os.getenv("TELEGRAM_BOT_USERNAME")
    if env_username:
        return env_username.lstrip("@")

    if _cached_bot_username:
        return _cached_bot_username

    if not TELEGRAM_API_BASE:
        return ""

    try:
        resp = await http_client.get(f"{TELEGRAM_API_BASE}/getMe")
        data = resp.json()
        username = data.get("result", {}).get("username", "")
        if username:
            _cached_bot_username = username
        return username
    except Exception as e:
        print(f"[Ogohlantirish] Bot username'ni avtomatik aniqlab bo'lmadi: {e}")
        return ""


async def close_telegram_bot_client():
    """FastAPI server to'xtatilganda HTTP mijozni xotiradan xavfsiz o'chirish."""
    await http_client.aclose()


async def send_telegram_message(chat_id: int | str, text: str, reply_markup: dict | None = None) -> bool:
    """
    Berilgan chat_id'ga HTML formatida bildirishnoma yuboradi.
    
    Tizim qulamasligi uchun barcha xatoliklar ushlanadi hamda async logga yoziladi.
    Yuqori yuklamalarda so'rovlar navbat bilan, serverni sekinlashtirmasdan uzatiladi.
    """
    if not TELEGRAM_API_BASE:
        print("DIQQAT: TELEGRAM_BOT_TOKEN .env faylida topilmadi — xabar yuborish o'tkazib yuborildi.")
        return False
    if not chat_id:
        return False

    payload = {
        "chat_id": chat_id, 
        "text": text, 
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        resp = await http_client.post(f"{TELEGRAM_API_BASE}/sendMessage", json=payload)
        if resp.status_code == 200:
            return True
        else:
            print(f"[Xatolar Logi] Telegram API Xatosi: Status {resp.status_code} | Tafsilot: {resp.text}")
            return False
    except httpx.RequestError as exc:
        print(f"[Tarmoq Xatosi] Telegram serveriga ulanishda uzilish: {exc}")
        return False
    except Exception as e:
        print(f"[Kutilmagan Xatolik] send_telegram_message: {e}")
        return False


async def send_telegram_photo(
    chat_id: int | str,
    photo_file_id: str,
    caption: str | None = None,
    reply_markup: dict | None = None,
) -> bool:
    """Berilgan chat_id'ga, Telegram'ning o'zida saqlangan file_id orqali
    rasm yuboradi (bizda allaqachon bor rasmni — masalan foydalanuvchi
    yuborgan 'chek'ni — QAYTA boshqa chat'ga YO'NALTIRISH uchun; qayta
    yuklash shart emas, Telegram file_id orqali ichki nusxalaydi)."""
    if not TELEGRAM_API_BASE or not chat_id or not photo_file_id:
        return False

    payload = {"chat_id": chat_id, "photo": photo_file_id, "parse_mode": "HTML"}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        resp = await http_client.post(f"{TELEGRAM_API_BASE}/sendPhoto", json=payload)
        if resp.status_code == 200:
            return True
        print(f"[Xatolar Logi] Telegram sendPhoto xatosi: Status {resp.status_code} | {resp.text}")
        return False
    except httpx.RequestError as exc:
        print(f"[Tarmoq Xatosi] sendPhoto: {exc}")
        return False
    except Exception as e:
        print(f"[Kutilmagan Xatolik] send_telegram_photo: {e}")
        return False


def contact_request_keyboard() -> dict:
    """Foydalanuvchidan telefon raqamini tasdiqlashni so'rovchi rasmiy tugma."""
    return {
        "keyboard": [[{"text": "📱 Telefon raqamni ulashish", "request_contact": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def normalize_phone(raw_phone: str) -> str:
    """
    Har qanday formatdagi telefon raqamini yagona standart
    (+998XXXXXXXXX) ko'rinishiga keltiradi.

    Masalan: '90 123-45-67' -> '+998901234567', '998901234567' -> '+998901234567',
    '8 90 123 45 67' -> '+998901234567', '+998 (90) 123-45-67' -> '+998901234567'.
    Bu funksiya admin paneldan kiritilganda ham, Telegram kontakti
    kelganda ham ISHLATILADI — shunda bir odam ikki xil formatda
    yozilib, ikkita alohida akkaunt (dublikat mijoz) bo'lib qolmaydi.
    """
    if not raw_phone:
        return ""

    digits = re.sub(r"\D", "", str(raw_phone))
    if not digits:
        return ""

    if len(digits) == 9:                                   # 901234567
        digits = "998" + digits
    elif len(digits) == 10 and digits.startswith("0"):     # 0901234567
        digits = "998" + digits[1:]
    elif len(digits) in (10, 11) and digits.startswith("8"):  # 8 90 123 45 67
        digits = "998" + digits[-9:]

    return f"+{digits}"


def phone_tail(raw_phone: str, n: int = 9) -> str:
    """Raqamning oxirgi n ta raqami — turli formatda saqlangan eski
    yozuvlarni ham topish uchun (masalan '90 123 45 67' va '+998901234567')."""
    digits = re.sub(r"\D", "", str(raw_phone or ""))
    return digits[-n:] if len(digits) >= n else digits


async def get_telegram_webhook_info() -> dict:
    """Hozirgi webhook sozlamasini Telegram'dan so'raydi — shu orqali
    'allowed_updates ichida callback_query bormi' kabi savollarga
    ishonchli javob olish mumkin (taxmin qilish shart emas)."""
    if not TELEGRAM_API_BASE:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN sozlanmagan"}
    try:
        resp = await http_client.get(f"{TELEGRAM_API_BASE}/getWebhookInfo")
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def set_telegram_webhook(webhook_url: str) -> dict:
    """
    FastAPI server ishga tushganda Telegram Webhook'ni avtomatik sozlashi uchun.
    Ilova doimiy rejimda uzilishlarsiz ishlashini kafolatlaydi.

    DIQQAT: allowed_updates ANIQ ko'rsatilishi SHART. Agar bu parametr
    berilmasa, Telegram AVVALGI sozlamani saqlab qoladi (hatto shu funksiya
    qayta chaqirilsa ham!) — ya'ni eski kod versiyasida webhook faqat
    oddiy xabarlar ("message") uchun sozlangan bo'lsa, tugma bosish
    ("callback_query") hech qachon serverga umuman yetib kelmay qoladi,
    garchi qolgan hamma narsa to'g'ri ishlasa ham. Shuning uchun bu yerda
    kerakli barcha turlarni har safar ANIQ qayta yozamiz.
    """
    if not TELEGRAM_API_BASE:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN sozlanmagan"}
    try:
        resp = await http_client.post(
            f"{TELEGRAM_API_BASE}/setWebhook",
            json={
                "url": webhook_url,
                "drop_pending_updates": False,
                "allowed_updates": ["message", "callback_query"],
            }
        )
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def validate_telegram_init_data(init_data: str, max_age_seconds: int = 86400) -> dict | None:
    """
    Telegram Mini App (WebApp) orqali yuborilgan initData xavfsizlik kalitini 
    HMAC-SHA256 standarti bo'yicha vaqt va imzolarini tekshiradi.
    
    Soxta buyurtmalar va tajovuzlarning oldini oladi.
    """
    if not TELEGRAM_BOT_TOKEN or not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        # Replay Attack (soxta ma'lumotlarni takroran yuborish) xavfidan xavfsizlik:
        auth_date = int(parsed.get("auth_date", 0))
        if (time.time() - auth_date) > max_age_seconds:
            print("[Xavfsizlik Ogohlantirishi] initData vaqti o'tib ketgan (Expired Session).")
            return None

        # Telegram standartiga ko'ra ma'lumotlarni saralash va tekshirish
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            print("[Xavfsizlik Ogohlantirishi] Soxtalashtirilgan initData aniqlandi!")
            return None

        user_json = parsed.get("user")
        if not user_json:
            return None
            
        return _json.loads(user_json)
    except Exception as e:
        print(f"[Avariya Logi] initData tekshirishda xatolik: {e}")
        return None


async def answer_callback_query(callback_query_id: str, text: str | None = None, show_alert: bool = False) -> bool:
    """
    Telegram Inline tugma bosilganda paydo bo'ladigan yuklanish (loading) holatini
    to'xtatish yoki foydalanuvchiga kichik xabar/alert chiqarish uchun funksiya.
    """
    if not TELEGRAM_API_BASE or not callback_query_id:
        return False

    payload = {
        "callback_query_id": callback_query_id,
        "show_alert": show_alert
    }
    if text:
        payload["text"] = text

    try:
        resp = await http_client.post(f"{TELEGRAM_API_BASE}/answerCallbackQuery", json=payload)
        return resp.status_code == 200
    except Exception as e:
        print(f"[Kutilmagan Xatolik] answer_callback_query: {e}")
        return False