"""Barqaror zaxira kalitlari.

.env da SESSION_SECRET_KEY / CARD_ENCRYPTION_KEY bo'lmasa, avval har ishga tushganda (va har worker'da)
TASODIFIY kalit yaratilar edi → foydalanuvchilar har harakatda tizimdan chiqib ketar, saqlangan kartalar
o'qilmay qolar edi. Endi zaxira kalit DATABASE_URL (baza paroli) va bot tokenidan HISOBLANADI —
shuning uchun barcha worker'larda va qayta ishga tushganda bir xil bo'ladi.
Baribir .env da alohida kalit qo'yish tavsiya etiladi (baza paroli o'zgarsa, sessiyalar yangilanadi)."""
import base64
import hashlib
import hmac
import os


def _seed() -> bytes:
    return ((os.getenv("DATABASE_URL") or "zoomo") + "|" + (os.getenv("TELEGRAM_BOT_TOKEN") or "")).encode()


def derive_hex(purpose: str) -> str:
    return hmac.new(_seed(), ("zoomo-v1:" + purpose).encode(), hashlib.sha256).hexdigest()


def derive_fernet_key(purpose: str) -> str:
    raw = hmac.new(_seed(), ("zoomo-v1:" + purpose).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).decode()
