from fastapi import Request, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User, UserRole

# bcrypt — parolni bir tomonlama xeshlaydi, hech qachon ochiq matnda saqlanmaydi
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


class RedirectToLogin(Exception):
    """/admin sahifasiga sessiyasiz kirilganda shu exception orqali /login'ga yo'naltiramiz."""
    pass


async def get_current_admin_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """
    Sessiyada saqlangan user_id orqali joriy foydalanuvchini topadi.
    Faqat OWNER yoki ADMIN (operator) rolidagilarga ruxsat beradi.
    Boshqa hollarda /login sahifasiga qaytaradi.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        raise RedirectToLogin()

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()

    if not user or not user.is_active or user.role not in (UserRole.OWNER, UserRole.ADMIN):
        request.session.clear()
        raise RedirectToLogin()

    return user


async def require_owner(user: User = Depends(get_current_admin_user)) -> User:
    """Faqat OWNER kira oladigan route'lar uchun (masalan tizim sozlamalari, do'kon o'chirish)."""
    if user.role != UserRole.OWNER:
        raise HTTPException(status_code=403, detail="Bu amal uchun ruxsatingiz yo'q — faqat egasi (OWNER) bajara oladi")
    return user


async def get_current_partner_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """Hamkor kabineti uchun.

    DIQQAT: endi bu ROL (role == PARTNER) emas, balki PROFIL MAVJUDLIGI
    (PartnerProfile bog'langanmi) orqali tekshiriladi. Sabab: bitta odam
    bir vaqtning o'zida ham mijoz, ham kuryer, ham hamkor bo'lishi mumkin —
    role maydoni endi faqat "ro'yxatdan o'tgandagi asosiy holat"ni
    bildiradi, u yagona imkoniyatni cheklamaydi."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise RedirectToLogin()

    result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.partner_profile))
    )
    user = result.scalars().first()

    if not user or not user.is_active or not user.partner_profile:
        raise RedirectToLogin()
    if user.partner_profile.application_status == "pending":
        raise HTTPException(
            status_code=403,
            detail="Hamkorlik arizangiz hali ko'rib chiqilmoqda — administratsiya tez orada siz bilan bog'lanadi.",
        )

    return user


async def get_current_courier_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """Kuryer kabineti uchun — CourierProfile bog'langan bo'lsa yetarli
    (yuqoridagi izohga qarang — multi-role tufayli role tekshirilmaydi)."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise RedirectToLogin()

    result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.courier_profile))
    )
    user = result.scalars().first()

    if not user or not user.is_active or not user.courier_profile:
        raise RedirectToLogin()
    if user.courier_profile.application_status == "pending":
        raise HTTPException(
            status_code=403,
            detail="Kuryerlik arizangiz hali ko'rib chiqilmoqda — administratsiya tez orada siz bilan bog'lanadi.",
        )

    return user


def is_scoped_to_city(user: User) -> bool:
    """OWNER — cheklovsiz (True qaytarsa filtr YO'Q). Operator — faqat o'z shahri (city_id bo'yicha filtr)."""
    return user.role == UserRole.ADMIN and user.city_id is not None
