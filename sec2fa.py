"""Qo'shimcha himoya: «maxfiy javob» (sevimli ... savollari).

Foydalanuvchi o'zi toifa tanlaydi (sevimli sportchi, qo'shiqchi, taom, ...) va unga xohlagan
javob yozadi (so'z, raqam, belgi, ism-familiya — kamida 5, ko'pi bilan 300 belgi). Javob
bcrypt bilan xeshlanadi. Pul yechish, sozlamalar va operator paneliga kirishda shu javob so'raladi.
5 marta xato qilinsa — 15 daqiqaga bloklanadi."""
import hashlib
import random
import secrets
import re
import time
import unicodedata
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User, UserRole, SecurityAnswer
from telegram_bot import send_telegram_message
from auth import hash_password, verify_password, require_owner

CATEGORIES = {
    "athlete": ("Sevimli sportchingiz", "Sevimli sportchingiz kim?"),
    "singer": ("Sevimli qo'shiqchingiz", "Sevimli qo'shiqchingiz kim?"),
    "food": ("Sevimli taomingiz", "Sevimli taomingiz nima?"),
    "song": ("Sevimli qo'shig'ingiz", "Sevimli qo'shig'ingiz qaysi?"),
    "movie": ("Sevimli kinoyingiz", "Sevimli kinoyingiz qaysi?"),
    "actor": ("Sevimli aktyoringiz", "Sevimli aktyoringiz kim?"),
    "sport": ("Sevimli sportingiz", "Sevimli sport turingiz qaysi?"),
    "brand": ("Sevimli brendingiz", "Sevimli brendingiz qaysi?"),
}
MIN_LEN, MAX_LEN = 5, 300
MAX_TRIES, LOCK_MIN, UNLOCK_SEC = 5, 15, 600
OTP_LEN, OTP_TTL_MIN, OTP_MAX_TRIES, MANAGE_SEC = 8, 5, 5, 600

sec_router = APIRouter(prefix="/security", tags=["Xavfsizlik"])


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").strip()
    return re.sub(r"\s+", " ", s).casefold()


def _digest(s: str) -> str:
    # bcrypt faqat 72 baytni oladi — 300 belgigacha javob uchun avval SHA-256
    return hashlib.sha256(_norm(s).encode("utf-8")).hexdigest()


def hash_answer(s: str) -> str:
    return hash_password(_digest(s))


def check_answer(s: str, h: str) -> bool:
    try:
        return verify_password(_digest(s), h)
    except Exception:
        return False


async def _user(request: Request, db: AsyncSession) -> User:
    uid = request.session.get("user_id")
    user = (await db.execute(select(User).where(User.id == uid))).scalars().first() if uid else None
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Avval tizimga kiring")
    return user


async def _entries(db: AsyncSession, uid: int) -> List[SecurityAnswer]:
    return (await db.execute(select(SecurityAnswer).where(SecurityAnswer.user_id == uid)
                             .order_by(SecurityAnswer.id))).scalars().all()


def _unlocked(request: Request, uid: int) -> bool:
    return request.session.get("sec_uid") == uid and request.session.get("sec_until", 0) > time.time()


def _unlock(request: Request, user: User):
    request.session["sec_uid"] = user.id
    request.session["sec_until"] = time.time() + UNLOCK_SEC
    if user.role == UserRole.ADMIN:
        request.session["sec_admin"] = user.id   # operator paneliga kirish — sessiya davomida


def _can_change(request: Request, uid: int) -> bool:
    """Javoblarni o'zgartirish/o'chirish — Telegramdan kelgan 8 xonali kod kiritilgandan keyingina."""
    return request.session.get("sec_mng_uid") == uid and request.session.get("sec_mng_until", 0) > time.time()


async def secure_action(request: Request, db: AsyncSession = Depends(get_db)):
    """Dependency: pul yechish / sozlamalar kabi nozik amallar oldidan maxfiy javob."""
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="Avval tizimga kiring")
    n = (await db.execute(select(func.count(SecurityAnswer.id)).where(SecurityAnswer.user_id == uid))).scalar() or 0
    if n == 0:
        raise HTTPException(status_code=428, detail={"code": "sec_setup", "message": "Avval maxfiy javobingizni o'rnating"})
    if not _unlocked(request, uid):
        raise HTTPException(status_code=428, detail={"code": "sec_verify", "message": "Maxfiy javobni kiriting"})


def _labels():
    return [{"key": k, "label": v[0]} for k, v in CATEGORIES.items()]


@sec_router.get("/status")
async def sec_status(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _user(request, db)
    es = await _entries(db, user.id)
    return {"has": bool(es), "unlocked": _unlocked(request, user.id), "can_change": _can_change(request, user.id), "has_tg": bool(user.telegram_id), "categories": _labels(),
            "mine": [{"id": e.id, "category": e.category, "label": CATEGORIES.get(e.category, ("?",))[0]} for e in es],
            "min": MIN_LEN, "max": MAX_LEN}


class SetupBody(BaseModel):
    entries: List[dict]


@sec_router.post("/setup")
async def sec_setup(body: SetupBody, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _user(request, db)
    es = await _entries(db, user.id)
    if es and not _unlocked(request, user.id):
        raise HTTPException(status_code=428, detail={"code": "sec_verify", "message": "Maxfiy javobni kiriting"})
    if es and not _can_change(request, user.id):
        raise HTTPException(status_code=428, detail={"code": "sec_otp", "message": "Telegramga yuborilgan kodni kiriting"})
    clean = {}
    for it in body.entries[:len(CATEGORIES)]:
        cat = str(it.get("category", ""))
        ans = str(it.get("answer", "")).strip()
        if cat not in CATEGORIES:
            raise HTTPException(status_code=400, detail="Toifa noto'g'ri")
        if len(ans) < MIN_LEN:
            raise HTTPException(status_code=400, detail=f"«{CATEGORIES[cat][0]}» javobi kamida {MIN_LEN} ta belgi bo'lsin")
        if len(ans) > MAX_LEN:
            raise HTTPException(status_code=400, detail=f"Javob {MAX_LEN} belgidan oshmasin")
        clean[cat] = ans
    if not clean:
        raise HTTPException(status_code=400, detail="Kamida bitta javob kiriting")
    for cat, ans in clean.items():
        await db.execute(delete(SecurityAnswer).where(SecurityAnswer.user_id == user.id, SecurityAnswer.category == cat))
        db.add(SecurityAnswer(user_id=user.id, category=cat, answer_hash=hash_answer(ans)))
    await db.commit()
    request.session.pop("sec_q", None)
    _unlock(request, user)
    return {"ok": True}


@sec_router.get("/challenge")
async def sec_challenge(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _user(request, db)
    es = await _entries(db, user.id)
    if not es:
        raise HTTPException(status_code=428, detail={"code": "sec_setup", "message": "Avval maxfiy javobingizni o'rnating"})
    ids = [e.id for e in es]
    qid = request.session.get("sec_q")
    if qid not in ids:
        qid = random.choice(ids)
        request.session["sec_q"] = qid
    e = next(x for x in es if x.id == qid)
    left = user.sec_locked_until and user.sec_locked_until > datetime.utcnow()
    return {"question": CATEGORIES.get(e.category, ("", "Maxfiy savol"))[1], "locked": bool(left),
            "locked_minutes": int((user.sec_locked_until - datetime.utcnow()).total_seconds() // 60) + 1 if left else 0}


class VerifyBody(BaseModel):
    answer: str


@sec_router.post("/verify")
async def sec_verify(body: VerifyBody, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _user(request, db)
    now = datetime.utcnow()
    if user.sec_locked_until and user.sec_locked_until > now:
        m = int((user.sec_locked_until - now).total_seconds() // 60) + 1
        raise HTTPException(status_code=429, detail=f"Ko'p xato urinish. {m} daqiqadan keyin qayta urinib ko'ring.")
    es = await _entries(db, user.id)
    if not es:
        raise HTTPException(status_code=428, detail={"code": "sec_setup", "message": "Avval maxfiy javobingizni o'rnating"})
    qid = request.session.get("sec_q")
    e = next((x for x in es if x.id == qid), None) or es[0]
    if body.answer and len(body.answer) <= 1000 and check_answer(body.answer, e.answer_hash):
        user.sec_fail_count = 0
        user.sec_locked_until = None
        await db.commit()
        request.session.pop("sec_q", None)
        _unlock(request, user)
        return {"ok": True}
    user.sec_fail_count = (user.sec_fail_count or 0) + 1
    left = MAX_TRIES - user.sec_fail_count
    request.session.pop("sec_q", None)
    if left <= 0:
        user.sec_fail_count = 0
        user.sec_locked_until = now + timedelta(minutes=LOCK_MIN)
        await db.commit()
        raise HTTPException(status_code=429, detail=f"Ko'p xato urinish. {LOCK_MIN} daqiqaga bloklandi.")
    await db.commit()
    raise HTTPException(status_code=403, detail=f"Javob noto'g'ri. Yana {left} ta urinish qoldi.")


@sec_router.post("/delete/{entry_id}")
async def sec_delete(entry_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _user(request, db)
    if not _unlocked(request, user.id):
        raise HTTPException(status_code=428, detail={"code": "sec_verify", "message": "Maxfiy javobni kiriting"})
    if not _can_change(request, user.id):
        raise HTTPException(status_code=428, detail={"code": "sec_otp", "message": "Telegramga yuborilgan kodni kiriting"})
    es = await _entries(db, user.id)
    if len(es) <= 1:
        raise HTTPException(status_code=400, detail="Kamida bitta maxfiy javob qolishi kerak")
    await db.execute(delete(SecurityAnswer).where(SecurityAnswer.id == entry_id, SecurityAnswer.user_id == user.id))
    await db.commit()
    return {"ok": True}


@sec_router.post("/otp/send")
async def sec_otp_send(request: Request, db: AsyncSession = Depends(get_db)):
    """Telegram botdan foydalanuvchining raqamiga bog'langan akkauntga 8 xonali bir martalik kod yuboradi."""
    user = await _user(request, db)
    now = datetime.utcnow()
    if user.sec_otp_expires and user.sec_otp_expires - timedelta(minutes=OTP_TTL_MIN) + timedelta(seconds=30) > now:
        raise HTTPException(status_code=429, detail="Kod yaqinda yuborilgan. 30 soniyadan keyin qayta so'rang.")
    if not user.telegram_id:
        raise HTTPException(status_code=400, detail="Telegram hisobingiz ulanmagan. Botga /start yuborib raqamingizni ulang yoki administratorga murojaat qiling.")
    code = "".join(secrets.choice("0123456789") for _ in range(OTP_LEN))
    ok = await send_telegram_message(
        user.telegram_id,
        f"🔐 <b>ZooMo xavfsizlik kodi</b>\n\n<code>{code}</code>\n\nMaxfiy javoblarni o'zgartirish yoki o'chirish uchun kod. "
        f"{OTP_TTL_MIN} daqiqa amal qiladi. Buni siz so'ramagan bo'lsangiz — hech kimga aytmang va kodni kiritmang.")
    if not ok:
        raise HTTPException(status_code=502, detail="Telegramga xabar yuborib bo'lmadi. Botni bloklamaganingizni tekshiring.")
    user.sec_otp_hash = hash_password(code)
    user.sec_otp_expires = now + timedelta(minutes=OTP_TTL_MIN)
    user.sec_otp_tries = 0
    await db.commit()
    return {"ok": True, "len": OTP_LEN, "minutes": OTP_TTL_MIN}


class OtpBody(BaseModel):
    code: str


@sec_router.post("/otp/verify")
async def sec_otp_verify(body: OtpBody, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _user(request, db)
    if not _unlocked(request, user.id):
        raise HTTPException(status_code=428, detail={"code": "sec_verify", "message": "Maxfiy javobni kiriting"})
    now = datetime.utcnow()
    if not user.sec_otp_hash or not user.sec_otp_expires or user.sec_otp_expires < now:
        raise HTTPException(status_code=400, detail="Kod muddati tugagan yoki yuborilmagan. Yangi kod so'rang.")
    code = re.sub(r"\D", "", body.code or "")
    if len(code) == OTP_LEN and verify_password(code, user.sec_otp_hash):
        user.sec_otp_hash = None
        user.sec_otp_expires = None
        user.sec_otp_tries = 0
        await db.commit()
        request.session["sec_mng_uid"] = user.id
        request.session["sec_mng_until"] = time.time() + MANAGE_SEC
        return {"ok": True}
    user.sec_otp_tries = (user.sec_otp_tries or 0) + 1
    if user.sec_otp_tries >= OTP_MAX_TRIES:
        user.sec_otp_hash = None
        user.sec_otp_expires = None
        user.sec_otp_tries = 0
        await db.commit()
        raise HTTPException(status_code=429, detail="Ko'p xato urinish. Yangi kod so'rang.")
    await db.commit()
    raise HTTPException(status_code=403, detail=f"Kod noto'g'ri. Yana {OTP_MAX_TRIES - user.sec_otp_tries} ta urinish qoldi.")


class ResetBody(BaseModel):
    phone: str


@sec_router.post("/reset")
async def sec_reset(body: ResetBody, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    """Foydalanuvchi maxfiy javobini unutsa — faqat egasi (OWNER) tiklaydi; foydalanuvchi qaytadan o'rnatadi."""
    from main import find_user_by_phone
    u = await find_user_by_phone(db, body.phone)
    if not u:
        raise HTTPException(status_code=404, detail="Foydalanuvchi topilmadi")
    await db.execute(delete(SecurityAnswer).where(SecurityAnswer.user_id == u.id))
    u.sec_fail_count = 0
    u.sec_locked_until = None
    await db.commit()
    return {"ok": True, "name": u.full_name}


_GATE = """<!doctype html><html lang="uz"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Xavfsizlik</title><style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f172a;font-family:system-ui,sans-serif;color:#e2e8f0}
p{max-width:320px;text-align:center;font-size:14px;color:#94a3b8}</style></head><body><p>🔐 Xavfsizlik tekshiruvi...</p>
<script src="/static/js/zm-secure.js?v=2"></script><script>ZMSec.gate(%s);</script></body></html>"""


@sec_router.get("/gate", response_class=HTMLResponse)
async def sec_gate(request: Request, next: str = "/admin", db: AsyncSession = Depends(get_db)):
    uid = request.session.get("user_id")
    if not uid:
        return RedirectResponse(url="/login", status_code=303)
    nxt = next if (next.startswith("/") and not next.startswith("//") and "\\" not in next) else "/admin"
    import json as _j
    return HTMLResponse(_GATE % _j.dumps(nxt))
