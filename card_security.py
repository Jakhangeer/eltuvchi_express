"""
Plastik karta xavfsizligi — shifrlash/deshifrlash, BIN (bank) aniqlash,
amal qilish muddati tekshiruvi va ekranga chiqarish uchun maskalash.

MUHIM (XAVFSIZLIK): 16 xonali karta raqami HECH QACHON ochiq (plain text)
holda bazaga yozilmaydi — faqat `encrypt_card_number()` orqali shifrlangan
ko'rinishda (Fernet — AES128-CBC + HMAC, symmetric). Deshifrlash FAQAT:
  (1) egasining o'ziga (u o'z kartasini ko'rayotganda — baribir maskalab
      ko'rsatiladi, to'liq raqam hech qachon frontendga yubormaymiz), yoki
  (2) OWNER/operator P2P o'tkazma qilishi uchun (pul yechish so'rovini
      ko'rib chiqayotganda) — amalga oshiriladi.

KALIT: CARD_ENCRYPTION_KEY muhit o'zgaruvchisida (.env) saqlanishi SHART.
Agar topilmasa, server ishga tushishda VAQTINCHALIK tasodifiy kalit
generatsiya qilinadi (SESSION_SECRET_KEY bilan bir xil naqsh) — bu holda
server qayta ishga tushganda (deploy/restart) ILGARI SHIFRLANGAN BARCHA
KARTALAR o'qib bo'lmay qoladi (deshifrlash xatosi beradi), shuning uchun
PRODUCTION'DA BU KALIT albatta .env/Render Environment'ga qo'yilishi kerak:
    CARD_ENCRYPTION_KEY=<Fernet.generate_key() natijasi, masalan bir marta
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    buyrug'i bilan generatsiya qiling>
"""

import os
import re
from datetime import date
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


# ==================== SHIFRLASH KALITI ====================
_ENV_KEY = os.getenv("CARD_ENCRYPTION_KEY")
if not _ENV_KEY:
    print(
        "DIQQAT: CARD_ENCRYPTION_KEY .env'da yo'q — vaqtinchalik tasodifiy kalit "
        "ishlatilyapti. PRODUCTION'DA BUNI ALBATTA .env GA QO'YING, aks holda "
        "server qayta ishga tushganda barcha saqlangan kartalar o'qilmay qoladi!"
    )
    _ENV_KEY = Fernet.generate_key().decode()

_fernet = Fernet(_ENV_KEY.encode() if isinstance(_ENV_KEY, str) else _ENV_KEY)


def encrypt_card_number(plain_card_number: str) -> str:
    """16 xonali (bo'shliqsiz) karta raqamini shifrlab, bazaga yozish uchun
    tayyor matn qaytaradi. Kirish bo'shliq/tire bilan kelsa ham tozalanadi."""
    digits = re.sub(r"\D", "", plain_card_number or "")
    return _fernet.encrypt(digits.encode()).decode()


def decrypt_card_number(encrypted_card_number: str) -> Optional[str]:
    """Shifrlangan qiymatni asl 16 xonali raqamga qaytaradi. Kalit mos
    kelmasa yoki ma'lumot buzilgan bo'lsa (masalan kalit almashtirilgan
    bo'lsa) — xato ko'tarmaydi, None qaytaradi (chaqiruvchi "—" ko'rsatadi)."""
    if not encrypted_card_number:
        return None
    try:
        return _fernet.decrypt(encrypted_card_number.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def mask_card_number(plain_or_encrypted: str, *, already_encrypted: bool = False) -> str:
    """Ekranga chiqarish uchun: '8600 12** **** 9012' ko'rinishi — birinchi
    6 va oxirgi 4 raqam ko'rinadi, o'rtasi yashiriladi (PCI-DSS'dagi
    umumiy amaliyot). `already_encrypted=True` bo'lsa, avval deshifrlaydi."""
    digits = decrypt_card_number(plain_or_encrypted) if already_encrypted else re.sub(r"\D", "", plain_or_encrypted or "")
    if not digits or len(digits) < 10:
        return "•••• •••• •••• ••••"

    first6, last4 = digits[:6], digits[-4:]
    middle_len = len(digits) - 10
    masked_middle = "*" * max(middle_len, 6)

    grouped = (first6[:4] + " " + first6[4:6] + masked_middle[:2] + " " + masked_middle[2:6] + " " + last4)
    return grouped.strip()


# ==================== BIN (BANK) ANIQLASH ====================
# DIQQAT: bu — O'zbekiston bozori uchun eng keng tarqalgan prefikslar
# ro'yxati (to'liq rasmiy BIN bazasi emas, evristik). Noma'lum prefiks
# bo'lsa, "Noma'lum bank" qaytariladi — bu xatolik emas, shunchaki
# bankni aniq ayta olmaymiz degani.
_BIN_TABLE = [
    ("8600", "Uzcard", "uzcard"),
    ("5614", "Uzcard", "uzcard"),
    ("9860", "Humo", "humo"),
    ("6262", "Humo", "humo"),
    ("4", "Visa", "visa"),           # Visa har doim "4" bilan boshlanadi
    ("51", "Mastercard", "mastercard"),
    ("52", "Mastercard", "mastercard"),
    ("53", "Mastercard", "mastercard"),
    ("54", "Mastercard", "mastercard"),
    ("55", "Mastercard", "mastercard"),
    ("2200", "Mir", "mir"),
]


def detect_card_bin(card_number: str) -> tuple[str, str]:
    """Karta raqamining birinchi 4-6 raqami bo'yicha (bank_name, card_type)
    qaytaradi. Masalan '8600123456789012' -> ('Uzcard', 'uzcard')."""
    digits = re.sub(r"\D", "", card_number or "")
    # Uzunroq (aniqroq) prefikslarni birinchi tekshiramiz, keyin qisqalarini
    for prefix, bank_name, card_type in sorted(_BIN_TABLE, key=lambda x: -len(x[0])):
        if digits.startswith(prefix):
            return bank_name, card_type
    return "Noma'lum bank", "unknown"


# ==================== VALIDATSIYA ====================
def luhn_valid(card_number: str) -> bool:
    """Luhn algoritmi — karta raqami xato terilmaganini (typo) tekshiradi.
    Bu karta HAQIQIY ekanini KAFOLATLAMAYDI, faqat raqamlar ketma-ketligi
    matematik jihatdan to'g'ri formatda ekanini tasdiqlaydi."""
    digits = re.sub(r"\D", "", card_number or "")
    if len(digits) < 13:
        return False
    total = 0
    reverse_digits = digits[::-1]
    for i, ch in enumerate(reverse_digits):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def validate_card_number(card_number: str) -> str:
    """Karta raqamini tekshiradi (16 xonali, Luhn) va tozalangan (faqat
    raqamlar) ko'rinishini qaytaradi. Xato bo'lsa ValueError ko'taradi —
    chaqiruvchi buni HTTPException(400)ga o'rab beradi."""
    digits = re.sub(r"\D", "", card_number or "")
    if len(digits) != 16:
        raise ValueError("Karta raqami aynan 16 xonadan iborat bo'lishi kerak")
    if not luhn_valid(digits):
        raise ValueError("Karta raqami noto'g'ri kiritilgan (tekshiruv summasi mos kelmadi)")
    return digits


def validate_expiry(month: int, year: int) -> None:
    """MM/YY sanasi o'tib ketmaganini tekshiradi. YY ikki xonali (masalan
    27 -> 2027) yoki to'rt xonali (2027) bo'lishi mumkin. Xato bo'lsa
    ValueError ko'taradi."""
    if not (1 <= month <= 12):
        raise ValueError("Oy 1 dan 12 gacha bo'lishi kerak")

    full_year = year if year > 99 else 2000 + year
    today = date.today()
    # Karta shu oyning OXIRIGACHA amal qiladi — masalan 09/25 bo'lsa,
    # 2025-yil sentyabr oxirigacha ishlaydi, shundan keyin o'tib ketgan.
    last_valid_day = date(full_year, month, 1)
    if full_year < today.year or (full_year == today.year and month < today.month):
        raise ValueError("Kartaning amal qilish muddati o'tib ketgan")
    if full_year > today.year + 15:
        raise ValueError("Amal qilish yili noto'g'ri kiritilgan")
