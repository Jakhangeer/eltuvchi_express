import json
import os
import html
import uuid
import math
import traceback
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, Request, Depends, Form, HTTPException, status, APIRouter, UploadFile, File
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text, extract, or_, inspect as sa_inspect
from sqlalchemy.schema import CreateColumn
from sqlalchemy.orm import selectinload

from database import engine, Base, get_db, AsyncSessionLocal
from telegram_bot import (
    send_telegram_message,
    send_telegram_photo,
    contact_request_keyboard,
    normalize_phone,
    phone_tail,
    set_telegram_webhook,
    get_telegram_webhook_info,
    validate_telegram_init_data,
    answer_callback_query,
    close_telegram_bot_client,
    get_bot_username,
)
from models import (
    User,
    UserRole,
    Order,
    OrderStatus,
    OrderItem,
    PartnerProfile,
    Product,
    SystemSetting,
    WeatherCondition,
    CourierProfile,
    City,
    Transaction,
    TransactionType,
    WithdrawalRequest,
    WithdrawalStatus,
    Banner,
    PromoCode,
    PromoCodeUsage,
    FavoriteProduct,
    Card,
    OperatorPermission,
)
import card_security
from auth import (
    hash_password,
    verify_password,
    get_current_admin_user,
    require_owner,
    get_current_partner_user,
    get_current_courier_user,
    RedirectToLogin,
)

# Buyurtma statuslarining o'zbekcha nomlari — bazada inglizcha (created, on_the_way...)
# saqlanadi (bu — kod uchun barqaror kalit), lekin admin panelda o'zbekcha ko'rsatiladi.
STATUS_LABELS_UZ = {
    "created": "Yaratildi",
    "accepted_by_partner": "Hamkor qabul qildi",
    "preparing": "Tayyorlanmoqda",
    "looking_for_courier": "Kuryer izlanmoqda",
    "on_the_way": "Yo'lda",
    "delivered": "Yetkazildi",
    "cancelled": "Bekor qilindi",
}

# Kuryer kabinetida yangi buyurtma kelganda tanlash mumkin bo'lgan 3 xil
# signal ovozi — frontend (courier.html) shu kalitlarga qarab Web Audio API
# orqali har xil ohang generatsiya qiladi (fayl saqlash shart emas).
COURIER_SOUND_OPTIONS = {
    "chime1": "🔔 Klassik qo'ng'iroq",
    "chime2": "🎵 Yumshoq melodiya",
    "chime3": "📯 Signal (kar-kar)",
}

# Joriy holatdan "tabiiy keyingi qadam"ga tez o'tish tugmasi uchun xarita.
NEXT_STATUS_MAP = {
    "created": ("accepted_by_partner", "✅ Hamkor qabul qildi"),
    "accepted_by_partner": ("preparing", "🍳 Tayyorlanmoqda"),
    "preparing": ("looking_for_courier", "🔍 Kuryer izlash"),
    "looking_for_courier": ("on_the_way", "🛵 Yo'lda"),
    "on_the_way": ("delivered", "✅ Yetkazildi"),
}


async def seed_default_data():
    """
    Ilova birinchi marta ishga tushganda:
    1. Shaharlarni (Uchquduq, Zarafshon) oldindan qo'shib qo'yadi (agar hali yo'q bo'lsa)
    2. OWNER akkaunt mavjudligini tekshiradi — bo'lmasa, .env orqali (OWNER_PHONE,
       OWNER_PASSWORD) yaratadi. Bu — birinchi kirish uchun "kalit" bo'ladi.
    """
    async with AsyncSessionLocal() as session:
        cities_result = await session.execute(select(City))
        if not cities_result.scalars().first():
            session.add_all([City(name="Uchquduq"), City(name="Zarafshon")])
            await session.commit()
            print("Shaharlar (Uchquduq, Zarafshon) qo'shildi.")

        owner_result = await session.execute(select(User).where(User.role == UserRole.OWNER))
        if not owner_result.scalars().first():
            owner_phone = os.getenv("OWNER_PHONE")
            owner_password = os.getenv("OWNER_PASSWORD")
            if owner_phone and owner_password:
                owner = User(
                    full_name=os.getenv("OWNER_NAME", "Egasi"),
                    phone_number=owner_phone,
                    role=UserRole.OWNER,
                    password_hash=hash_password(owner_password),
                    is_active=True,
                )
                session.add(owner)
                await session.commit()
                print(f"OWNER akkaunt yaratildi: {owner_phone}")
            else:
                print(
                    "DIQQAT: hali OWNER akkaunt yo'q. .env fayliga OWNER_PHONE va "
                    "OWNER_PASSWORD qo'shib, serverni qayta ishga tushiring."
                )


# ==================== STARTUP / SHUTDOWN ====================
def _relax_legacy_not_null_columns(sync_conn):
    """Modelda ENDI yo'q, lekin bazada eski "NOT NULL" ustun bo'lib qolgan
    bo'lsa (masalan banners.image_url), yangi qator qo'shishda Postgres
    xato beradi — bu "Internal Server Error"ning keng tarqalgan sababi
    (masalan rasm bilan banner qo'shganda). Bunday ustunlarni NULL
    qabul qiladigan qilamiz. Hech narsa o'chirilmaydi."""
    inspector = sa_inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        if table.name not in inspector.get_table_names():
            continue
        model_columns = {c.name for c in table.columns}
        for col in inspector.get_columns(table.name):
            if col["name"] in model_columns or col.get("nullable", True):
                continue
            if col.get("default") is not None or col.get("autoincrement"):
                continue
            try:
                sync_conn.execute(text(f'ALTER TABLE "{table.name}" ALTER COLUMN "{col["name"]}" DROP NOT NULL'))
                print(f"[AUTO-MIGRATE] NOT NULL olib tashlandi (eski ustun): {table.name}.{col['name']}")
            except Exception as e:
                print(f"[AUTO-MIGRATE OGOHLANTIRISH] {table.name}.{col['name']}: {e}")


async def auto_sync_missing_columns(conn):
    """
    Modeldagi (models.py) har bir ustunni bazadagi haqiqiy holat bilan
    solishtiradi, va YETISHMAYOTGANLARINI O'ZI ALTER TABLE bilan qo'shadi.

    NEGA BU KERAK: Alembic — to'g'ri, professional yechim, lekin Render
    kabi joyda uni sozlash (Start Command, versiyalar tarixi) tez-tez
    chalkashlikka olib kelmoqda. Bu funksiya esa — "hech qachon
    unutilmaydigan, qo'lda hech narsa qilish shart bo'lmagan" ehtiyot
    chorasi: HAR safar server ishga tushganda o'zi tekshiradi va
    tuzatadi, Alembic ishlatilsa ham, ishlatilmasa ham xavfsiz.

    Ishlash tartibi:
    1. Bazadagi haqiqiy jadval/ustunlar ro'yxatini o'qiydi (inspector).
    2. models.py'dagi har bir jadval/ustunni shu bilan solishtiradi.
    3. Yo'q ustunni topsa — ALTER TABLE ... ADD COLUMN ... buyrug'ini
       AVTOMATIK generatsiya qilib, ishga tushiradi.
    4. Bitta ustunda xatolik bo'lsa ham, qolganlariga to'sqinlik qilmaydi
       (har biri alohida try/except ichida).
    """
    def _sync_columns(sync_conn):
        inspector = sa_inspect(sync_conn)
        existing_tables = set(inspector.get_table_names())

        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                # Bu jadval umuman yo'q — create_all allaqachon yaratadi, o'tkazib yuboramiz
                continue

            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}

            for column in table.columns:
                if column.name in existing_columns:
                    continue

                try:
                    # Ustunning SQL ta'rifini (nomi, turi) avtomatik generatsiya qilamiz —
                    # bu yerda hech qanday qo'lda yozilgan ustun nomlari yo'q, shuning
                    # uchun kelajakda qo'shiladigan HAR QANDAY yangi ustun ham
                    # avtomatik qo'shiladi.
                    col_ddl = str(CreateColumn(column).compile(dialect=sync_conn.dialect))
                    # Mavjud (bo'sh bo'lmagan) jadvalga ustun qo'shganda, agar u
                    # "NOT NULL" bo'lsa-yu standart qiymati bo'lmasa, Postgres xato
                    # beradi — shuning uchun xavfsizlik uchun har doim NULLABLE
                    # sifatida qo'shamiz (dastur darajasidagi default qiymatlar
                    # baribir keyingi yozuvlarda ishlaydi).
                    col_ddl_nullable = col_ddl.replace(" NOT NULL", "")
                    ddl_statement = f'ALTER TABLE "{table.name}" ADD COLUMN {col_ddl_nullable}'
                    sync_conn.execute(text(ddl_statement))
                    # DIQQAT: bu yerda sync_conn.commit() CHAQIRILMAYDI — chunki
                    # bu funksiya tashqi "async with engine.begin() as conn:"
                    # tranzaksiyasi ICHIDA ishlaydi, u o'zi avtomatik commit
                    # qiladi. Qo'lda commit() chaqirish tashqi tranzaksiyani
                    # muddatidan oldin "yopib" qo'yib, undan keyingi barcha
                    # amallarni "closed transaction" xatosi bilan buzardi —
                    # aynan shu xato oldingi versiyada yuz bergan edi.
                    print(f"[AUTO-MIGRATE] Qo'shildi: {table.name}.{column.name}")
                except Exception as e:
                    print(f"[AUTO-MIGRATE OGOHLANTIRISH] {table.name}.{column.name} qo'shilmadi: {e}")

    await conn.run_sync(_sync_columns)
    await conn.run_sync(_relax_legacy_not_null_columns)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        # 1-qadam: umuman yo'q jadvallarni yaratish (yangi model qo'shilganda)
        await conn.run_sync(Base.metadata.create_all)
        # 2-qadam: mavjud jadvallardagi yetishmayotgan ustunlarni avtomatik qo'shish
        await auto_sync_missing_columns(conn)

    print("PostgreSQL jadvallari tayyor va sinxronlashtirildi.")
    await seed_default_data()

    # ---- TELEGRAM WEBHOOK'NI AVTOMATIK ULASH ----
    # Avval buni har safar qo'lda (/telegram/set-webhook) qilish kerak edi —
    # deploy'dan keyin (yoki manzil o'zgarganda) unutilsa, "/start bosilganda
    # javob kelmaydi". Endi server ishga tushganda o'zi ulaydi.
    # Render'da RENDER_EXTERNAL_URL avtomatik beriladi; boshqa hostingda
    # PUBLIC_BASE_URL=https://sizning-domen.uz deb qo'shing.
    try:
        public_url = get_public_base_url(None)
        if os.getenv("TELEGRAM_BOT_TOKEN") and public_url.startswith("https://"):
            wh = await set_telegram_webhook(public_url + "/telegram/webhook")
            print(f"[TELEGRAM WEBHOOK] {public_url}/telegram/webhook -> {wh}")
        elif os.getenv("TELEGRAM_BOT_TOKEN"):
            print("[TELEGRAM WEBHOOK] O'TKAZIB YUBORILDI: PUBLIC_BASE_URL (https) topilmadi. "
                  "Render'da RENDER_EXTERNAL_URL avtomatik bo'ladi; boshqa joyda PUBLIC_BASE_URL kiriting "
                  "yoki brauzerda /telegram/set-webhook ni oching.")
        else:
            print("[TELEGRAM WEBHOOK] TELEGRAM_BOT_TOKEN topilmadi — bot ishlamaydi.")
    except Exception as e:
        print(f"[TELEGRAM WEBHOOK XATOSI] {e!r}")
    yield
    # Server to'xtatilganda Telegram uchun ochilgan HTTP ulanishlarni yopamiz
    await close_telegram_bot_client()


app = FastAPI(title="Eltuvchi Express API", lifespan=lifespan)

# Sessiya (login holatini "eslab qolish") uchun. SESSION_SECRET_KEY albatta
# .env faylida bo'lishi kerak — aks holda server qayta ishga tushganda barcha
# foydalanuvchilar tizimdan chiqarilib yuboriladi, va agar kimdir bu kalitni
# bilib olsa, sessiyani soxtalashtirishi mumkin bo'ladi.
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY")
if not SESSION_SECRET_KEY:
    print("DIQQAT: SESSION_SECRET_KEY .env'da yo'q — vaqtinchalik tasodifiy kalit ishlatilyapti.")
    import secrets as _secrets
    SESSION_SECRET_KEY = _secrets.token_hex(32)

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY, same_site="lax")


# ==================== CSRF HIMOYASI ====================
# DIQQAT: bu yerda "token har bir formaga qo'shilsin" usuli emas,
# "Origin/Referer tekshiruvi" usuli tanlangan — chunki bizda 50+ dan
# ortiq HTML forma bor (admin/hamkor/kuryer panellarida), va ularning
# HAMMASINI birma-bir o'zgartirish xato qilish ehtimolini oshiradi.
#
# Bu usul esa: har bir "xavfli" so'rov (POST/PUT/PATCH/DELETE) qayerdan
# kelganini (brauzer avtomatik yuboradigan Origin/Referer sarlavhasi
# orqali) tekshiradi — agar so'rov BIZNING saytimizdan kelmagan bo'lsa
# (masalan, boshqa saytdagi yashirin forma orqali soxta so'rov yuborishga
# urinilsa), rad etiladi. Bu — zamonaviy brauzerlarda ishonchli va keng
# qo'llaniladigan himoya, va HECH QANDAY formaga o'zgartirish shart emas.
CSRF_EXEMPT_PREFIXES = (
    "/telegram/webhook",   # Telegram serveridan keladi — Origin sarlavhasi yo'q, lekin
                            # bu yo'l allaqachon boshqa yo'l bilan himoyalangan (faqat
                            # Telegram token orqali topiladigan maxfiy URL)
    "/api/shop/",           # Mini App so'rovlari — Telegram initData (HMAC imzo) orqali
                            # ALLAQACHON qattiq tasdiqlanadi, CSRF bu yerda ortiqcha
)


@app.middleware("http")
async def csrf_origin_check_middleware(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        path = request.url.path
        if not any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES):
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin:
                from urllib.parse import urlparse
                origin_host = urlparse(origin).hostname
                request_host = request.url.hostname
                if origin_host != request_host:
                    print(f"[CSRF RAD ETILDI] path={path} origin_host={origin_host} kutilgan={request_host}")
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "So'rov manbasi tasdiqlanmadi (CSRF himoyasi)."},
                    )
            # DIQQAT: Origin/Referer sarlavhasi UMUMAN yo'q bo'lsa — bu yerda
            # ataylab RAD ETMAYMIZ. Sabab: haqiqiy CSRF hujumida brauzer har
            # doim Origin sarlavhasini yuboradi (buni tajovuzkor yashira
            # olmaydi), shuning uchun yuqoridagi "mos kelmasa rad etish"
            # tekshiruvi haqiqiy hujumni allaqachon ushlaydi. Sarlavha
            # umuman yo'qligi ko'proq: eski brauzer, maxfiylik sozlamasi,
            # yoki API test vositasi (masalan curl/Postman) bo'lishi mumkin
            # — buni ham rad etish, real foydalanuvchilarni bexosdan
            # tizimdan chiqarib qo'yish xavfini oshiradi.
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Kutilmagan xatolik yuz berganda "Internal Server Error" o'rniga
    NIMA bo'lganini ko'rsatadi (va server logiga to'liq izini yozadi) —
    shunda "banner yuklansa xato beradi" kabi muammolarni topish oson bo'ladi."""
    print(f"[500 XATO] {request.method} {request.url.path}: {exc!r}")
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"Server xatosi: {type(exc).__name__}: {str(exc)[:300]}",
            "path": request.url.path,
        },
    )


@app.exception_handler(RedirectToLogin)
async def redirect_to_login_handler(request: Request, exc: RedirectToLogin):
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


# Statik fayllar va shablonlar
# DIQQAT: agar "static" papkasi serverda mavjud bo'lmasa (masalan, Git bo'sh
# papkalarni saqlamagani uchun), StaticFiles import paytida xatolik berib,
# butun dastur ishga tushmay qolardi. Shuning uchun avval papka borligini
# ta'minlaymiz.
os.makedirs("static/images", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# DIQQAT: bazadagi barcha vaqtlar UTC'da saqlanadi (datetime.utcnow()) —
# bu to'g'ri amaliyot (server qayerda joylashgani muhim emas, hammasi bir
# xil hisoblanadi). Lekin FOYDALANUVCHIGA ko'rsatishda buni O'zbekiston
# vaqtiga (UTC+5, yoz vaqtiga o'tish yo'q) o'girish kerak — aks holda
# barcha vaqtlar 5 soat "orqada" ko'rinardi. Shu uchun maxsus Jinja
# filtri qo'shamiz: {{ x.created_at|uzb_time('%d.%m.%Y %H:%M') }}
UZB_TZ_OFFSET = timedelta(hours=5)


def uzb_time_filter(value, fmt="%d.%m.%Y %H:%M"):
    if value is None:
        return "—"
    return (value + UZB_TZ_OFFSET).strftime(fmt)


templates.env.filters["uzb_time"] = uzb_time_filter

# Routerlar. DIQQAT: `dependencies=[Depends(get_current_admin_user)]` — bu router
# ostidagi BARCHA route'lar uchun "login qilingan bo'lishi shart" tekshiruvini
# avtomatik qo'shadi. `require_owner` esa qo'shimcha — faqat OWNER'ga.
# ==================== OPERATOR KO'RISH HUQUQLARI (admin.html bo'limlari) ====================
# OWNER /admin/settings/operator-permissions sahifasidan har bir bo'limni
# operatorga ko'rsatish/yashirishni boshqaradi. Bu yerdagi qiymatlar — hali
# bazada qator yo'q bo'lsa ishlatiladigan STANDART (xavfsiz) holat: moliyaviy
# va tizim darajasidagi bo'limlar standart holda YOPIQ, operativ ishlash
# uchun kerakli bo'limlar OCHIQ.
SECTION_LABELS_UZ = {
    "buyurtmalar": "📦 Buyurtmalar boshqaruvi",
    "kuryerlar": "🛵 Kuryerlar ro'yxati",
    "mijozlar": "👥 Mijozlar ro'yxati",
    "dokonlar": "🏪 Do'konlar (hamkorlar) boshqaruvi",
    "moliya": "💰 Moliya (pul yechish so'rovlari, tranzaksiyalar)",
    "operatorlar": "🧑‍💼 Operatorlar ro'yxati",
    "banner": "🖼️ Banner boshqaruvi",
    "shartlar": "📄 Kuryer/Hamkor shartlari matni",
    "referal": "🎁 Referal dasturi sozlamalari",
    "bonuscashback": "💸 Bonus/Cashback sozlamalari",
    "tugilgankun": "🎂 Tug'ilgan kunlar ro'yxati",
}
DEFAULT_OPERATOR_SECTION_VISIBILITY = {
    # Kundalik operativ ish uchun shart bo'lgan bo'limlar — standart OCHIQ
    # (operator buyurtma/kuryer/mijoz/do'kon bilan ishlay olishi kerak,
    # aks holda ishi falaj bo'lib qoladi). OWNER istasa keyin yopadi.
    "buyurtmalar": True,
    "kuryerlar": True,
    "mijozlar": True,
    "dokonlar": True,
    # Moliyaviy va tizim darajasidagi bo'limlar — standart YOPIQ (nozik,
    # OWNER ataylab ochishi kerak).
    "moliya": False,
    "operatorlar": False,
    "banner": False,
    "shartlar": False,
    "referal": True,
    "bonuscashback": True,
    "tugilgankun": True,
}
# "xavfzone" (tizimni butunlay tozalash) — BU YERDA ATAYLAB YO'Q: shu bo'lim
# har doim, hech qanday sozlamadan qat'i nazar, FAQAT OWNER'ga ko'rinadi va
# ishlaydi — bu amal qaytarib bo'lmaydigan bo'lgani uchun delegatsiya
# qilinmaydi (qarang reset_system).


async def get_operator_permissions(db: AsyncSession) -> dict:
    """Barcha bo'limlar uchun {section_key: True/False} lug'atini qaytaradi
    — bazadagi qiymat bilan standart qiymatni birlashtirib. Admin panelining
    bitta GET so'rovida bir marta chaqiriladi (arzon — kamida qatordan kam)."""
    result = await db.execute(select(OperatorPermission))
    saved = {row.section_key: row.enabled for row in result.scalars().all()}
    merged = dict(DEFAULT_OPERATOR_SECTION_VISIBILITY)
    merged.update(saved)
    return merged


async def operator_can_see(db: AsyncSession, current_user: User, section_key: str) -> bool:
    """OWNER — har doim True. Operator (ADMIN) — faqat shu bo'lim OWNER
    tomonidan yoqilgan bo'lsa True."""
    if current_user.role == UserRole.OWNER:
        return True
    perms = await get_operator_permissions(db)
    return perms.get(section_key, DEFAULT_OPERATOR_SECTION_VISIBILITY.get(section_key, False))


def require_section(section_key: str):
    """FastAPI dependency generatori: `/admin/...` routerlaridagi bo'limni
    OWNER doim ko'radi; operator esa FAQAT shu bo'lim OWNER tomonidan
    yoqilgan bo'lsa kira oladi — aks holda 403. Bu FRONTENDDAGI (admin.html)
    yashirishning backend tarafidagi "haqiqiy" himoyasi: operator hatto
    to'g'ridan-to'g'ri URL chaqirsa ham, ruxsatsiz bo'limga kira olmaydi."""

    async def _dep(
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_admin_user),
    ) -> User:
        if not await operator_can_see(db, current_user, section_key):
            raise HTTPException(
                status_code=403,
                detail="Bu bo'limni ko'rish huquqingiz yo'q — administrator bilan bog'laning.",
            )
        return current_user

    return _dep




admin_router = APIRouter(tags=["Admin Dashboard"])
settings_router = APIRouter(
    prefix="/admin/settings", tags=["Tizim Sozlamalari"], dependencies=[Depends(require_owner)]
)
orders_router = APIRouter(
    prefix="/admin/orders", tags=["Buyurtmalar Boshqaruvi"], dependencies=[Depends(require_section("buyurtmalar"))]
)
partners_router = APIRouter(
    prefix="/admin/partners", tags=["Do'konlar Boshqaruvi"], dependencies=[Depends(require_section("dokonlar"))]
)
products_router = APIRouter(
    prefix="/admin/products", tags=["Mahsulotlar (Menu) Boshqaruvi"], dependencies=[Depends(require_section("dokonlar"))]
)
couriers_router = APIRouter(
    prefix="/admin/couriers", tags=["Kuryerlar Boshqaruvi"], dependencies=[Depends(require_section("kuryerlar"))]
)
clients_router = APIRouter(
    prefix="/admin/clients", tags=["Mijozlar Boshqaruvi"], dependencies=[Depends(require_section("mijozlar"))]
)
operators_router = APIRouter(
    prefix="/admin/operators", tags=["Operatorlar Boshqaruvi"], dependencies=[Depends(require_owner)]
)
cities_router = APIRouter(
    prefix="/admin/cities", tags=["Shaharlar Boshqaruvi"], dependencies=[Depends(require_owner)]
)
finance_router = APIRouter(
    prefix="/admin/finance", tags=["Moliyaviy Boshqaruv"], dependencies=[Depends(get_current_admin_user)]
)
# DIQQAT: router darajasida endi faqat "login qilingan admin/operator"
# tekshiriladi (require_owner EMAS) — chunki operatorga "moliya" bo'limi
# OWNER tomonidan yoqilgan bo'lishi mumkin (qarang require_section va
# OperatorPermission). Har bir alohida endpoint o'z darajasidagi aniqroq
# tekshiruvni o'zi belgilaydi: moliyaviy so'rovlarni ko'rib chiqish —
# require_section("moliya") (operator ham kirishi MUMKIN bo'lgan amal);
# qo'lda balans tuzatish va ruxsatlarni boshqarish — require_owner (hech
# qachon delegatsiya qilinmaydigan amallar).


# ==================== 0. LOGIN / LOGOUT ====================
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/admin"):
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None, "next_url": next})


class TelegramLoginBody(BaseModel):
    init_data: str
    next: Optional[str] = None


@app.post("/telegram-login")
async def telegram_login(body: TelegramLoginBody, request: Request, db: AsyncSession = Depends(get_db)):
    """Mini App orqali (Telegram WebView ichida) ochilganda, PIN qayta
    so'ralmasdan avtomatik kirish uchun. login.html sahifasi ochilganda,
    agar Telegram.WebApp mavjud bo'lsa, shu manzilga initData yuboradi —
    muvaffaqiyatli bo'lsa, foydalanuvchi PIN kiritishning hojati
    bo'lmasdan to'g'ridan-to'g'ri o'z kabinetiga kiradi.

    XAVFSIZLIK: bu yerda parol umuman tekshirilmaydi — buning o'rniga
    Telegram'ning o'zi imzolagan initData tekshiriladi (HMAC), bu esa
    "faqat Telegram orqali, faqat shu odamning o'zi" ekanini kafolatlaydi
    xuddi PIN kabi ishonchli, chunki uni soxtalashtirib bo'lmaydi."""
    tg_user = validate_telegram_init_data(body.init_data)
    if not tg_user:
        return JSONResponse(status_code=403, content={"ok": False, "detail": "Telegram tasdiqlanmadi"})

    telegram_id = str(tg_user["id"])
    result = await db.execute(
        select(User)
        .where(User.telegram_id == telegram_id)
        .options(selectinload(User.courier_profile), selectinload(User.partner_profile))
    )
    user = result.scalars().first()

    if not user or not user.is_active:
        return JSONResponse(status_code=404, content={"ok": False, "detail": "Foydalanuvchi topilmadi"})

    can_admin = user.role in (UserRole.OWNER, UserRole.ADMIN)
    can_partner = user.partner_profile is not None
    can_courier = user.courier_profile is not None

    if not (can_admin or can_partner or can_courier):
        return JSONResponse(status_code=403, content={"ok": False, "detail": "Sizga tegishli panel topilmadi"})

    request.session["user_id"] = user.id

    # Agar "next" so'ralgan bo'lsa va foydalanuvchi haqiqatan shu panelga
    # kira olsa — o'shani ishlatamiz; aks holda mos panelni o'zimiz tanlaymiz.
    redirect_to = "/admin"
    if body.next == "/partner" and can_partner:
        redirect_to = "/partner"
    elif body.next == "/courier" and can_courier:
        redirect_to = "/courier"
    elif can_admin:
        redirect_to = "/admin"
    elif can_partner:
        redirect_to = "/partner"
    elif can_courier:
        redirect_to = "/courier"

    return {"ok": True, "redirect": redirect_to}


@app.post("/login")
async def login_submit(
    request: Request,
    phone_number: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    # DIQQAT: endi rol bo'yicha emas — telefon raqami va parol/PIN mos
    # kelsa, kirish beriladi. Qaysi panel(lar)ga kira olishi keyin
    # PROFIL MAVJUDLIGI (courier_profile / partner_profile) orqali
    # aniqlanadi — bitta odam bir nechtasiga ega bo'lishi mumkin.
    user = await find_user_by_phone(
        db, phone_number,
        options=(selectinload(User.courier_profile), selectinload(User.partner_profile)),
    )

    if not user or not user.is_active or not user.password_hash or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Telefon raqami yoki parol/PIN noto'g'ri"},
        )

    request.session["user_id"] = user.id

    # Qaysi panellarga kira olishini aniqlaymiz
    can_admin = user.role in (UserRole.OWNER, UserRole.ADMIN)
    can_partner = user.partner_profile is not None
    can_courier = user.courier_profile is not None
    available = [can_admin, can_partner, can_courier].count(True)

    if available > 1:
        # Bir nechta panelga kira oladi — tanlov sahifasini ko'rsatamiz
        return RedirectResponse(url="/choose-panel", status_code=status.HTTP_303_SEE_OTHER)
    if can_admin:
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    if can_partner:
        return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)
    if can_courier:
        return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)

    # Hech qanday panelga ruxsati yo'q (masalan oddiy CLIENT parol bilan
    # kirishga urinsa — clientlar odatda parolga ega bo'lmaydi, lekin
    # ehtiyot chorasi sifamida)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "Sizga tegishli boshqaruv paneli topilmadi"},
    )


@app.get("/choose-panel", response_class=HTMLResponse)
async def choose_panel(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    result = await db.execute(
        select(User)
        .where(User.id == user_id)
        .options(selectinload(User.courier_profile), selectinload(User.partner_profile))
    )
    user = result.scalars().first()
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    panels = []
    if user.role in (UserRole.OWNER, UserRole.ADMIN):
        panels.append({"url": "/admin", "label": "🛠 Boshqaruv Paneli"})
    if user.partner_profile:
        panels.append({"url": "/partner", "label": "🏪 Hamkor Kabineti"})
    if user.courier_profile:
        panels.append({"url": "/courier", "label": "🛵 Kuryer Kabineti"})

    return templates.TemplateResponse(request=request, name="choose_panel.html", context={"panels": panels})


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


# ==================== 1. ADMIN DASHBOARD ====================
@admin_router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    city_id: Optional[int] = None,
    current_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    # DIQQAT: date.today() server vaqti (UTC) bo'yicha hisoblaydi — bu esa
    # O'zbekiston vaqtidan 5 soat orqada. Ya'ni Toshkentda tun yarmidan
    # o'tib, allaqachon "ertangi kun" boshlangan bo'lsa ham, server hali
    # "kecha" deb hisoblab, "Bugungi buyurtmalar", "Bugungi tug'ilgan
    # kunlar" kabi hisoblarni NOTO'G'RI ko'rsatardi. Shuning uchun "bugun"
    # tushunchasini O'ZBEKISTON vaqtiga qarab aniqlaymiz.
    today = (datetime.utcnow() + UZB_TZ_OFFSET).date()
    is_owner = current_user.role == UserRole.OWNER

    # OWNER shaharni tepadagi almashtirgichdan tanlaydi (None = "Barchasi").
    # Operator uchun bu — har doim o'ziga biriktirilgan shahar, o'zgartira olmaydi.
    active_city_id = city_id if is_owner else current_user.city_id

    cities_query = await db.execute(select(City).order_by(City.name))
    all_cities = cities_query.scalars().all()

    # ---- DO'KONLAR (shahar bo'yicha filtrlanadi) ----
    partners_stmt = select(PartnerProfile).options(selectinload(PartnerProfile.city))
    if active_city_id is not None:
        partners_stmt = partners_stmt.where(PartnerProfile.city_id == active_city_id)
    partners_query = await db.execute(partners_stmt)
    partners = partners_query.scalars().all()
    active_partners = sum(1 for p in partners if p.is_open)

    # ---- MAHSULOTLAR (do'kon orqali shaharga bog'lanadi) ----
    products_stmt = select(Product).options(selectinload(Product.partner))
    if active_city_id is not None:
        products_stmt = products_stmt.join(PartnerProfile, Product.partner_id == PartnerProfile.id).where(
            PartnerProfile.city_id == active_city_id
        )
    products_query = await db.execute(products_stmt)
    products = products_query.scalars().all()

    # ---- KURYERLAR (shahar bo'yicha filtrlanadi) ----
    # DIQQAT: ko'p-rolli tizimda User.role har doim "client" bo'lib qoladi
    # (qarang auth.py) — kimning kuryer ekanini FAQAT CourierProfile
    # mavjudligi orqali (join) aniqlaymiz, User.role orqali EMAS.
    couriers_stmt = (
        select(User)
        .join(CourierProfile, CourierProfile.user_id == User.id)
        .where(User.is_active == True)
    )
    if active_city_id is not None:
        couriers_stmt = couriers_stmt.where(User.city_id == active_city_id)
    couriers_query = await db.execute(couriers_stmt)
    couriers = couriers_query.scalars().all()
    active_couriers = len(couriers)

    all_couriers_stmt = (
        select(User)
        .join(CourierProfile, CourierProfile.user_id == User.id)
        .options(selectinload(User.courier_profile), selectinload(User.city))
        .order_by(User.created_at.desc())
    )
    if active_city_id is not None:
        all_couriers_stmt = all_couriers_stmt.where(User.city_id == active_city_id)
    all_couriers_query = await db.execute(all_couriers_stmt)
    all_couriers = all_couriers_query.scalars().all()

    # ---- MIJOZLAR ----
    # Mijozning shahri User.city_id orqali aniqlanadi — bu maydon avval
    # faqat operatorlar uchun ishlatilgan edi, endi mijozlar uchun ham
    # ishlatamiz (buyurtma berayotganda, qaysi shahar hamkoridan xarid
    # qilsa, o'sha shaharga "yozib qo'yiladi" — pastda create_order'da).
    clients_stmt = (
        select(
            User,
            func.count(Order.id).label("order_count"),
            func.coalesce(func.sum(Order.total_price), 0).label("total_spent"),
        )
        .outerjoin(Order, Order.client_id == User.id)
        .where(
            User.role == UserRole.CLIENT,
            # Ko'p-rolli tizimda kuryer/hamkorlarning ham User.role'i
            # "client" bo'lib qoladi — shu ro'yxatga ular ARALASHIB
            # KETMASLIGI uchun CourierProfile/PartnerProfile'ga ega
            # foydalanuvchilarni bu yerdan chetlatamiz (ular allaqachon
            # o'z alohida — Kuryerlar / Do'konlar — bo'limlarida ko'rinadi).
            # DIQQAT: subquery'da NULL bo'lsa, "NOT IN" BUTUN natijani bo'shatib
            # yuboradi (SQL'ning mashhur tuzog'i). user_id'si bo'sh do'konlar
            # (login berilmagan) bor — shuning uchun NULL'larni chiqarib tashlaymiz.
            ~User.id.in_(select(CourierProfile.user_id).where(CourierProfile.user_id.isnot(None))),
            ~User.id.in_(select(PartnerProfile.user_id).where(PartnerProfile.user_id.isnot(None))),
        )
    )
    if active_city_id is not None:
        clients_stmt = clients_stmt.where(User.city_id == active_city_id)
    clients_stmt = clients_stmt.group_by(User.id).order_by(User.created_at.desc())
    clients = (await db.execute(clients_stmt)).all()

    # ---- OPERATORLAR (faqat OWNER ko'radi, lekin har doim so'raymiz — arzon so'rov) ----
    operators_query = await db.execute(
        select(User)
        .where(User.role == UserRole.ADMIN)
        .options(selectinload(User.city))
        .order_by(User.created_at.desc())
    )
    operators = operators_query.scalars().all()

    # ---- BUGUNGI STATISTIKA ----
    orders_count_stmt = select(func.count(Order.id)).where(func.date(Order.created_at) == today)
    revenue_stmt = select(func.sum(Order.delivery_fee)).where(
        func.date(Order.created_at) == today, Order.status == OrderStatus.DELIVERED
    )
    if active_city_id is not None:
        orders_count_stmt = orders_count_stmt.join(
            PartnerProfile, Order.partner_id == PartnerProfile.id
        ).where(PartnerProfile.city_id == active_city_id)
        revenue_stmt = revenue_stmt.join(
            PartnerProfile, Order.partner_id == PartnerProfile.id
        ).where(PartnerProfile.city_id == active_city_id)

    today_orders_count = (await db.execute(orders_count_stmt)).scalar() or 0
    today_revenue_val = (await db.execute(revenue_stmt)).scalar() or 0.0
    today_revenue = f"{today_revenue_val:,.0f}".replace(",", " ")

    # ---- BUYURTMALAR (tarkibi bilan, shahar bo'yicha filtrlangan) ----
    orders_stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.client), selectinload(Order.courier))
        .order_by(Order.created_at.desc())
    )
    if active_city_id is not None:
        orders_stmt = orders_stmt.join(PartnerProfile, Order.partner_id == PartnerProfile.id).where(
            PartnerProfile.city_id == active_city_id
        )
    orders_query = await db.execute(orders_stmt)
    orders = orders_query.scalars().all()

    # ---- TIZIM SOZLAMASI ----
    setting_query = await db.execute(select(SystemSetting))
    system_setting = setting_query.scalars().first()

    if system_setting:
        suggested_delivery_fee = system_setting.base_delivery_fee * system_setting.weather_multiplier
    else:
        suggested_delivery_fee = 10000.0

    # "Qo'lda buyurtma yaratish" formasi uchun — mahsulotlarni JS tomonida
    # do'konga qarab guruhlab ko'rsatish maqsadida JSON qilib tayyorlaymiz
    products_json = json.dumps(
        [
            {
                "id": p.id,
                "name": p.name,
                "price": p.price,
                "partner_id": p.partner_id,
                "partner_name": p.partner.brand_name if p.partner else "",
            }
            for p in products
            if p.is_available
        ]
    )

    # DIQQAT: bular — "Buyurtmalar" jadvali sahifani qayta yuklamasdan (F5
    # bosmasdan) o'zi yangilanib turishi (avto-refresh) uchun JS tomonida
    # kerak bo'ladigan ma'lumotlar. JSON.parse orqali data-atributlardan
    # o'qiladi (Jinja'ni to'g'ridan-to'g'ri <script> ichiga yozishdan farqli —
    # bu ancha xavfsizroq usul, qarang admin.html).
    couriers_json = json.dumps([{"id": c.id, "full_name": c.full_name} for c in couriers])
    order_statuses_json = json.dumps([s.value for s in OrderStatus])
    status_labels_json = json.dumps(STATUS_LABELS_UZ)
    next_status_map_json = json.dumps(NEXT_STATUS_MAP)

    # ---- BUGUN TUG'ILGAN KUNI BO'LGAN MIJOZLAR (OWNER va operator ikkalasi ham ko'radi) ----
    # DIQQAT: bu yerda faqat oy+kun solishtiriladi (yil emas), chunki bizni
    # "kim bugun tug'ilgan" qiziqtiradi, "kim aynan shu yil tug'ilgan" emas.
    birthday_query = await db.execute(
        select(User).where(
            User.role == UserRole.CLIENT,
            User.birth_date.isnot(None),
            extract("month", User.birth_date) == today.month,
            extract("day", User.birth_date) == today.day,
        )
    )
    birthday_clients_today = birthday_query.scalars().all()
    for _c in birthday_clients_today:
        _c.age = today.year - _c.birth_date.year - (
            (today.month, today.day) < (_c.birth_date.month, _c.birth_date.day)
        )

    # ---- TRANZAKSIYALAR TARIXI (faqat OWNER, oxirgi 50 ta) ----
    recent_transactions = []
    if is_owner:
        tx_query = await db.execute(
            select(Transaction)
            .options(
                selectinload(Transaction.user),
                selectinload(Transaction.partner),
                selectinload(Transaction.created_by),
            )
            .order_by(Transaction.created_at.desc())
            .limit(50)
        )
        recent_transactions = tx_query.scalars().all()

    # ---- OPERATOR KO'RISH HUQUQLARI — admin.html shu bo'yicha nav va
    # bo'limlarni ko'rsatadi/yashiradi (frontend tarafidagi yashirish;
    # backend tarafidagi "haqiqiy" himoya — qarang require_section) ----
    operator_perms = await get_operator_permissions(db)
    can_see_finance = is_owner or operator_perms.get("moliya")

    # ---- KUTILAYOTGAN PUL YECHISH SO'ROVLARI (OWNER, yoki "moliya" ruxsati
    # berilgan operator — qarang require_section("moliya")) ----
    pending_withdrawals = []
    if can_see_finance:
        wd_query = await db.execute(
            select(WithdrawalRequest)
            .options(
                selectinload(WithdrawalRequest.user),
                selectinload(WithdrawalRequest.partner),
                selectinload(WithdrawalRequest.card),
            )
            .where(WithdrawalRequest.status == WithdrawalStatus.PENDING)
            .order_by(WithdrawalRequest.requested_at)
        )
        pending_withdrawals = wd_query.scalars().all()
        # P2P o'tkazma qilish uchun OWNER/operatorga karta raqami TO'LIQ
        # (deshifrlangan) holda ko'rinishi shart — shuning uchun shu yerda,
        # faqat shu so'rov doirasida, bir martalik deshifrlab, har bir
        # so'rov obyektiga vaqtinchalik `decrypted_card` biriktiramiz
        # (bazaga yozilmaydi, faqat shu HTML javobda ko'rsatish uchun).
        for wd in pending_withdrawals:
            wd.decrypted_card = (
                card_security.decrypt_card_number(wd.card.encrypted_card_number) if wd.card else None
            )

    # ---- MIJOZLARDAN P2P TO'LOV QABUL QILISH UCHUN KARTALAR (faqat OWNER
    # boshqaradi — qarang add_p2p_card/activate_p2p_card/delete_p2p_card) ----
    p2p_cards = []
    pending_p2p_orders = []
    if is_owner:
        p2p_cards_query = await db.execute(
            select(Card).where(Card.user_id == current_user.id, Card.is_active == True).order_by(Card.created_at.desc())
        )
        p2p_cards = [{**_card_to_dict(c), "is_p2p_active": c.is_p2p_active} for c in p2p_cards_query.scalars().all()]
    if can_see_finance:
        p2p_orders_query = await db.execute(
            select(Order)
            .options(selectinload(Order.client), selectinload(Order.partner))
            .where(Order.payment_method == "p2p", Order.payment_verified == False, Order.status != OrderStatus.CANCELLED)
            .order_by(Order.created_at)
        )
        pending_p2p_orders = p2p_orders_query.scalars().all()

    # ---- ANALITIKA (faqat OWNER) ----
    # DIQQAT: komissiya foizi har bir do'kon uchun boshqacha bo'lishi mumkin
    # (partner.commission_rate), shuning uchun buni SQL darajasida bitta
    # formula bilan hisoblab bo'lmaydi — har bir buyurtmani alohida ko'rib,
    # o'sha buyurtmaning o'z hamkoriga tegishli foizi bilan hisoblaymiz.
    banners_result = await db.execute(select(Banner).order_by(Banner.display_order, Banner.id))
    banners = banners_result.scalars().all()

    analytics = None
    if is_owner:
        month_start = today.replace(day=1)
        courier_pct = (system_setting.courier_share_percent if system_setting else 80.0) / 100
        default_commission_pct = (system_setting.service_commission_percent if system_setting else 10.0) / 100

        async def compute_period_stats(period_start):
            stmt = (
                select(Order)
                .options(selectinload(Order.partner))
                .where(Order.status == OrderStatus.DELIVERED, func.date(Order.created_at) >= period_start)
            )
            if active_city_id is not None:
                stmt = stmt.join(PartnerProfile, Order.partner_id == PartnerProfile.id).where(
                    PartnerProfile.city_id == active_city_id
                )
            delivered = (await db.execute(stmt)).scalars().all()

            courier_pay = partner_pay = commission_income = delivery_income = 0.0
            for o in delivered:
                rate = (o.partner.commission_rate / 100) if o.partner else default_commission_pct
                commission = o.total_price * rate
                partner_pay += o.total_price - commission
                commission_income += commission
                c_pay = o.delivery_fee * courier_pct
                courier_pay += c_pay
                delivery_income += o.delivery_fee - c_pay

            return {
                "order_count": len(delivered),
                "courier_pay": courier_pay,
                "partner_pay": partner_pay,
                "commission_income": commission_income,
                "delivery_income": delivery_income,
                "net_profit": commission_income + delivery_income,
            }

        async def compute_loss_stats(period_start):
            stmt = select(
                func.count(Order.id), func.coalesce(func.sum(Order.total_price + Order.delivery_fee), 0)
            ).where(Order.status == OrderStatus.CANCELLED, func.date(Order.created_at) >= period_start)
            if active_city_id is not None:
                stmt = stmt.join(PartnerProfile, Order.partner_id == PartnerProfile.id).where(
                    PartnerProfile.city_id == active_city_id
                )
            count, total = (await db.execute(stmt)).first()
            return {"count": count or 0, "amount": total or 0.0}

        analytics = {
            "today": await compute_period_stats(today),
            "month": await compute_period_stats(month_start),
            "today_loss": await compute_loss_stats(today),
            "month_loss": await compute_loss_stats(month_start),
        }

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "current_user": current_user,
            "is_owner": is_owner,
            "all_cities": all_cities,
            "active_city_id": active_city_id,
            "active_couriers": active_couriers,
            "active_partners": active_partners,
            "today_orders_count": today_orders_count,
            "today_revenue": today_revenue,
            "system_setting": system_setting,
            "orders": orders,
            "couriers": couriers,
            "all_couriers": all_couriers,
            "clients": clients,
            "operators": operators,
            "partners": partners,
            "products": products,
            "products_json": products_json,
            "couriers_json": couriers_json,
            "order_statuses_json": order_statuses_json,
            "status_labels_json": status_labels_json,
            "next_status_map_json": next_status_map_json,
            "suggested_delivery_fee": suggested_delivery_fee,
            "weather_conditions": [w.value for w in WeatherCondition],
            "order_statuses": [s.value for s in OrderStatus],
            "status_labels": STATUS_LABELS_UZ,
            "next_status_map": NEXT_STATUS_MAP,
            "recent_transactions": recent_transactions,
            "pending_withdrawals": pending_withdrawals,
            "p2p_cards": p2p_cards,
            "pending_p2p_orders": pending_p2p_orders,
            "birthday_clients_today": birthday_clients_today,
            "banners": banners,
            "analytics": analytics,
            "operator_perms": operator_perms,
            "section_labels": SECTION_LABELS_UZ,
        },
    )


@admin_router.get("/admin/orders/live.json")
async def admin_orders_live(
    city_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """
    'Buyurtmalar' jadvali sahifani qayta yuklamasdan (F5siz) o'zi
    yangilanib turishi uchun — admin.html JS tomonidan bir necha
    sekundda bir marta shu yerga so'rov yuborib turadi, va faqat
    O'ZGARGAN qatorlarni (yoki yangi kelgan buyurtmalarni) DOM'da
    almashtiradi — butun sahifa "boshidan" qayta chizilmaydi.

    admin_dashboard() dagi bilan AYNAN bir xil filtrlash mantig'i.
    """
    is_owner = current_user.role == UserRole.OWNER
    active_city_id = city_id if is_owner else current_user.city_id

    orders_stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.client), selectinload(Order.courier))
        .order_by(Order.created_at.desc())
        .limit(200)
    )
    if active_city_id is not None:
        orders_stmt = orders_stmt.join(PartnerProfile, Order.partner_id == PartnerProfile.id).where(
            PartnerProfile.city_id == active_city_id
        )
    orders = (await db.execute(orders_stmt)).scalars().all()

    return JSONResponse([
        {
            "id": o.id,
            "delivery_address": o.delivery_address,
            "total_price": o.total_price,
            "delivery_fee": o.delivery_fee,
            "status": o.status.value,
            "courier_id": o.courier_id,
            "courier_name": o.courier.full_name if o.courier else None,
            "courier_phone": o.courier.phone_number if o.courier else None,
            "client_name": o.client.full_name if o.client else None,
            "client_phone": o.client.phone_number if o.client else None,
            "client_comment": o.client_comment,
            "items": [
                {"product_name": it.product_name, "quantity": it.quantity, "unit_price": it.unit_price}
                for it in o.items
            ],
        }
        for o in orders
    ])


@admin_router.get("/admin/analytics/hourly.json")
async def admin_analytics_hourly(
    city_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Dashboard'dagi JONLI GRAFIK (Chart.js) uchun — BUGUNGI kunning har
    bir soati bo'yicha nechta buyurtma tushgani va nechta so'm
    (yetkazilganlaridan) tushum kelgani. Har safar chaqirilganda qayta
    hisoblanadi — shu sababli grafik "jonli" (yangi buyurtma kelishi
    bilan tegishli soat ustuni o'zi o'sib boradi).

    DIQQAT: soat — O'ZBEKISTON vaqti bo'yicha (UZB_TZ_OFFSET), server
    (UTC) vaqti bo'yicha emas — aks holda kechqurun grafik "ertangi kun"
    soatlariga siljib ketardi."""
    is_owner = current_user.role == UserRole.OWNER
    active_city_id = city_id if is_owner else current_user.city_id

    today_uzb = (datetime.utcnow() + UZB_TZ_OFFSET).date()
    # Keng SQL oralig'i (aniq filtr Python tomonida, UZB vaqti bo'yicha) —
    # boshqa joylarda ham (masalan yuqoridagi tug'ilgan kun hisobida)
    # ishlatilgan xuddi shu naqsh.
    range_start = datetime.combine(today_uzb, datetime.min.time()) - UZB_TZ_OFFSET
    range_end = range_start + timedelta(days=1, hours=1)

    orders_stmt = select(Order.created_at, Order.total_price, Order.status).where(
        Order.created_at >= range_start, Order.created_at < range_end
    )
    if active_city_id is not None:
        orders_stmt = orders_stmt.join(PartnerProfile, Order.partner_id == PartnerProfile.id).where(
            PartnerProfile.city_id == active_city_id
        )
    rows = (await db.execute(orders_stmt)).all()

    order_counts = [0] * 24
    revenue_by_hour = [0.0] * 24
    for created_at, total_price, order_status in rows:
        local_dt = created_at + UZB_TZ_OFFSET
        if local_dt.date() != today_uzb:
            continue
        h = local_dt.hour
        order_counts[h] += 1
        if order_status == OrderStatus.DELIVERED:
            revenue_by_hour[h] += total_price or 0.0

    return JSONResponse({
        "hours": [f"{h:02d}:00" for h in range(24)],
        "order_counts": order_counts,
        "revenue_by_hour": revenue_by_hour,
    })


# ==================== 2. TIZIM SOZLAMALARI (faqat OWNER) ====================
@settings_router.post("")
async def update_settings(
    base_fee: float = Form(...),
    weather_condition: str = Form(...),
    weather_multiplier: float = Form(...),
    service_commission_percent: float = Form(...),
    courier_share_percent: float = Form(...),
    max_cards_per_courier: int = Form(3),
    max_cards_per_partner: int = Form(3),
    db: AsyncSession = Depends(get_db),
):
    try:
        weather_enum = WeatherCondition(weather_condition)
    except ValueError:
        raise HTTPException(status_code=400, detail="Noto'g'ri ob-havo qiymati")

    if not (0 <= courier_share_percent <= 100):
        raise HTTPException(status_code=400, detail="Kuryer ulushi 0-100 oralig'ida bo'lishi kerak")
    if not (1 <= max_cards_per_courier <= 20) or not (1 <= max_cards_per_partner <= 20):
        raise HTTPException(status_code=400, detail="Karta soni chegarasi 1 dan 20 gacha bo'lishi kerak")

    setting_query = await db.execute(select(SystemSetting))
    setting = setting_query.scalars().first()

    if not setting:
        setting = SystemSetting(
            base_delivery_fee=base_fee,
            weather_condition=weather_enum,
            weather_multiplier=weather_multiplier,
            service_commission_percent=service_commission_percent,
            courier_share_percent=courier_share_percent,
            max_cards_per_courier=max_cards_per_courier,
            max_cards_per_partner=max_cards_per_partner,
        )
        db.add(setting)
    else:
        setting.base_delivery_fee = base_fee
        setting.weather_condition = weather_enum
        setting.weather_multiplier = weather_multiplier
        setting.service_commission_percent = service_commission_percent
        setting.courier_share_percent = courier_share_percent
        setting.max_cards_per_courier = max_cards_per_courier
        setting.max_cards_per_partner = max_cards_per_partner

    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# Faqat shu formatlarga ruxsat — boshqa fayl turlari (masalan .exe, .php)
# serverga yuklanmasligi uchun. Hajm ham cheklanadi, aks holda kimdir
# juda katta fayl yuklab, diskni to'ldirib qo'yishi mumkin edi.
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


EXT_TO_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


async def process_uploaded_image(file: UploadFile) -> tuple[bytes, str]:
    """Yuklangan rasmni tekshiradi va (bytes, mime_type) qilib qaytaradi —
    DISKKA EMAS, chaqiruvchi funksiya buni bazaga yozadi.

    DIQQAT — MUHIM O'ZGARISH: avval rasmlar serverning "static/uploads/"
    papkasiga yozilardi. Bu Render kabi hosting'larda muammo edi: server
    qayta ishga tushganda (har bir yangi deploy, yoki bepul tarifda
    "uyquga ketib-uyg'onish") konteyner NOLDAN qayta yaratiladi va
    runtime'da yozilgan fayllar (yuklangan rasmlar) BUTUNLAY YO'QOLIB
    QOLARDI. Shuning uchun endi rasm baytlari to'g'ridan-to'g'ri bazaga
    (Postgres — bu doim saqlanadi) yoziladi.

    Xavfsizlik choralari avvalgidek: ruxsat etilgan formatlar, hajm chegarasi.
    """
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Faqat JPG, PNG yoki WEBP formatidagi rasmlarga ruxsat berilgan")

    contents = await file.read()
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Bo'sh fayl yuklandi")
    if len(contents) > MAX_IMAGE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="Rasm hajmi 5 MB dan oshmasligi kerak")

    return contents, EXT_TO_MIME[ext]


@app.get("/media/product/{product_id}")
async def media_product_image(product_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalars().first()
    if not product or not product.image_data:
        raise HTTPException(status_code=404, detail="Rasm topilmadi")
    return Response(
        content=product.image_data,
        media_type=product.image_mime or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/media/banner/{banner_id}")
async def media_banner_image(banner_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Banner).where(Banner.id == banner_id))
    banner = result.scalars().first()
    if not banner or not banner.image_data:
        raise HTTPException(status_code=404, detail="Rasm topilmadi")
    return Response(
        content=banner.image_data,
        media_type=banner.image_mime or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def get_or_create_referral_code(db: AsyncSession, client: User) -> str:
    """Har bir mijozning o'ziga xos referal kodi bo'lishi kerak — birinchi
    marta so'ralganda generatsiya qilinadi va saqlanadi (keyingi safar
    o'sha kodning o'zi qaytariladi, doim bir xil bo'lishi uchun)."""
    if client.referral_code:
        return client.referral_code

    import random
    import string
    for _ in range(10):  # kamdan-kam holatda tasodifiy to'qnashuv bo'lsa, qayta urinamiz
        candidate = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        existing = await db.execute(select(User).where(User.referral_code == candidate))
        if not existing.scalars().first():
            client.referral_code = candidate
            return candidate
    # Juda kam ehtimol, lekin himoya sifatida — id asosida kafolatlangan noyob kod
    client.referral_code = f"REF{client.id}"
    return client.referral_code


def validate_pin(pin: str) -> None:
    """4 xonali PIN — telefonda kiritish qulay bo'lishi uchun ataylab
    qisqa. Xavfsizlik: PIN bcrypt bilan xeshlanadi (auth.py)."""
    if not (pin.isdigit() and len(pin) == 4):
        raise HTTPException(status_code=400, detail="PIN aynan 4 ta raqamdan iborat bo'lishi kerak")


async def find_user_by_phone(db: AsyncSession, raw_phone: str, *, options=None) -> Optional[User]:
    """Telefon bo'yicha foydalanuvchini topadi: avval aniq (+998...) mos
    kelish, bo'lmasa — eski/noto'g'ri formatda saqlangan yozuvlarni ham
    oxirgi 9 raqam bo'yicha qidiradi (masalan '90 123 45 67')."""
    normalized = normalize_phone(raw_phone)
    if not normalized and not raw_phone:
        return None

    stmt = select(User).where(User.phone_number.in_([p for p in {normalized, raw_phone} if p]))
    if options:
        stmt = stmt.options(*options)
    user = (await db.execute(stmt)).scalars().first()
    if user:
        return user

    tail = phone_tail(raw_phone)
    if len(tail) < 9:
        return None
    stmt = select(User).where(User.phone_number.like(f"%{tail[-4:]}"))
    if options:
        stmt = stmt.options(*options)
    for candidate in (await db.execute(stmt)).scalars().all():
        if phone_tail(candidate.phone_number) == tail:
            return candidate
    return None


def get_public_base_url(request: Optional[Request] = None) -> str:
    """Serverning tashqi (https) manzili. Render kabi proxy ortida
    request.base_url ko'pincha 'http://' bo'lib qoladi — Telegram esa
    webhook va Mini App tugmalari uchun FAQAT https talab qiladi, shuning
    uchun (1) PUBLIC_BASE_URL / RENDER_EXTERNAL_URL muhit o'zgaruvchisi,
    (2) X-Forwarded-Proto sarlavhasi ishlatiladi va http avtomatik
    https'ga almashtiriladi (localhost bundan mustasno)."""
    env_url = os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if env_url:
        return env_url.rstrip("/")
    if request is None:
        return ""
    base = str(request.base_url).rstrip("/")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto == "https" and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    elif base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://"):]
    return base


async def find_or_create_login_user(
    db: AsyncSession,
    phone_number: str,
    password: str,
    full_name: str,
    city_id: Optional[int] = None,
) -> User:
    """Telefon raqami bo'yicha foydalanuvchini topadi (multi-role: agar
    u allaqachon mijoz/kuryer/hamkor bo'lsa, O'SHA akkauntga profil
    qo'shiladi — yangi dublikat yaratilmaydi). Agar bu telefon OWNER yoki
    operator (ADMIN) ga tegishli bo'lsa — xavfsizlik uchun rad etamiz."""
    validate_pin(password)

    # DIQQAT: telefon HAR DOIM yagona formatga (+998XXXXXXXXX) keltiriladi.
    # Aks holda admin paneldan "90 123 45 67" deb kiritilgan kuryer/hamkor,
    # keyin botda kontakt ulashganda ("+998901234567") BOSHQA odam deb
    # hisoblanib, alohida MIJOZ akkaunti yaratilib ketardi — aynan shu
    # "kuryer/hamkor mijozlarga qo'shilib ketyapti" xatosining sababi edi.
    phone_number = normalize_phone(phone_number) or phone_number
    user = await find_user_by_phone(db, phone_number)

    if user:
        if user.role in (UserRole.OWNER, UserRole.ADMIN):
            raise HTTPException(
                status_code=400,
                detail="Bu telefon raqami admin/operator akkauntiga tegishli — uni kuryer/hamkor qilib bo'lmaydi",
            )
        user.password_hash = hash_password(password)
        user.phone_number = phone_number  # eski, noto'g'ri formatni ham tuzatib qo'yamiz
        if city_id is not None and user.city_id is None:
            user.city_id = city_id
        return user

    new_user = User(
        full_name=full_name,
        phone_number=phone_number,
        password_hash=hash_password(password),
        role=UserRole.CLIENT,  # multi-role tizimida bu shunchaki "boshlang'ich" belgi
        city_id=city_id,
        is_active=True,
    )
    db.add(new_user)
    await db.flush()
    return new_user


async def _get_or_create_setting(db: AsyncSession) -> SystemSetting:
    result = await db.execute(select(SystemSetting))
    setting = result.scalars().first()
    if not setting:
        setting = SystemSetting()
        db.add(setting)
    return setting


async def apply_cod_delivery_financials(db: AsyncSession, order: Order) -> None:
    """
    Buyurtma "Yetkazildi" (DELIVERED) deb belgilanganda avtomatik chaqiriladi.

    REAL BIZNES MANTIG'I: hozircha onlayn to'lov yo'q, ya'ni mijoz PULNI
    NAQD, KURYERGA to'laydi (mahsulot narxi + yetkazish narxi — hammasi
    birga). Demak yetkazgandan keyin kuryerning cho'ntagida bor pulning:

    - bir qismi — o'zining yetkazish haqi (buni o'zida qoldiradi, teginmaymiz)
    - qolgani — ASLIDA hamkorga va sizga (egaga) tegishli, lekin hozircha
      kuryerning qo'lida turibdi

    Shuning uchun:
    1. Kuryer balansiga QARZ sifatida yoziladi (necha pul sizga
       topshirishi kerakligini bildiradi) — buni keyin siz "Yechish"
       orqali, naqd pulni undan olganingizda, nolga tushirasiz.
    2. Hamkor balansiga esa, aksincha, SIZ UNGA QARZDORLIGINGIZ sifatida
       yoziladi (uning sotuv daromadi, komissiyadan tashqari) — buni
       hamkorga naqd pul yoki o'tkazma orqali to'laganingizda "Yechish"
       bilan nolga tushirasiz.

    Bu funksiya faqat BUYURTMA ILK MARTA "Yetkazildi" bo'lganda chaqirilishi
    kerak (chaqiruvchi tomonda tekshiriladi) — aks holda bir xil buyurtma
    uchun pul ikki marta hisoblanib ketadi.
    """
    setting = await _get_or_create_setting(db)
    courier_pct = setting.courier_share_percent / 100

    courier_earning = order.delivery_fee * courier_pct
    courier_owes = (order.total_price + order.delivery_fee) - courier_earning

    if order.courier_id and courier_owes > 0:
        courier_result = await db.execute(
            select(User).where(User.id == order.courier_id).options(selectinload(User.courier_profile))
        )
        courier = courier_result.scalars().first()
        if courier and courier.courier_profile:
            courier.courier_profile.balance += courier_owes
            db.add(Transaction(
                user_id=courier.id,
                type=TransactionType.DEPOSIT,
                amount=courier_owes,
                note=f"Naqd pul yig'ildi — buyurtma #{order.id} (sizga topshirilishi kerak)",
                created_by_id=None,
            ))
            # KREDIT LIMITI: qarz belgilangan chegaradan oshsa, kuryer
            # avtomatik bloklanadi — yangi buyurtma qabul qila olmaydi,
            # to'plagan naqd pulini egasiga topshirgunga (yoki admin
            # balansni qo'lda kamaytirgunga) qadar.
            if courier.courier_profile.balance > courier.courier_profile.credit_limit:
                courier.courier_profile.is_blocked = True

    if order.partner_id:
        partner_result = await db.execute(select(PartnerProfile).where(PartnerProfile.id == order.partner_id))
        partner = partner_result.scalars().first()
        if partner:
            commission_rate = partner.commission_rate / 100
            partner_due = order.total_price * (1 - commission_rate)
            if partner_due > 0:
                partner.balance += partner_due
                db.add(Transaction(
                    partner_id=partner.id,
                    type=TransactionType.DEPOSIT,
                    amount=partner_due,
                    note=f"Sotuv daromadi — buyurtma #{order.id} (komissiya ayirilgan)",
                    created_by_id=None,
                ))

    # ---- MIJOZGA KESHBEK VA REFERAL BONUSI ----
    # DIQQAT: bular faqat buyurtma YETKAZIB BO'LINGANDAN keyin beriladi
    # (bekor qilingan buyurtma uchun emas) — bu haqiqiy do'konlarda
    # ham shunday: suiiste'mol (buyurtma berib, keyin bekor qilib,
    # baribir bonus olish) oldini oladi.
    client_result = await db.execute(select(User).where(User.id == order.client_id))
    client = client_result.scalars().first()
    if client:
        if setting.cashback_earn_percent and setting.cashback_earn_percent > 0:
            cashback_earned = order.total_price * (setting.cashback_earn_percent / 100)
            client.cashback_balance += cashback_earned
            order.cashback_earned = cashback_earned

        # Referal bonusi — faqat BIRINCHI marta, faqat kimdir taklif qilgan bo'lsa
        if client.referred_by_id and not client.referral_bonus_given and setting.referral_bonus_amount > 0:
            delivered_count_result = await db.execute(
                select(func.count(Order.id)).where(Order.client_id == client.id, Order.status == OrderStatus.DELIVERED)
            )
            delivered_count = delivered_count_result.scalar() or 0
            if delivered_count <= 1:  # aynan shu buyurtma — birinchisi
                referrer_result = await db.execute(select(User).where(User.id == client.referred_by_id))
                referrer = referrer_result.scalars().first()
                if referrer:
                    bonus = setting.referral_bonus_amount
                    client.cashback_balance += bonus
                    referrer.cashback_balance += bonus
                    client.referral_bonus_given = True
                    try:
                        if referrer.telegram_id:
                            await send_telegram_message(
                                referrer.telegram_id,
                                f"🎁 Sizning do'stingiz birinchi buyurtmasini yetkazib oldi — sizga {bonus:,.0f} so'm keshbek berildi!",
                            )
                        if client.telegram_id:
                            await send_telegram_message(
                                client.telegram_id,
                                f"🎁 Referal orqali kelganingiz uchun sizga ham {bonus:,.0f} so'm keshbek berildi!",
                            )
                    except Exception as e:
                        print(f"Referal bildirishnomasi yuborilmadi: {e}")


# ==================== KURYER AVTO-BELGILASH (AUTO-ASSIGN) ====================
def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Ikki nuqta (GPS koordinata) orasidagi masofani km da hisoblaydi."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# Joylashuv shuncha daqiqadan eski bo'lsa, kuryer "signal yo'q/oflayn" deb
# hisoblanadi — bu qiymat AVTOMATIK BIRIKTIRISH (auto_assign_nearest_courier)
# uchun ishlatiladi va ataylab bir oz "kechikishga toqatli" (mobil brauzerlar
# fon rejimida GPS'ni sekinlashtirishi mumkin).
# ==================== PUL YECHISH (WITHDRAWAL) CHEGARALARI ====================
MIN_WITHDRAWAL_AMOUNT = 10_000.0
MAX_WITHDRAWAL_AMOUNT = 5_000_000.0


# ==================== PLASTIK KARTA — QO'SHISH / RO'YXAT / O'CHIRISH ====================
# Kuryer va hamkor uchun BIR XIL mantiq (Card.user_id — ikkalasida ham
# users.id), shuning uchun umumiy yordamchi funksiyalarga chiqarilgan;
# pastda ikkala router (courier_router_app, partner_router) shularni chaqiradi.

def _card_to_dict(card: Card) -> dict:
    """Frontendga yuboriladigan, XAVFSIZ (maskalangan) karta ma'lumoti —
    to'liq raqam HECH QACHON bu orqali chiqmaydi."""
    return {
        "id": card.id,
        "masked_number": card_security.mask_card_number(card.encrypted_card_number, already_encrypted=True),
        "card_holder_name": card.card_holder_name,
        "expire": f"{card.expire_month:02d}/{str(card.expire_year)[-2:]}",
        "bank_name": card.bank_name,
        "card_type": card.card_type,
        "is_active": card.is_active,
    }


async def _add_card_for_user(
    db: AsyncSession, user_id: int, card_number: str, card_holder_name: str, expire_month: int, expire_year: int,
    *, max_cards: Optional[int] = None,
) -> Card:
    """`max_cards` berilsa (kuryer/hamkor uchun — qarang SystemSetting.max_cards_per_courier/
    max_cards_per_partner), shu foydalanuvchining FAOL kartalari soni shu chegaradan
    oshsa, yangi karta qo'shilmaydi. OWNER/operator o'z (P2P) kartalarini qo'shganda
    `max_cards=None` uzatiladi — ularga cheklov yo'q."""
    try:
        clean_number = card_security.validate_card_number(card_number)
        card_security.validate_expiry(expire_month, expire_year)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not card_holder_name or not card_holder_name.strip():
        raise HTTPException(status_code=400, detail="Karta egasining ismini kiriting")

    if max_cards is not None:
        existing_count = (await db.execute(
            select(func.count(Card.id)).where(Card.user_id == user_id, Card.is_active == True)
        )).scalar() or 0
        if existing_count >= max_cards:
            raise HTTPException(
                status_code=400,
                detail=f"Siz ko\'pi bilan {max_cards} ta karta qo\'sha olasiz. Avval eskisini o\'chiring.",
            )

    bank_name, card_type = card_security.detect_card_bin(clean_number)
    full_year = expire_year if expire_year > 99 else 2000 + expire_year

    new_card = Card(
        user_id=user_id,
        encrypted_card_number=card_security.encrypt_card_number(clean_number),
        card_holder_name=card_holder_name.strip(),
        expire_month=expire_month,
        expire_year=full_year,
        bank_name=bank_name,
        card_type=card_type,
        is_active=True,
    )
    db.add(new_card)
    await db.commit()
    await db.refresh(new_card)
    return new_card


async def _get_card_limits(db: AsyncSession) -> tuple[int, int]:
    """(kuryer uchun limit, hamkor uchun limit) — SystemSetting'dan, bo'lmasa standart 3."""
    setting_query = await db.execute(select(SystemSetting))
    setting = setting_query.scalars().first()
    if not setting:
        return 3, 3
    return (setting.max_cards_per_courier or 3), (setting.max_cards_per_partner or 3)


async def _delete_card_for_user(db: AsyncSession, user_id: int, card_id: int) -> None:
    result = await db.execute(select(Card).where(Card.id == card_id, Card.user_id == user_id))
    card = result.scalars().first()
    if not card:
        raise HTTPException(status_code=404, detail="Karta topilmadi")

    # DIQQAT: hard-delete emas — agar bu kartaga bog'langan eski
    # WithdrawalRequest bo'lsa (tarix uchun), uni "yo'qotib qo'ymaslik"
    # uchun faqat is_active=False qilamiz (ro'yxatda, tanlashda ko'rinmaydi).
    card.is_active = False
    await db.commit()


COURIER_LOCATION_FRESHNESS_MINUTES = 15

# Admin/operator JONLI XARITASI uchun alohida, ANCHA QATTIQROQ chegara:
# kuryer ilovani yopib qo'ysa (watchPosition to'xtaydi), u xaritadan shu
# necha daqiqada AVTOMATIK yo'qoladi — garchi bazada hali "online" deb
# belgilangan bo'lsa ham (buyurtma qabul qilish huquqiga tegmaydi, faqat
# xaritada ko'rinishga ta'sir qiladi).
MAP_LOCATION_FRESHNESS_MINUTES = 2


async def auto_assign_nearest_courier(db: AsyncSession, order: Order) -> Optional[User]:
    """
    Buyurtma "Kuryer izlanmoqda" holatiga o'tganda avtomatik chaqiriladi.

    Hamkorning (do'konning) joylashuvidan eng yaqin turgan, hozir ONLINE,
    joylashuvi so'nggi COURIER_LOCATION_FRESHNESS_MINUTES daqiqa ichida
    yangilangan va hozir boshqa buyurtmani yetkazib yurmagan kuryerni
    avtomatik shu buyurtmaga biriktiradi.

    Mos kuryer topilmasa (masalan hech kimning joylashuvi yo'q, yoki
    hamkorning o'zi xaritadan manzil belgilamagan) — HECH NARSA qilmaydi,
    buyurtma "Yangi buyurtmalar" ro'yxatida qolib, kuryerlar o'zi qo'lda
    "Qabul qilaman" bosishi mumkin bo'lib qoladi (zaxira reja).
    """
    if order.status != OrderStatus.LOOKING_FOR_COURIER or order.courier_id is not None:
        return None

    partner_result = await db.execute(select(PartnerProfile).where(PartnerProfile.id == order.partner_id))
    partner = partner_result.scalars().first()
    if not partner or partner.latitude is None or partner.longitude is None:
        return None  # Do'kon xaritada belgilanmagan — masofani hisoblab bo'lmaydi

    freshness_cutoff = datetime.utcnow() - timedelta(minutes=COURIER_LOCATION_FRESHNESS_MINUTES)

    candidates_stmt = (
        select(User)
        .join(CourierProfile, CourierProfile.user_id == User.id)
        .options(selectinload(User.courier_profile))
        .where(
            User.is_active == True,
            CourierProfile.is_approved == True,
            CourierProfile.is_online == True,
            CourierProfile.latitude.is_not(None),
            CourierProfile.longitude.is_not(None),
            CourierProfile.location_updated_at.is_not(None),
            CourierProfile.location_updated_at >= freshness_cutoff,
            CourierProfile.is_blocked == False,  # qarzi limitdan oshgan kuryerga avtomatik biriktirilmaydi
        )
    )
    if partner.city_id is not None:
        candidates_stmt = candidates_stmt.where(User.city_id == partner.city_id)

    candidates = (await db.execute(candidates_stmt)).scalars().all()
    if not candidates:
        return None

    busy_result = await db.execute(
        select(Order.courier_id).where(
            Order.courier_id.in_([c.id for c in candidates]),
            Order.status == OrderStatus.ON_THE_WAY,
        )
    )
    busy_ids = {row[0] for row in busy_result.all()}
    free_candidates = [c for c in candidates if c.id not in busy_ids]
    if not free_candidates:
        return None

    nearest = min(
        free_candidates,
        key=lambda c: _haversine_km(
            partner.latitude, partner.longitude,
            c.courier_profile.latitude, c.courier_profile.longitude,
        ),
    )

    order.courier_id = nearest.id
    order.status = OrderStatus.ON_THE_WAY
    await db.commit()
    return nearest


@admin_router.post("/admin/settings/birthday")
async def update_birthday_setting(
    birthday_bonus_amount: float = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("tugilgankun")),
):
    setting = await _get_or_create_setting(db)
    setting.birthday_bonus_amount = birthday_bonus_amount
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/admin/settings/referral")
async def update_referral_setting(
    referral_program_text: str = Form(""),
    referral_visible: bool = Form(False),
    referral_bonus_amount: float = Form(0.0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("referal")),
):
    setting = await _get_or_create_setting(db)
    setting.referral_program_text = referral_program_text
    setting.referral_visible = referral_visible
    setting.referral_bonus_amount = referral_bonus_amount
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/admin/settings/cashback")
async def update_cashback_setting(
    bonus_cashback_text: str = Form(""),
    cashback_visible: bool = Form(False),
    cashback_earn_percent: float = Form(0.0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("bonuscashback")),
):
    setting = await _get_or_create_setting(db)
    setting.bonus_cashback_text = bonus_cashback_text
    setting.cashback_visible = cashback_visible
    setting.cashback_earn_percent = cashback_earn_percent
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/banners/create")
async def create_banner(
    title: str = Form(""),
    text_content: str = Form(""),
    link_url: str = Form(""),
    display_order: int = Form(0),
    banner_image: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("banner")),
):
    image_data, image_mime = None, None
    if banner_image and banner_image.filename:
        image_data, image_mime = await process_uploaded_image(banner_image)

    if not image_data and not text_content.strip():
        raise HTTPException(status_code=400, detail="Banner uchun rasm yoki matn kiritilishi shart")

    try:
        db.add(Banner(
            title=title or None,
            text_content=text_content or None,
            link_url=link_url or None,
            display_order=display_order,
            image_data=image_data,
            image_mime=image_mime,
            is_active=True,
        ))
        await db.commit()
    except Exception as e:
        await db.rollback()
        # DIQQAT: agar bu yerda "column ... does not exist" degan xato
        # chiqsa — bu bazada "banners" jadvali image_data/image_mime
        # ustunlarisiz, ESKI holatda qolib ketgan degani (avtomatik
        # migratsiya ALTER TABLE'ni startup paytida logda ko'rinmas holda
        # o'tkazib yuborgan bo'lishi mumkin — qarang auto_sync_missing_columns
        # va server ishga tushgandagi "[AUTO-MIGRATE OGOHLANTIRISH]" satrlari).
        print(f"[BANNER YARATISH XATOSI] {e!r}")
        raise HTTPException(
            status_code=500,
            detail=(
                "Bannerni saqlab bo'lmadi. Agar bu birinchi marta rasm bilan banner "
                "qo'shishga urinish bo'lsa — serverni qayta ishga tushirib (Render'da "
                "'Manual Deploy' yoki 'Restart') qayta urinib ko'ring, avtomatik "
                "migratsiya bazani sozlab qo'yishi kerak."
            ),
        )
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/banners/{banner_id}/toggle")
async def toggle_banner(banner_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("banner"))):
    result = await db.execute(select(Banner).where(Banner.id == banner_id))
    banner = result.scalars().first()
    if not banner:
        raise HTTPException(status_code=404, detail="Banner topilmadi")
    banner.is_active = not banner.is_active
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/banners/{banner_id}/delete")
async def delete_banner(banner_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("banner"))):
    result = await db.execute(select(Banner).where(Banner.id == banner_id))
    banner = result.scalars().first()
    if not banner:
        raise HTTPException(status_code=404, detail="Banner topilmadi")
    await db.delete(banner)
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/admin/settings/terms")
async def update_terms_settings(
    courier_terms: str = Form(""),
    partner_terms: str = Form(""),
    client_terms: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("shartlar")),
):
    setting = await _get_or_create_setting(db)
    setting.courier_terms = courier_terms
    setting.partner_terms = partner_terms
    setting.client_terms = client_terms
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# ==================== 3. BUYURTMALAR BOSHQARUVI (login qilingan har kim) ====================
@orders_router.post("/create")
async def create_order(
    partner_id: int = Form(...),
    client_name: str = Form(...),
    client_phone: str = Form(...),
    delivery_address: str = Form(...),
    client_comment: Optional[str] = Form(None),
    product_ids: List[int] = Form(...),
    quantities: List[int] = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if len(product_ids) != len(quantities) or len(product_ids) == 0:
        raise HTTPException(status_code=400, detail="Mahsulotlar ro'yxati noto'g'ri")

    partner_for_city_query = await db.execute(select(PartnerProfile).where(PartnerProfile.id == partner_id))
    partner_for_city = partner_for_city_query.scalars().first()
    if not partner_for_city:
        raise HTTPException(status_code=404, detail="Bunday do'kon topilmadi")

    client_query = await db.execute(select(User).where(User.phone_number == client_phone))
    client = client_query.scalars().first()
    if not client:
        client = User(
            full_name=client_name,
            phone_number=client_phone,
            role=UserRole.CLIENT,
            is_active=True,
            # Mijoz birinchi marta qaysi shahar do'konidan buyurtma qilsa,
            # o'sha shaharga "yozib qo'yamiz" — shu orqali operatorlar
            # faqat o'z shahridagi mijozlarni ko'radi.
            city_id=partner_for_city.city_id,
        )
        db.add(client)
        await db.flush()
    elif client.city_id is None:
        # Mijoz avval Telegram bot orqali ro'yxatdan o'tgan bo'lishi mumkin —
        # o'sha payt shahri hali noma'lum edi. Endi birinchi buyurtmasi
        # orqali shahrini aniqlab, to'ldirib qo'yamiz.
        client.city_id = partner_for_city.city_id

    total_price = 0.0
    order_items_data = []
    for product_id, qty in zip(product_ids, quantities):
        if qty <= 0:
            continue
        prod_query = await db.execute(
            select(Product).where(Product.id == product_id, Product.partner_id == partner_id)
        )
        product = prod_query.scalars().first()
        if not product:
            raise HTTPException(status_code=400, detail=f"Mahsulot topilmadi (id={product_id})")

        total_price += product.price * qty
        order_items_data.append((product, qty))

    if not order_items_data:
        raise HTTPException(status_code=400, detail="Kamida bitta mahsulot tanlanishi kerak")

    if partner_for_city.min_order_amount and total_price < partner_for_city.min_order_amount:
        raise HTTPException(
            status_code=400,
            detail=f"Bu do'konda minimal buyurtma summasi {partner_for_city.min_order_amount:.0f} so'm",
        )

    setting_query = await db.execute(select(SystemSetting))
    setting = setting_query.scalars().first()
    delivery_fee = setting.base_delivery_fee * setting.weather_multiplier if setting else 10000.0

    new_order = Order(
        client_id=client.id,
        partner_id=partner_id,
        status=OrderStatus.CREATED,
        total_price=total_price,
        delivery_fee=delivery_fee,
        delivery_address=delivery_address,
        client_comment=client_comment,
    )
    db.add(new_order)
    await db.flush()

    for product, qty in order_items_data:
        db.add(
            OrderItem(
                order_id=new_order.id,
                product_id=product.id,
                product_name=product.name,
                unit_price=product.price,
                quantity=qty,
            )
        )

    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@orders_router.post("/{order_id}/update")
async def update_order_status_and_courier(
    order_id: int,
    new_status: str = Form(...),
    courier_id: Optional[int] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    order_query = await db.execute(
        select(Order)
        .options(selectinload(Order.partner))
        .where(Order.id == order_id)
    )
    order = order_query.scalars().first()

    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")

    old_status = order.status
    old_courier_id = order.courier_id

    try:
        order.status = OrderStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Noto'g'ri buyurtma holati")

    if courier_id:
        order.courier_id = courier_id

    # Avtomatik moliyaviy hisob-kitob — FAQAT buyurtma ENDI, shu safar
    # birinchi marta "Yetkazildi" bo'layotgan bo'lsa (aks holda takroriy
    # saqlashda pul ikki marta hisoblanib ketardi).
    if order.status == OrderStatus.DELIVERED and old_status != OrderStatus.DELIVERED:
        await apply_cod_delivery_financials(db, order)

    await db.commit()

    # ---- TELEGRAM BILDIRISHNOMALARI ----
    # DIQQAT: bu yerdagi xatoliklar (masalan Telegram serveriga ulanib
    # bo'lmasa) buyurtmani yangilashni to'xtatmasligi kerak — shuning
    # uchun commit'dan KEYIN, alohida yuborilyapti.
    try:
        # Mijozga: status haqiqatan o'zgargan bo'lsagina xabar boramiz
        if order.status != old_status:
            client_query = await db.execute(select(User).where(User.id == order.client_id))
            client = client_query.scalars().first()
            if client and client.telegram_id:
                label = STATUS_LABELS_UZ.get(order.status.value, order.status.value)
                await send_telegram_message(
                    client.telegram_id,
                    f"📦 <b>Buyurtma #{order.id}</b> holati yangilandi:\n<b>{label}</b>",
                )

        # Kuryerga: yangi biriktirilgan bo'lsa (avval yo'q edi yoki boshqa kuryer edi)
        if courier_id and courier_id != old_courier_id:
            courier_query = await db.execute(select(User).where(User.id == courier_id))
            courier = courier_query.scalars().first()
            if courier and courier.telegram_id:
                partner_name = order.partner.brand_name if order.partner else "—"
                await send_telegram_message(
                    courier.telegram_id,
                    f"🛵 Sizga yangi buyurtma biriktirildi!\n\n"
                    f"<b>Buyurtma:</b> #{order.id}\n"
                    f"<b>Do'kon:</b> {partner_name}\n"
                    f"<b>Manzil:</b> {order.delivery_address}",
                )
    except Exception as e:
        print(f"Bildirishnoma yuborishda xatolik (buyurtma ishlashiga ta'sir qilmadi): {e}")

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@orders_router.post("/{order_id}/update-json")
async def update_order_status_json(
    order_id: int,
    new_status: str = Form(...),
    courier_id: Optional[int] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Yuqoridagi `/update` bilan AYNAN bir xil ish mantig'i (status,
    moliya, Telegram bildirishnomalari) — faqat sahifani qayta
    yuklab (redirect) yubormaydi, JSON qaytaradi. Admin/operator
    panelidagi KANBAN taxtasi (sudrab-tashlab status o'zgartirish)
    shu endpointdan foydalanadi, chunki drag-and-drop paytida butun
    sahifani qayta yuklash tajribani buzadi."""
    order_query = await db.execute(
        select(Order)
        .options(selectinload(Order.partner))
        .where(Order.id == order_id)
    )
    order = order_query.scalars().first()

    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")

    old_status = order.status
    old_courier_id = order.courier_id

    try:
        order.status = OrderStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Noto'g'ri buyurtma holati")

    if courier_id:
        order.courier_id = courier_id

    if order.status == OrderStatus.DELIVERED and old_status != OrderStatus.DELIVERED:
        await apply_cod_delivery_financials(db, order)

    await db.commit()

    try:
        if order.status != old_status:
            client_query = await db.execute(select(User).where(User.id == order.client_id))
            client = client_query.scalars().first()
            if client and client.telegram_id:
                label = STATUS_LABELS_UZ.get(order.status.value, order.status.value)
                await send_telegram_message(
                    client.telegram_id,
                    f"📦 <b>Buyurtma #{order.id}</b> holati yangilandi:\n<b>{label}</b>",
                )
        if courier_id and courier_id != old_courier_id:
            courier_query = await db.execute(select(User).where(User.id == courier_id))
            courier = courier_query.scalars().first()
            if courier and courier.telegram_id:
                partner_name = order.partner.brand_name if order.partner else "—"
                await send_telegram_message(
                    courier.telegram_id,
                    f"🛵 Sizga yangi buyurtma biriktirildi!\n\n"
                    f"<b>Buyurtma:</b> #{order.id}\n"
                    f"<b>Do'kon:</b> {partner_name}\n"
                    f"<b>Manzil:</b> {order.delivery_address}",
                )
    except Exception as e:
        print(f"Bildirishnoma yuborishda xatolik (buyurtma ishlashiga ta'sir qilmadi): {e}")

    return JSONResponse({"ok": True, "id": order.id, "status": order.status.value})


# ==================== 4. DO'KONLAR BOSHQARUVI ====================
# DIQQAT: create/update/delete — faqat OWNER (require_owner qo'shimcha tekshiruvi bilan).
# toggle — operator ham qila oladi (router darajasidagi get_current_admin_user yetarli).
@partners_router.post("/create")
async def create_partner(
    request: Request,
    brand_name: str = Form(...),
    category: str = Form(...),
    address: str = Form(...),
    city_id: int = Form(...),
    commission_rate: float = Form(10.0),
    opening_time: str = Form("09:00"),
    closing_time: str = Form("23:00"),
    min_order_amount: float = Form(0.0),
    login_phone: Optional[str] = Form(None),
    login_password: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    owner: User = Depends(require_owner),
):
    new_partner = PartnerProfile(
        brand_name=brand_name,
        category=category,
        address=address,
        city_id=city_id,
        commission_rate=commission_rate,
        opening_time=opening_time,
        closing_time=closing_time,
        min_order_amount=min_order_amount,
        is_open=True,
        balance=0.0,
    )
    db.add(new_partner)
    await db.flush()

    # Agar telefon+parol kiritilgan bo'lsa — do'kon egasi kirishi mumkin
    # bo'lgan alohida akkaunt (User, role=PARTNER) yaratamiz va shu
    # do'konga bog'laymiz. Kiritilmasa — do'konni faqat siz boshqarasiz,
    # bu ham to'liq to'g'ri variant.
    partner_user = None
    if login_phone and login_password:
        partner_user = await find_or_create_login_user(db, login_phone, login_password, brand_name, city_id)
        new_partner.user_id = partner_user.id

    await db.commit()

    # Agar bu odam avval botga /start bosib, Telegram'ga ulangan bo'lsa —
    # endi unga to'g'ridan-to'g'ri "kabinetni ochish" tugmasini yuboramiz,
    # shunda u qayta PIN kiritmasdan, Mini App orqali kirib ketaveradi.
    if partner_user and partner_user.telegram_id:
        try:
            partner_url = get_public_base_url(request) + "/login?next=/partner"
            await send_telegram_message(
                partner_user.telegram_id,
                f"🎉 <b>{brand_name}</b> do'koningiz tizimga to'liq qo'shildi!\n\n"
                f"Quyidagi tugma orqali kabinetingizni oching:",
                reply_markup={"inline_keyboard": [[{"text": "🏪 Hamkor kabinetini ochish", "web_app": {"url": partner_url}}]]},
            )
        except Exception as e:
            print(f"Hamkorga Telegram xabari yuborilmadi: {e}")

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@partners_router.post("/{partner_id}/update")
async def update_partner(
    partner_id: int,
    brand_name: str = Form(...),
    category: str = Form(...),
    address: str = Form(...),
    city_id: int = Form(...),
    commission_rate: float = Form(...),
    opening_time: str = Form(...),
    closing_time: str = Form(...),
    min_order_amount: float = Form(0.0),
    db: AsyncSession = Depends(get_db),
    owner: User = Depends(require_owner),
):
    partner_query = await db.execute(select(PartnerProfile).where(PartnerProfile.id == partner_id))
    partner = partner_query.scalars().first()

    if not partner:
        raise HTTPException(status_code=404, detail="Do'kon topilmadi")

    partner.brand_name = brand_name
    partner.category = category
    partner.address = address
    partner.city_id = city_id
    partner.commission_rate = commission_rate
    partner.opening_time = opening_time
    partner.closing_time = closing_time
    partner.min_order_amount = min_order_amount

    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@partners_router.post("/{partner_id}/toggle")
async def toggle_partner_status(partner_id: int, db: AsyncSession = Depends(get_db)):
    partner_query = await db.execute(select(PartnerProfile).where(PartnerProfile.id == partner_id))
    partner = partner_query.scalars().first()
    if not partner:
        raise HTTPException(status_code=404, detail="Do'kon topilmadi")

    partner.is_open = not partner.is_open
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@partners_router.post("/{partner_id}/set-login")
async def set_partner_login(
    partner_id: int,
    login_phone: str = Form(...),
    login_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
    owner: User = Depends(require_owner),
):
    """Avval kirish ma'lumotisiz yaratilgan do'konga keyinroq login/parol
    berish uchun (yoki mavjud parolni yangilash uchun)."""
    partner_query = await db.execute(
        select(PartnerProfile).options(selectinload(PartnerProfile.user)).where(PartnerProfile.id == partner_id)
    )
    partner = partner_query.scalars().first()
    if not partner:
        raise HTTPException(status_code=404, detail="Do'kon topilmadi")

    if partner.user_id:
        # Allaqachon akkaunti bor — parol/telefonni yangilaymiz
        validate_pin(login_password)
        partner.user.phone_number = normalize_phone(login_phone) or login_phone
        partner.user.password_hash = hash_password(login_password)
        partner.user.is_active = True
    else:
        partner_user = await find_or_create_login_user(db, login_phone, login_password, partner.brand_name)
        partner.user_id = partner_user.id

    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@partners_router.post("/{partner_id}/delete")
async def delete_partner(
    partner_id: int,
    db: AsyncSession = Depends(get_db),
    owner: User = Depends(require_owner),
):
    partner_query = await db.execute(select(PartnerProfile).where(PartnerProfile.id == partner_id))
    partner = partner_query.scalars().first()
    if not partner:
        raise HTTPException(status_code=404, detail="Do'kon topilmadi")

    await db.delete(partner)
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# ==================== 5. MAHSULOTLAR (MENU) BOSHQARUVI ====================
@products_router.post("/create")
async def create_product(
    partner_id: int = Form(...),
    name: str = Form(...),
    price: float = Form(...),
    description: Optional[str] = Form(None),
    category: str = Form("Boshqa"),
    image: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
    owner: User = Depends(require_owner),
):
    partner_query = await db.execute(select(PartnerProfile).where(PartnerProfile.id == partner_id))
    if not partner_query.scalars().first():
        raise HTTPException(status_code=404, detail="Bunday do'kon topilmadi")

    image_data, image_mime = None, None
    if image and image.filename:
        image_data, image_mime = await process_uploaded_image(image)

    new_product = Product(
        partner_id=partner_id,
        name=name,
        price=price,
        description=description,
        category=category,
        image_data=image_data,
        image_mime=image_mime,
        is_available=True,
    )
    db.add(new_product)
    await db.flush()
    if image_data:
        new_product.image_url = f"/media/product/{new_product.id}"
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@products_router.post("/{product_id}/update")
async def update_product(
    product_id: int,
    name: str = Form(...),
    price: float = Form(...),
    description: Optional[str] = Form(None),
    category: str = Form("Boshqa"),
    image: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
    owner: User = Depends(require_owner),
):
    prod_query = await db.execute(select(Product).where(Product.id == product_id))
    product = prod_query.scalars().first()

    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")

    product.name = name
    product.price = price
    product.description = description
    product.category = category

    # Rasm faqat YANGI fayl tanlangandagina almashtiriladi — bo'sh
    # qoldirsa, eski rasm o'zgarmasdan qoladi (har safar qayta yuklashga
    # majburlamaslik uchun).
    if image and image.filename:
        image_data, image_mime = await process_uploaded_image(image)
        product.image_data = image_data
        product.image_mime = image_mime
        product.image_url = f"/media/product/{product.id}"

    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@products_router.post("/{product_id}/toggle")
async def toggle_product(product_id: int, db: AsyncSession = Depends(get_db)):
    # DIQQAT: toggle (mavjud/tugadi belgilash) — operator ham qila oladi,
    # chunki bu kunlik operatsion ish (masalan "bugun tovuq tugadi").
    prod_query = await db.execute(select(Product).where(Product.id == product_id))
    product = prod_query.scalars().first()
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")

    product.is_available = not product.is_available
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@products_router.post("/{product_id}/delete")
async def delete_product(product_id: int, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    prod_query = await db.execute(select(Product).where(Product.id == product_id))
    product = prod_query.scalars().first()
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")

    await db.delete(product)
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# ==================== 6. KURYERLAR BOSHQARUVI ====================
@couriers_router.get("/live-locations")
async def couriers_live_locations(
    city_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Admin/operator xaritasi uchun JSON — BITTA so'rovda uchta qatlamni
    qaytaradi: (1) hozir ONLINE va joylashuvi 'yangi' bo'lgan kuryerlar,
    (2) shu shahardagi ochiq hamkorlar/do'konlar (statik joylashuv),
    (3) hozir faol (hali yetkazilmagan/bekor qilinmagan) buyurtmalarning
    mijoz-yetkazish nuqtalari. Operator (ADMIN) uchun bu har doim FAQAT
    o'z shahri bilan cheklanadi — OWNER esa city_id orqali istalgan
    shaharni yoki (bermasa) hammasini ko'radi."""
    is_owner = current_user.role == UserRole.OWNER
    active_city_id = city_id if is_owner else current_user.city_id

    # DIQQAT: bu yerda MAP_LOCATION_FRESHNESS_MINUTES (qattiqroq) ishlatiladi,
    # COURIER_LOCATION_FRESHNESS_MINUTES emas — sabab yuqoridagi izohda.
    freshness_cutoff = datetime.utcnow() - timedelta(minutes=MAP_LOCATION_FRESHNESS_MINUTES)

    # ---- 1) KURYERLAR ----
    courier_stmt = (
        select(User)
        .join(CourierProfile, CourierProfile.user_id == User.id)
        .options(selectinload(User.courier_profile))
        .where(
            User.is_active == True,
            CourierProfile.is_online == True,
            CourierProfile.latitude.is_not(None),
            CourierProfile.longitude.is_not(None),
            CourierProfile.location_updated_at >= freshness_cutoff,
        )
    )
    if active_city_id is not None:
        courier_stmt = courier_stmt.where(User.city_id == active_city_id)

    couriers = (await db.execute(courier_stmt)).scalars().all()

    courier_ids = [c.id for c in couriers]
    active_orders_map = {}
    if courier_ids:
        orders_result = await db.execute(
            select(Order).where(Order.courier_id.in_(courier_ids), Order.status == OrderStatus.ON_THE_WAY)
        )
        for o in orders_result.scalars().all():
            active_orders_map[o.courier_id] = o

    couriers_data = []
    for c in couriers:
        cp = c.courier_profile
        active_order = active_orders_map.get(c.id)
        couriers_data.append({
            "courier_id": c.id,
            "full_name": c.full_name,
            "phone_number": c.phone_number,
            "transport_type": cp.transport_type,
            "lat": cp.latitude,
            "lng": cp.longitude,
            "updated_at": cp.location_updated_at.isoformat() if cp.location_updated_at else None,
            "busy": active_order is not None,
            "active_order_id": active_order.id if active_order else None,
            "active_order_address": active_order.delivery_address if active_order else None,
        })

    # ---- 2) HAMKORLAR / DO'KONLAR ----
    partner_stmt = select(PartnerProfile).where(
        PartnerProfile.latitude.is_not(None),
        PartnerProfile.longitude.is_not(None),
    )
    if active_city_id is not None:
        partner_stmt = partner_stmt.where(PartnerProfile.city_id == active_city_id)
    partners = (await db.execute(partner_stmt)).scalars().all()
    partners_data = [
        {
            "partner_id": p.id,
            "name": p.brand_name,
            "category": p.category,
            "address": p.address,
            "lat": p.latitude,
            "lng": p.longitude,
            "is_open": p.is_open,
        }
        for p in partners
    ]

    # ---- 3) FAOL BUYURTMALAR — MIJOZ YETKAZISH NUQTASI ----
    orders_stmt = (
        select(Order)
        .options(selectinload(Order.client), selectinload(Order.partner))
        .where(
            Order.status.notin_([OrderStatus.DELIVERED, OrderStatus.CANCELLED]),
            Order.delivery_latitude.is_not(None),
            Order.delivery_longitude.is_not(None),
        )
    )
    if active_city_id is not None:
        orders_stmt = orders_stmt.join(PartnerProfile, Order.partner_id == PartnerProfile.id).where(
            PartnerProfile.city_id == active_city_id
        )
    active_orders = (await db.execute(orders_stmt)).scalars().all()
    clients_data = [
        {
            "order_id": o.id,
            "status": o.status.value,
            "client_name": o.client.full_name if o.client else None,
            "partner_name": o.partner.brand_name if o.partner else None,
            "delivery_address": o.delivery_address,
            "lat": o.delivery_latitude,
            "lng": o.delivery_longitude,
        }
        for o in active_orders
    ]

    return JSONResponse({"couriers": couriers_data, "partners": partners_data, "active_clients": clients_data})


@couriers_router.post("/create")
async def create_courier(
    full_name: str = Form(...),
    phone_number: str = Form(...),
    password: str = Form(...),
    transport_type: str = Form("walking"),
    city_id: Optional[int] = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    # Operator faqat o'z shahriga kuryer qo'sha oladi — city_id majburan o'ziniki bo'ladi
    resolved_city_id = city_id if current_user.role == UserRole.OWNER else current_user.city_id

    new_user = await find_or_create_login_user(db, phone_number, password, full_name, resolved_city_id)

    existing_profile = await db.execute(select(CourierProfile).where(CourierProfile.user_id == new_user.id))
    if existing_profile.scalars().first():
        raise HTTPException(status_code=400, detail="Bu foydalanuvchi allaqachon kuryer sifatida ro'yxatdan o'tgan")

    new_courier_profile = CourierProfile(
        user_id=new_user.id,
        transport_type=transport_type,
        is_approved=True,
        is_online=False,
        balance=0.0,
    )
    db.add(new_courier_profile)

    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@couriers_router.post("/{user_id}/update")
async def update_courier(
    user_id: int,
    full_name: str = Form(...),
    phone_number: str = Form(...),
    transport_type: str = Form(...),
    city_id: Optional[int] = Form(None),
    credit_limit: Optional[float] = Form(None),
    new_password: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    user_query = await db.execute(
        select(User)
        .join(CourierProfile, CourierProfile.user_id == User.id)
        .where(User.id == user_id)
        .options(selectinload(User.courier_profile))
    )
    courier_user = user_query.scalars().first()
    if not courier_user:
        raise HTTPException(status_code=404, detail="Kuryer topilmadi")

    if phone_number != courier_user.phone_number:
        existing_query = await db.execute(
            select(User).where(User.phone_number == phone_number, User.id != user_id)
        )
        if existing_query.scalars().first():
            raise HTTPException(status_code=400, detail="Bu telefon raqami boshqa foydalanuvchiga tegishli")

    courier_user.full_name = full_name
    courier_user.phone_number = normalize_phone(phone_number) or phone_number
    if courier_user.courier_profile:
        courier_user.courier_profile.transport_type = transport_type

        # Kredit limitini (naqd qarz chegarasini) FAQAT OWNER o'zgartira
        # oladi — bu kuryerga nechchi pulgacha "ishonish"ni belgilaydigan
        # moliyaviy qaror, operatorga delegatsiya qilinmaydi. Qiymat
        # o'zgartirilgach, bloklanish holati ham DARHOL qayta tekshiriladi
        # (masalan limit oshirilsa va qarz endi chegaradan past bo'lsa,
        # kuryer avtomatik blokdan chiqadi; pasaytirilsa — aksincha).
        if current_user.role == UserRole.OWNER and credit_limit is not None:
            if credit_limit < 0:
                raise HTTPException(status_code=400, detail="Kredit limiti manfiy bo'la olmaydi")
            courier_user.courier_profile.credit_limit = credit_limit
            courier_user.courier_profile.is_blocked = (
                courier_user.courier_profile.balance > courier_user.courier_profile.credit_limit
            )

    if new_password:
        validate_pin(new_password)
        courier_user.password_hash = hash_password(new_password)

    # Faqat OWNER kuryerni boshqa shaharga o'tkaza oladi
    if current_user.role == UserRole.OWNER and city_id is not None:
        courier_user.city_id = city_id

    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@couriers_router.post("/{user_id}/toggle")
async def toggle_courier(user_id: int, db: AsyncSession = Depends(get_db)):
    user_query = await db.execute(
        select(User).join(CourierProfile, CourierProfile.user_id == User.id).where(User.id == user_id)
    )
    courier_user = user_query.scalars().first()
    if not courier_user:
        raise HTTPException(status_code=404, detail="Kuryer topilmadi")

    courier_user.is_active = not courier_user.is_active
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@couriers_router.post("/{user_id}/toggle-block")
async def toggle_courier_block(user_id: int, db: AsyncSession = Depends(get_db)):
    """OWNER/operatorning QO'LDA bloklash/blokdan chiqarish tugmasi — avtomatik
    kredit-limit blokidan farqli, bu yerda inson qaror qabul qiladi (masalan,
    kuryer qarzi hali to'lanmagan bo'lsa ham, vaqtincha ishlashga ruxsat berish)."""
    profile_query = await db.execute(
        select(CourierProfile).join(User, User.id == CourierProfile.user_id).where(User.id == user_id)
    )
    profile = profile_query.scalars().first()
    if not profile:
        raise HTTPException(status_code=404, detail="Kuryer topilmadi")
    profile.is_blocked = not profile.is_blocked
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@couriers_router.post("/{user_id}/delete")
async def delete_courier(user_id: int, db: AsyncSession = Depends(get_db)):
    user_query = await db.execute(
        select(User).join(CourierProfile, CourierProfile.user_id == User.id).where(User.id == user_id)
    )
    courier_user = user_query.scalars().first()
    if not courier_user:
        raise HTTPException(status_code=404, detail="Kuryer topilmadi")

    await db.delete(courier_user)
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# ==================== 7. MIJOZLAR BOSHQARUVI ====================
@clients_router.post("/{user_id}/toggle")
async def toggle_client(user_id: int, db: AsyncSession = Depends(get_db)):
    user_query = await db.execute(select(User).where(User.id == user_id, User.role == UserRole.CLIENT))
    client_user = user_query.scalars().first()
    if not client_user:
        raise HTTPException(status_code=404, detail="Mijoz topilmadi")

    client_user.is_active = not client_user.is_active
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@clients_router.post("/{user_id}/birthday-bonus")
async def mark_birthday_bonus_given(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    owner: User = Depends(require_owner),  # bu amal faqat OWNER uchun
):
    client_query = await db.execute(select(User).where(User.id == user_id, User.role == UserRole.CLIENT))
    client = client_query.scalars().first()
    if not client:
        raise HTTPException(status_code=404, detail="Mijoz topilmadi")

    setting = await _get_or_create_setting(db)

    # Balans emas, faqat TARIX sifatida yoziladi — chunki mijozda balans
    # tushunchasi yo'q, bu shunchaki "kimga qachon bonus berilgani"ni
    # eslab qolish uchun.
    db.add(Transaction(
        user_id=client.id,
        type=TransactionType.DEPOSIT,
        amount=setting.birthday_bonus_amount,
        note=f"Tug'ilgan kun bonusi — {client.full_name}",
        created_by_id=owner.id,
    ))
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# ==================== 8. OPERATORLAR BOSHQARUVI (faqat OWNER) ====================
@operators_router.post("/create")
async def create_operator(
    full_name: str = Form(...),
    phone_number: str = Form(...),
    password: str = Form(...),
    city_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    phone_number = normalize_phone(phone_number) or phone_number
    if await find_user_by_phone(db, phone_number):
        raise HTTPException(status_code=400, detail="Bu telefon raqami allaqachon band")

    new_operator = User(
        full_name=full_name,
        phone_number=phone_number,
        role=UserRole.ADMIN,
        city_id=city_id,
        password_hash=hash_password(password),
        is_active=True,
    )
    db.add(new_operator)
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@operators_router.post("/{user_id}/toggle")
async def toggle_operator(user_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id, User.role == UserRole.ADMIN))
    operator = result.scalars().first()
    if not operator:
        raise HTTPException(status_code=404, detail="Operator topilmadi")

    operator.is_active = not operator.is_active
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@operators_router.post("/{user_id}/delete")
async def delete_operator(user_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id, User.role == UserRole.ADMIN))
    operator = result.scalars().first()
    if not operator:
        raise HTTPException(status_code=404, detail="Operator topilmadi")

    await db.delete(operator)
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# ==================== 8. MOLIYAVIY BOSHQARUV (faqat OWNER) ====================
@finance_router.post("/courier")
async def update_courier_balance(
    user_id: int = Form(...),
    action: str = Form(...),  # "deposit" yoki "withdraw"
    amount: float = Form(...),
    note: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_owner),
):
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Summa musbat bo'lishi kerak")

    result = await db.execute(
        select(User)
        .join(CourierProfile, CourierProfile.user_id == User.id)
        .where(User.id == user_id)
        .options(selectinload(User.courier_profile))
    )
    courier = result.scalars().first()
    if not courier or not courier.courier_profile:
        raise HTTPException(status_code=404, detail="Kuryer topilmadi")

    if action == "deposit":
        courier.courier_profile.balance += amount
        tx_type = TransactionType.DEPOSIT
    elif action == "withdraw":
        courier.courier_profile.balance -= amount
        tx_type = TransactionType.WITHDRAWAL
    else:
        raise HTTPException(status_code=400, detail="Noto'g'ri amal turi")

    # Bu yerda odatda "− Yechish" tugmasi — kuryer naqd pulni egasiga
    # jismonan topshirganda bosiladi (qarz kamayadi). Agar shu tufayli
    # qarz endi kredit limitidan past bo'lsa — bloklash AVTOMATIK yechiladi.
    if courier.courier_profile.balance <= courier.courier_profile.credit_limit:
        courier.courier_profile.is_blocked = False
    elif courier.courier_profile.balance > courier.courier_profile.credit_limit:
        courier.courier_profile.is_blocked = True

    db.add(Transaction(
        user_id=courier.id,
        type=tx_type,
        amount=amount,
        note=note,
        created_by_id=current_user.id,
    ))
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@finance_router.post("/partner")
async def update_partner_balance(
    partner_id: int = Form(...),
    action: str = Form(...),
    amount: float = Form(...),
    note: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_owner),
):
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Summa musbat bo'lishi kerak")

    result = await db.execute(select(PartnerProfile).where(PartnerProfile.id == partner_id))
    partner = result.scalars().first()
    if not partner:
        raise HTTPException(status_code=404, detail="Do'kon topilmadi")

    if action == "deposit":
        partner.balance += amount
        tx_type = TransactionType.DEPOSIT
    elif action == "withdraw":
        partner.balance -= amount
        tx_type = TransactionType.WITHDRAWAL
    else:
        raise HTTPException(status_code=400, detail="Noto'g'ri amal turi")

    db.add(Transaction(
        partner_id=partner.id,
        type=tx_type,
        amount=amount,
        note=note,
        created_by_id=current_user.id,
    ))
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


async def _finalize_withdrawal_approval(
    db: AsyncSession, wd: WithdrawalRequest, approved_by_id: int, *, receipt_file_id: Optional[str] = None
) -> None:
    """Pul yechish so'rovini UZIL-KESIL tasdiqlaydi: balansni yakunlaydi
    (frozen_balance'dan chiqaradi) va tarixga yozadi. IKKI YERDAN
    chaqiriladi — (1) admin panelidagi '✅ To'landi' tugmasi, (2)
    admin/operator Telegram botga chek rasm yuborganda — shuning uchun
    bu yerda bitta joyga chiqarilgan (DRY), ikkalasi ham bir xil
    moliyaviy natijaga olib kelishi SHART. `receipt_file_id` berilsa
    (bot orqali chaqirilganda), chek shu yerga ham yoziladi."""
    if wd.status != WithdrawalStatus.PENDING:
        raise HTTPException(status_code=400, detail="Bu so'rov allaqachon ko'rib chiqilgan")

    if wd.user_id:
        # ROW-LEVEL LOCK — qarang courier_request_withdrawal'dagi izoh.
        courier_result = await db.execute(
            select(CourierProfile).where(CourierProfile.user_id == wd.user_id).with_for_update()
        )
        profile = courier_result.scalars().first()
        if profile:
            # So'rov paytida balance -300 bo'lib, frozen_balance=50000 edi.
            # Endi uzil-kesil "to'landi": balance 0'ga yaqinlashadi (-300+50000),
            # frozen_balance'dan chiqariladi.
            profile.balance += wd.amount
            profile.frozen_balance = max(0.0, profile.frozen_balance - wd.amount)
        db.add(Transaction(
            user_id=wd.user_id, type=TransactionType.WITHDRAWAL, amount=wd.amount,
            note=f"Pul yechish so'rovi #{wd.id} tasdiqlandi (karta: {wd.card_id or '—'})",
            created_by_id=approved_by_id,
        ))
    elif wd.partner_id:
        partner_result = await db.execute(
            select(PartnerProfile).where(PartnerProfile.id == wd.partner_id).with_for_update()
        )
        partner = partner_result.scalars().first()
        if partner:
            partner.balance -= wd.amount
            partner.frozen_balance = max(0.0, partner.frozen_balance - wd.amount)
        db.add(Transaction(
            partner_id=wd.partner_id, type=TransactionType.WITHDRAWAL, amount=wd.amount,
            note=f"Pul yechish so'rovi #{wd.id} tasdiqlandi (karta: {wd.card_id or '—'})",
            created_by_id=approved_by_id,
        ))

    wd.status = WithdrawalStatus.APPROVED
    wd.processed_at = datetime.utcnow()
    wd.processed_by_id = approved_by_id
    if receipt_file_id:
        wd.receipt_file_id = receipt_file_id
        wd.receipt_sent_at = datetime.utcnow()
    await db.commit()


@finance_router.post("/withdrawals/{request_id}/approve")
async def approve_withdrawal(
    request_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("moliya")),
):
    """DIQQAT: bu tugmani bosishdan oldin, pulni real hayotda (Click/Payme
    yoki naqd) kuryer/hamkorning kartasiga siz ALLAQACHON o'tkazgan
    bo'lishingiz kerak — bu tugma faqat tizimdagi balansni shunga mos
    ravishda yakunlaydi, pulni o'zi jismonan yubormaydi. Chekni Telegram
    bot orqali yuborish — alohida, ixtiyoriy qadam (qarang admin
    panelidagi "📤 Chek yuborish" havolasi)."""
    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    wd = result.scalars().first()
    if not wd:
        raise HTTPException(status_code=404, detail="So'rov topilmadi")

    await _finalize_withdrawal_approval(db, wd, current_user.id)

    try:
        if wd.user_id:
            u_result = await db.execute(select(User).where(User.id == wd.user_id))
            u = u_result.scalars().first()
            if u and u.telegram_id:
                await send_telegram_message(u.telegram_id, f"✅ {wd.amount:,.0f} so'm yechib olish so'rovingiz tasdiqlandi.")
    except Exception as e:
        print(f"Bildirishnoma xatoligi: {e}")

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@finance_router.post("/withdrawals/{request_id}/reject")
async def reject_withdrawal(
    request_id: int,
    reject_reason: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("moliya")),
):
    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    wd = result.scalars().first()
    if not wd:
        raise HTTPException(status_code=404, detail="So'rov topilmadi")
    if wd.status != WithdrawalStatus.PENDING:
        raise HTTPException(status_code=400, detail="Bu so'rov allaqachon ko'rib chiqilgan")

    # Rad etilganda — so'ralgan summa band qilingan (frozen_balance) joydan
    # chiqariladi, asosiy balansga HECH NARSA qo'shilmaydi (chunki so'rov
    # paytida asosiy balansga tegilmagan edi — faqat frozen oshgan edi),
    # natijada "mavjud mablag'" (balance − frozen) avtomatik avvalgi holatiga qaytadi.
    if wd.user_id:
        courier_result = await db.execute(
            select(CourierProfile).where(CourierProfile.user_id == wd.user_id).with_for_update()
        )
        profile = courier_result.scalars().first()
        if profile:
            profile.frozen_balance = max(0.0, profile.frozen_balance - wd.amount)
    elif wd.partner_id:
        partner_result = await db.execute(
            select(PartnerProfile).where(PartnerProfile.id == wd.partner_id).with_for_update()
        )
        partner = partner_result.scalars().first()
        if partner:
            partner.frozen_balance = max(0.0, partner.frozen_balance - wd.amount)

    wd.status = WithdrawalStatus.REJECTED
    wd.reject_reason = reject_reason.strip() or None
    wd.processed_at = datetime.utcnow()
    wd.processed_by_id = current_user.id
    await db.commit()

    try:
        if wd.user_id:
            u_result = await db.execute(select(User).where(User.id == wd.user_id))
            u = u_result.scalars().first()
            if u and u.telegram_id:
                reason_text = f"\nSabab: {wd.reject_reason}" if wd.reject_reason else ""
                await send_telegram_message(
                    u.telegram_id,
                    f"❌ {wd.amount:,.0f} so'm yechib olish so'rovingiz rad etildi.{reason_text}",
                )
    except Exception as e:
        print(f"Bildirishnoma xatoligi: {e}")

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@finance_router.post("/operator-permissions/update")
async def update_operator_permissions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    owner: User = Depends(require_owner),
):
    """Faqat OWNER: admin panelidagi har bir bo'limni operatorga
    ko'rsatish/yashirishni shu yerdan belgilaydi. Checkbox belgilanmagan
    bo'lim formadan UMUMAN kelmaydi (HTML standarti) — shuning uchun
    SECTION_LABELS_UZ ro'yxatidagi BARCHA kalitlarni kelgan/kelmaganiga
    qarab True/False qilib yozamiz (faqat belgilanganlarni emas)."""
    form = await request.form()
    for section_key in SECTION_LABELS_UZ:
        enabled = form.get(f"section_{section_key}") == "on"
        existing = await db.execute(select(OperatorPermission).where(OperatorPermission.section_key == section_key))
        row = existing.scalars().first()
        if row:
            row.enabled = enabled
        else:
            db.add(OperatorPermission(section_key=section_key, enabled=enabled))
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)



# ==================== 8-B. P2P (KARTADAN-KARTAGA) TO'LOV — OWNER KARTALARI ====================
# Mijoz checkout'da "P2P" usulini tanlasa, aynan shu yerda FAOL (is_p2p_active)
# deb belgilangan kartaning raqami ko'rsatiladi. OWNER xohlagancha karta
# qo'shishi mumkin, lekin bir vaqtda faqat BITTASI "faol" bo'ladi.

async def _get_active_p2p_card(db: AsyncSession) -> Optional[Card]:
    result = await db.execute(select(Card).where(Card.is_p2p_active == True, Card.is_active == True))
    return result.scalars().first()


@finance_router.get("/p2p-cards")
async def list_p2p_cards(db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    """OWNER'ning P2P uchun qo'shgan barcha shaxsiy kartalari."""
    result = await db.execute(
        select(Card).where(Card.user_id == owner.id, Card.is_active == True).order_by(Card.created_at.desc())
    )
    cards = result.scalars().all()
    return {"cards": [{**_card_to_dict(c), "is_p2p_active": c.is_p2p_active} for c in cards]}


@finance_router.post("/p2p-cards")
async def add_p2p_card(
    card_number: str = Form(...),
    card_holder_name: str = Form(...),
    expire_month: int = Form(...),
    expire_year: int = Form(...),
    db: AsyncSession = Depends(get_db),
    owner: User = Depends(require_owner),
):
    """OWNER o'zining shaxsiy (P2P to'lovlar uchun ko'rsatiladigan)
    kartasini qo'shadi — kuryer/hamkor kartalari bilan BIR XIL xavfsiz
    yo'l (_add_card_for_user — shifrlash, BIN aniqlash, Luhn tekshiruvi)."""
    new_card = await _add_card_for_user(db, owner.id, card_number, card_holder_name, expire_month, expire_year)

    # Agar bu OWNER'ning BIRINCHI kartasi bo'lsa, qulaylik uchun avtomatik
    # faollashtiramiz — aks holda P2P to'lov hali "kartasiz" holda qolib,
    # mijozlar bosganda hech narsa ko'rsatilmay qolishi mumkin edi.
    existing_active = await _get_active_p2p_card(db)
    if not existing_active:
        new_card.is_p2p_active = True
        await db.commit()

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@finance_router.post("/p2p-cards/{card_id}/activate")
async def activate_p2p_card(
    card_id: int, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)
):
    """Shu kartani FAOL (mijozlarga ko'rsatiladigan) qiladi, qolganlarini
    avtomatik o'chiradi — bir vaqtda faqat bitta karta faol bo'lishi uchun."""
    result = await db.execute(select(Card).where(Card.id == card_id, Card.user_id == owner.id))
    card = result.scalars().first()
    if not card:
        raise HTTPException(status_code=404, detail="Karta topilmadi")

    all_owner_cards = await db.execute(select(Card).where(Card.user_id == owner.id))
    for c in all_owner_cards.scalars().all():
        c.is_p2p_active = (c.id == card.id)
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@finance_router.post("/p2p-cards/{card_id}/delete")
async def delete_p2p_card(
    card_id: int, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)
):
    await _delete_card_for_user(db, owner.id, card_id)
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


async def _verify_p2p_order(db: AsyncSession, order: Order, approved: bool, verified_by_id: int, reason: str = "") -> None:
    """P2P buyurtmani tasdiqlaydi (hamkorga ko'rinadigan qiladi) yoki rad
    etadi (bekor qiladi). IKKI YERDAN chaqiriladi: Telegram bot
    tugmasidan va admin panelidagi endpointdan — shuning uchun DRY."""
    if order.payment_verified or order.status == OrderStatus.CANCELLED:
        raise HTTPException(status_code=400, detail="Bu buyurtma allaqachon ko'rib chiqilgan")

    if approved:
        order.payment_verified = True
        order.payment_verified_by_id = verified_by_id
        order.payment_verified_at = datetime.utcnow()
    else:
        order.status = OrderStatus.CANCELLED
        order.payment_verified_by_id = verified_by_id
        order.payment_verified_at = datetime.utcnow()
    await db.commit()

    try:
        client_result = await db.execute(select(User).where(User.id == order.client_id))
        client = client_result.scalars().first()
        if client and client.telegram_id:
            if approved:
                await send_telegram_message(
                    client.telegram_id,
                    f"✅ To'lovingiz tasdiqlandi! Buyurtma #{order.id} hamkorga yuborildi.",
                )
            else:
                reason_text = f"\nSabab: {reason}" if reason else ""
                await send_telegram_message(
                    client.telegram_id,
                    f"❌ Buyurtma #{order.id} uchun to'lovingiz tasdiqlanmadi va bekor qilindi.{reason_text}\n"
                    f"Savolingiz bo'lsa, administratsiya bilan bog'laning.",
                )
    except Exception as e:
        print(f"P2P tasdiqlash bildirishnomasi xatoligi: {e}")


@finance_router.get("/p2p-orders")
async def list_pending_p2p_orders(
    db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("moliya"))
):
    """Hali tasdiqlanmagan P2P buyurtmalar — admin panel shu ro'yxatni
    ko'rsatadi (bot orqali tasdiqlash ishlamay qolgan holatlar uchun
    zaxira yo'l)."""
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.client), selectinload(Order.partner))
        .where(Order.payment_method == "p2p", Order.payment_verified == False, Order.status != OrderStatus.CANCELLED)
        .order_by(Order.created_at)
    )
    orders = result.scalars().all()
    return {
        "orders": [
            {
                "id": o.id,
                "client_name": o.client.full_name if o.client else "—",
                "partner_name": o.partner.brand_name if o.partner else "—",
                "total": o.total_price + o.delivery_fee,
                "created_at": o.created_at.isoformat(),
                "has_receipt": bool(o.payment_receipt_file_id),
            }
            for o in orders
        ]
    }


@finance_router.post("/p2p-orders/{order_id}/approve")
async def approve_p2p_order(
    order_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("moliya"))
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalars().first()
    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    await _verify_p2p_order(db, order, True, current_user.id)
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@finance_router.post("/p2p-orders/{order_id}/reject")
async def reject_p2p_order(
    order_id: int,
    reason: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("moliya")),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalars().first()
    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    await _verify_p2p_order(db, order, False, current_user.id, reason)
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# ==================== 9. TIZIMNI TOZALASH — RESET (faqat OWNER) ====================
@app.post("/admin/reset")
async def reset_system(
    confirmation: str = Form(...),
    db: AsyncSession = Depends(get_db),
    owner: User = Depends(require_owner),
):
    # Ikki bosqichli himoya: JS'da tasdiqlash oynasi + bu yerda aniq matn
    # ("TOZALASH") kiritilishi shart. Bu — qaytarib bo'lmaydigan amal bo'lgani
    # uchun, tasodifan bosilib ketishning oldini olish uchun ataylab qattiq
    # qilingan.
    if confirmation != "TOZALASH":
        raise HTTPException(
            status_code=400,
            detail="Tasdiqlash matni noto'g'ri. Aniq katta harflarda 'TOZALASH' deb yozing.",
        )

    # DIQQAT: bu yerda ataylab FK (bog'liqlik) tartibiga rioya qilingan —
    # avval "farzand" jadvallar, keyin "ota" jadvallar o'chiriladi.
    # OWNER va operatorlar (ADMIN roli), shaharlar va tizim sozlamalari
    # HECH QACHON bu bilan o'chirilmaydi — aks holda tizimga kirolmay qolasiz.
    await db.execute(text("DELETE FROM order_items"))
    await db.execute(text("DELETE FROM orders"))
    await db.execute(text("DELETE FROM transactions"))
    await db.execute(text("DELETE FROM products"))
    await db.execute(text("DELETE FROM partner_profiles"))
    await db.execute(text("DELETE FROM courier_profiles"))
    await db.execute(text("DELETE FROM users WHERE role IN ('client', 'courier')"))
    await db.commit()

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# ==================== 8b. SHAHARLAR BOSHQARUVI (faqat OWNER) ====================
@cities_router.post("/create")
async def create_city(name: str = Form(...), db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(City).where(City.name == name))
    if existing.scalars().first():
        raise HTTPException(status_code=400, detail="Bunday shahar allaqachon mavjud")

    # DIQQAT: qo'shimcha ish qilishning hojati yo'q — shahar qo'shilgan
    # zahoti, tizimdagi barcha filtrlar (do'kon/kuryer/mijoz/buyurtma/
    # operator) allaqachon city_id orqali ishlaydi, ya'ni yangi shahar
    # avtomatik ravishda "to'liq ishlaydigan" bo'lim bo'ladi — operator
    # shu shaharga biriktirilsa, faqat shu shaharni ko'radi, boshqa
    # hech narsa alohida sozlashning hojati yo'q.
    db.add(City(name=name))
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@cities_router.post("/{city_id}/delete")
async def delete_city(city_id: int, db: AsyncSession = Depends(get_db)):
    city_result = await db.execute(select(City).where(City.id == city_id))
    city = city_result.scalars().first()
    if not city:
        raise HTTPException(status_code=404, detail="Shahar topilmadi")

    # Xavfsizlik: agar bu shaharda hali ham do'kon, kuryer yoki operator
    # bo'lsa — o'chirishga ruxsat bermaymiz, aks holda ular "osilib qolgan"
    # (shahri yo'q) holatga tushib qolardi.
    partner_count = (await db.execute(
        select(func.count(PartnerProfile.id)).where(PartnerProfile.city_id == city_id)
    )).scalar()
    people_count = (await db.execute(
        select(func.count(User.id)).where(User.city_id == city_id)
    )).scalar()

    if partner_count or people_count:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Bu shaharda hali {partner_count} ta do'kon va {people_count} ta "
                f"foydalanuvchi (kuryer/operator/mijoz) bor — avval ularni boshqa "
                f"shaharga o'tkazing yoki o'chiring, keyin shaharni o'chiring."
            ),
        )

    await db.delete(city)
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# Barcha routerlarni ilovaga ulash
app.include_router(admin_router)
app.include_router(settings_router)
app.include_router(orders_router)
app.include_router(partners_router)
app.include_router(products_router)
app.include_router(couriers_router)
app.include_router(clients_router)
app.include_router(operators_router)
app.include_router(cities_router)
# ==================== 10. TELEGRAM BOT ====================
# Botda ko'p bosqichli suhbat (masalan "PIN kutilmoqda") uchun vaqtinchalik
# xotira. DIQQAT: bu — oddiy Python lug'ati, ya'ni faqat BITTA server
# jarayoni (process) ishlayotganda to'g'ri ishlaydi (Render'ning bepul/
# standart web-service rejimi shunday). Agar kelajakda bir nechta
# jarayonda (masalan bir nechta "worker") ishga tushirilsa, bu holatni
# Redis kabi umumiy xotiraga ko'chirish kerak bo'ladi.
bot_conversation_state: dict = {}

DEFAULT_COURIER_TERMS = "Kuryer sifatida ishlash shartlari hali kiritilmagan. Iltimos, adminstratsiya bilan bog'laning."
DEFAULT_PARTNER_TERMS = "Hamkorlik shartlari hali kiritilmagan. Iltimos, adminstratsiya bilan bog'laning."


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Telegram har bir yangi xabar/harakat haqida shu manzilga POST
    so'rov yuboradi. Bu yerda: /start, telefon ulashish (contact), tugma
    bosish (callback_query) va PIN kiritish (oddiy matn) qayta ishlanadi."""
    update = await request.json()

    # DIQQAT (VAQTINCHALIK DEBUG): Telegram'dan har bir kelgan update'ni
    # to'liq ko'rish uchun. Muammo tuzatilgach, buni olib tashlash mumkin —
    # hozircha "tugma bosilganda hech narsa bo'lmayapti" degan xatoni
    # aniqlashtirish uchun eng ishonchli yo'l shu.
    print(f"[TELEGRAM UPDATE KELDI] {update}")

    # ---- TUGMA BOSILGANDA (masalan "Kuryer bo'lish", "Roziman") ----
    callback_query = update.get("callback_query")
    if callback_query:
        await answer_callback_query(callback_query["id"])
        chat_id = callback_query["message"]["chat"]["id"]
        data = callback_query.get("data", "")
        print(f"[TELEGRAM CALLBACK] chat_id={chat_id} data={data!r}")

        try:
            user_result = await db.execute(select(User).where(User.telegram_id == str(chat_id)))
            user = user_result.scalars().first()
            if not user:
                await send_telegram_message(chat_id, "Avval /start bosib, telefon raqamingizni ulashing.")
                return {"ok": True}

            # ---- P2P BUYURTMANI TASDIQLASH / RAD ETISH (operator/OWNER botda) ----
            if data.startswith("p2p_ok:") or data.startswith("p2p_no:"):
                if user.role not in (UserRole.OWNER, UserRole.ADMIN):
                    await answer_callback_query(callback_query["id"], text="Bu amal faqat admin/operator uchun.", show_alert=True)
                    return {"ok": True}
                order_id = int(data.split(":")[1])
                order_result = await db.execute(select(Order).where(Order.id == order_id))
                order = order_result.scalars().first()
                if not order:
                    await answer_callback_query(callback_query["id"], text="Buyurtma topilmadi.", show_alert=True)
                    return {"ok": True}
                if order.payment_verified or order.status == OrderStatus.CANCELLED:
                    await answer_callback_query(callback_query["id"], text="Bu buyurtma allaqachon ko'rib chiqilgan.", show_alert=True)
                    return {"ok": True}

                approved = data.startswith("p2p_ok:")
                await _verify_p2p_order(db, order, approved, user.id)
                result_text = f"✅ Buyurtma #{order.id} TASDIQLANDI — {user.full_name}" if approved else f"❌ Buyurtma #{order.id} RAD ETILDI — {user.full_name}"
                try:
                    await send_telegram_message(chat_id, result_text)
                except Exception:
                    pass
                return {"ok": True}

            # ---- KURYER/HAMKOR: pul yechish — "chekni oldim" tasdig'i ----
            if data.startswith("wd_confirm:"):
                wd_id = int(data.split(":")[1])
                wd_result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == wd_id))
                wd = wd_result.scalars().first()
                belongs_to_me = False
                if wd and wd.user_id and wd.user_id == user.id:
                    belongs_to_me = True
                elif wd and wd.partner_id:
                    own_partner = await db.execute(
                        select(PartnerProfile).where(PartnerProfile.id == wd.partner_id, PartnerProfile.user_id == user.id)
                    )
                    belongs_to_me = own_partner.scalars().first() is not None
                if not wd or not belongs_to_me:
                    await answer_callback_query(callback_query["id"], text="Bu so'rov sizga tegishli emas.", show_alert=True)
                    return {"ok": True}
                if not wd.recipient_confirmed_at:
                    wd.recipient_confirmed_at = datetime.utcnow()
                    await db.commit()
                await send_telegram_message(chat_id, "Rahmat! Tasdiqlandi ✅")
                return {"ok": True}

            if data in ("become:courier", "become:partner"):
                role_key = data.split(":")[1]
                already = False
                if role_key == "courier":
                    existing = await db.execute(select(CourierProfile).where(CourierProfile.user_id == user.id))
                    already = existing.scalars().first() is not None
                else:
                    existing = await db.execute(select(PartnerProfile).where(PartnerProfile.user_id == user.id))
                    already = existing.scalars().first() is not None

                if already:
                    await send_telegram_message(chat_id, "Siz allaqachon shu rolda ro'yxatdan o'tgansiz ✅")
                    return {"ok": True}

                setting = await _get_or_create_setting(db)
                raw_terms = (setting.courier_terms if role_key == "courier" else setting.partner_terms) or (
                    DEFAULT_COURIER_TERMS if role_key == "courier" else DEFAULT_PARTNER_TERMS
                )
                # DIQQAT: xabar HTML formatida (parse_mode=HTML) yuboriladi.
                # Agar OWNER shartlar matniga "<", ">" yoki "&" kabi belgilarni
                # yozib qo'ysa, Telegram buni noto'g'ri HTML deb hisoblab,
                # xabarni BUTUNLAY rad etadi — va foydalanuvchi tugmani
                # bossa ham "hech narsa bo'lmayapti" degan taassurot qoladi
                # (chunki xato faqat server logida ko'rinadi, botda ko'rinmaydi).
                # Shuning uchun matnni albatta xavfsizlashtiramiz (escape qilamiz).
                safe_terms = html.escape(raw_terms)
                label = "Kuryer" if role_key == "courier" else "Hamkor"
                await send_telegram_message(
                    chat_id,
                    f"📄 <b>{label} bo'lish shartlari:</b>\n\n{safe_terms}",
                    reply_markup={"inline_keyboard": [[{"text": "✅ Roziman va davom etaman", "callback_data": f"agree:{role_key}"}]]},
                )
                return {"ok": True}

            if data in ("agree:courier", "agree:partner"):
                role_key = data.split(":")[1]
                bot_conversation_state[chat_id] = {"awaiting_pin_for": role_key}
                await send_telegram_message(
                    chat_id,
                    "🔐 Endi o'zingiz uchun 4 xonali PIN kod o'ylab toping va shu yerga yozing (masalan: 4271).\n"
                    "Bu PIN bilan keyinchalik kompyuter yoki telefondan kabinetingizga kirasiz.",
                )
                return {"ok": True}

            return {"ok": True}
        except Exception as e:
            # DIQQAT: bu yerda xatolikni yutib yubormaymiz — logga to'liq yozamiz
            # VA foydalanuvchiga ham ko'rinadigan alert chiqaramiz, aks holda
            # "tugma bosilgani bilan hech narsa bo'lmayapti" degan holat
            # foydalanuvchi uchun tushunarsiz bo'lib qolaveradi.
            print(f"[TELEGRAM CALLBACK XATOSI] data={data!r} chat_id={chat_id} xato={e!r}")
            try:
                await answer_callback_query(callback_query["id"], text="Xatolik yuz berdi, qaytadan urinib ko'ring.", show_alert=True)
            except Exception:
                pass
            return {"ok": True}

    message = update.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    contact = message.get("contact")

    # DIQQAT: pastdagi butun blok endi try/except ICHIDA — sabab: agar biror
    # kutilmagan xato (masalan bazaga vaqtincha ulanib bo'lmasa) yuz bersa-yu,
    # bu yerda ushlanmasa, FastAPI Telegram'ga 500 qaytaradi. Telegram esa
    # bir nechta ketma-ket 500'dan keyin webhook'ni AVTOMATIK to'xtatib
    # qo'yishi mumkin — shundan keyin "/start bosilganda hech qanday javob
    # kelmaydi" degan holat butunlay, hamma uchun boshlanadi, garchi asl
    # sabab bitta vaqtinchalik xato bo'lgan bo'lsa ham. Shu sababli bu yerda
    # xatoni albatta ushlaymiz, logga yozamiz va baribir Telegram'ga
    # "ok" qaytaramiz — webhook doim tirik qolishi uchun.
    try:
        await _handle_telegram_text_message(chat_id, text, contact, message, request, db)
    except Exception as e:
        print(f"[TELEGRAM XABAR XATOSI] chat_id={chat_id} text={text!r} xato={e!r}")
    return {"ok": True}


async def _handle_telegram_text_message(chat_id, text, contact, message, request, db):
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        start_payload = parts[1].strip() if len(parts) > 1 else None

        bot_conversation_state.pop(chat_id, None)

        # ---- ADMIN/OPERATOR: pul yechish so'rovi uchun CHEK yuborish rejimi ----
        # Admin panelidagi "📤 Chek yuborish" havolasi shu formatda keladi:
        # https://t.me/BOT?start=wd_123
        if start_payload and start_payload.startswith("wd_") and start_payload[3:].isdigit():
            wd_id = int(start_payload[3:])
            admin_user_result = await db.execute(select(User).where(User.telegram_id == str(chat_id)))
            admin_user = admin_user_result.scalars().first()
            if not admin_user or admin_user.role not in (UserRole.OWNER, UserRole.ADMIN):
                await send_telegram_message(chat_id, "Bu havola faqat admin/operator uchun. Avval o'z hisobingiz bilan /start bosing.")
                return {"ok": True}
            wd_result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == wd_id))
            wd = wd_result.scalars().first()
            if not wd or wd.status != WithdrawalStatus.PENDING:
                await send_telegram_message(chat_id, "Bu so'rov topilmadi yoki allaqachon ko'rib chiqilgan.")
                return {"ok": True}
            bot_conversation_state[chat_id] = {"awaiting_receipt_for_withdrawal": wd_id}
            await send_telegram_message(
                chat_id,
                f"💸 Pul yechish so'rovi #{wd.id} — {wd.amount:,.0f} so'm.\n\n"
                f"Pulni kartaga o'tkazgach, TO'LOV CHEKINI (skrinshot) shu yerga rasm qilib yuboring — "
                f"so'rov avtomatik yakunlanadi va qabul qiluvchiga chek yuboriladi.",
            )
            return {"ok": True}

        referral_code_payload = start_payload
        if referral_code_payload:
            # Referal kodini vaqtincha saqlab qo'yamiz — telefon ulashilganda
            # (pastroqda) shu kodga tegishli odamni "taklif qiluvchi" deb yozamiz.
            bot_conversation_state[chat_id] = {"referral_code": referral_code_payload}

        await send_telegram_message(
            chat_id,
            "Assalomu alaykum! 👋 <b>Eltuvchi Express</b> botiga xush kelibsiz.\n\n"
            "Tizimga ulanish uchun quyidagi tugma orqali telefon raqamingizni yuboring:",
            reply_markup=contact_request_keyboard(),
        )
        return {"ok": True}

    # ---- PIN KUTILAYOTGAN BO'LSA (foydalanuvchi oddiy matn yuboryapti) ----
    pending = bot_conversation_state.get(chat_id)
    if pending and "awaiting_pin_for" in pending and text:
        pin = text.strip()
        if not (pin.isdigit() and len(pin) == 4):
            await send_telegram_message(chat_id, "❗ PIN aynan 4 ta raqamdan iborat bo'lishi kerak. Qaytadan urinib ko'ring:")
            return {"ok": True}

        user_result = await db.execute(select(User).where(User.telegram_id == str(chat_id)))
        user = user_result.scalars().first()
        if not user:
            bot_conversation_state.pop(chat_id, None)
            await send_telegram_message(chat_id, "Xatolik yuz berdi — /start bosib qaytadan urinib ko'ring.")
            return {"ok": True}

        role_key = pending["awaiting_pin_for"]
        user.password_hash = hash_password(pin)

        if role_key == "courier":
            db.add(CourierProfile(
                user_id=user.id, transport_type="walking", is_approved=True,
                terms_accepted_at=datetime.utcnow(),
            ))
            await db.commit()
            courier_login_url = get_public_base_url(request) + "/login?next=/courier"
            await send_telegram_message(
                chat_id,
                "🎉 Tabriklaymiz — endi siz kuryersiz!\n\n"
                "Kabinetingizga shu telefon raqamingiz va PIN kodingiz bilan istalgan qurilmadan "
                "(kompyuter yoki telefon) kirishingiz mumkin.\n\n"
                "Sizga qaysi shaharda ishlashingiz OWNER/operator tomonidan tayinlanadi — "
                "agar hali tayinlanmagan bo'lsa, ular bilan bog'laning.",
                reply_markup={"inline_keyboard": [[{"text": "🛵 Kuryer kabinetini ochish", "web_app": {"url": courier_login_url}}]]},
            )
        else:
            # Hamkor (do'kon) bo'lish — bunga do'kon nomi, manzili, shahri
            # kabi ko'p ma'lumot kerak, buni chatda yig'ish noqulay va
            # xatoga moyil. Shuning uchun: PIN va rozilikni saqlaymiz,
            # so'ng OWNER'ga xabar boradi — u admin paneldagi mavjud
            # "Yangi Do'kon Qo'shish" formasida shu telefon raqamni
            # "Kirish uchun telefon" maydoniga yozib, bir necha soniyada
            # to'liq sozlab beradi (parol allaqachon saqlangani uchun
            # qayta kiritishning hojati yo'q).
            await db.commit()

            owner_result = await db.execute(select(User).where(User.role == UserRole.OWNER))
            owner_user = owner_result.scalars().first()
            if owner_user and owner_user.telegram_id:
                await send_telegram_message(
                    owner_user.telegram_id,
                    f"🏪 Yangi hamkorlik so'rovi!\n\n"
                    f"<b>Ism:</b> {user.full_name}\n"
                    f"<b>Telefon:</b> {user.phone_number}\n\n"
                    f"Admin panelda \"Yangi Do'kon Qo'shish\" formasidagi \"Kirish uchun telefon\" "
                    f"maydoniga shu raqamni yozib, do'konni sozlab bering (PIN allaqachon saqlangan).",
                )

            await send_telegram_message(
                chat_id,
                "✅ So'rovingiz qabul qilindi!\n\n"
                "Tez orada administratsiya siz bilan bog'lanib, do'koningizni tizimga to'liq qo'shib beradi.",
            )

        bot_conversation_state.pop(chat_id, None)
        return {"ok": True}

    if contact:
        phone = normalize_phone(contact.get("phone_number", ""))
        # Admin paneldan boshqa formatda ("90 123 45 67") kiritilgan kuryer/hamkor
        # ham topilishi uchun — tolerant qidiruv (oxirgi 9 raqam bo'yicha).
        user = await find_user_by_phone(db, phone)

        shop_url = get_public_base_url(request) + "/shop"
        role_choice_keyboard = {
            "inline_keyboard": [
                [{"text": "🛍 Menyuni ochish (mijoz)", "web_app": {"url": shop_url}}],
                [{"text": "🛵 Kuryer bo'lib ishlash", "callback_data": "become:courier"}],
                [{"text": "🏪 Hamkor bo'lish", "callback_data": "become:partner"}],
            ]
        }

        if user:
            user.telegram_id = str(chat_id)
            user.phone_number = phone  # yagona standart formatga keltiramiz
            await db.commit()
            await send_telegram_message(
                chat_id,
                f"✅ Muvaffaqiyatli ulandingiz, <b>{user.full_name}</b>!\n\nNima qilmoqchisiz?",
                reply_markup=role_choice_keyboard,
            )
        else:
            referred_by_id = None
            pending_ref = bot_conversation_state.pop(chat_id, None)
            if pending_ref and pending_ref.get("referral_code"):
                referrer_result = await db.execute(
                    select(User).where(User.referral_code == pending_ref["referral_code"])
                )
                referrer = referrer_result.scalars().first()
                if referrer:
                    referred_by_id = referrer.id

            new_user = User(
                full_name=contact.get("first_name") or "Mijoz",
                phone_number=phone,
                role=UserRole.CLIENT,
                telegram_id=str(chat_id),
                referred_by_id=referred_by_id,
            )
            db.add(new_user)
            await db.commit()
            await send_telegram_message(
                chat_id,
                "✅ Ro'yxatdan muvaffaqiyatli o'tdingiz!\n\nNima qilmoqchisiz?",
                reply_markup=role_choice_keyboard,
            )
        return {"ok": True}

    return {"ok": True}


@app.get("/telegram/set-webhook")
async def telegram_set_webhook(request: Request, owner: User = Depends(require_owner)):
    """Buni FAQAT BIR MARTA, brauzerda ochish orqali ishga tushirasiz —
    shundan keyin Telegram xabarlarni avtomatik shu serverga yubora boshlaydi.
    Masalan: https://eltuvchi-express.onrender.com/telegram/set-webhook"""
    webhook_url = get_public_base_url(request) + "/telegram/webhook"
    result = await set_telegram_webhook(webhook_url)
    return {"webhook_url": webhook_url, "telegram_response": result}


@app.get("/telegram/webhook-info")
async def telegram_webhook_info(owner: User = Depends(require_owner)):
    """Diagnostika uchun: hozirgi webhook sozlamasi qanday ekanini
    ko'rsatadi — xususan 'allowed_updates' ichida 'callback_query'
    borligini shu yerdan TEKSHIRISH mumkin (taxmin qilmasdan)."""
    return await get_telegram_webhook_info()


# ==================== 11. MIJOZ MINI APP (Telegram WebApp) ====================
# DIQQAT: bu yerdagi barcha endpointlar login/parolsiz — chunki mijoz admin
# emas. Buning o'rniga, HAR BIR so'rovda Telegram'ning initData'si
# tekshiriladi (validate_telegram_init_data) — bu, aslida, "parol" vazifasini
# bajaradi, chunki uni faqat Telegram'ning o'zi to'g'ri yarata oladi.

shop_router = APIRouter(prefix="/api/shop", tags=["Mijoz Mini App"])


@shop_router.get("/promotions")
async def shop_promotions(db: AsyncSession = Depends(get_db)):
    """Mini App tepasidagi banner(lar) va referal/cashback ma'lumotlarini
    qaytaradi — bularning barchasi OWNER tomonidan boshqariladi.
    Frontend (shop.html) shu manzilni chaqirib, mos joylarni chizadi."""
    setting = await _get_or_create_setting(db)
    await db.commit()  # _get_or_create_setting yangi qator yaratgan bo'lishi mumkin

    banners_result = await db.execute(
        select(Banner).where(Banner.is_active == True).order_by(Banner.display_order, Banner.id)
    )
    banners = banners_result.scalars().all()

    return {
        "banners": [
            {
                "id": b.id,
                "title": b.title,
                "text": b.text_content,
                "image_url": f"/media/banner/{b.id}" if b.image_data else None,
                "link_url": b.link_url,
            }
            for b in banners
        ],
        "referral": {
            "visible": setting.referral_visible,
            "text": setting.referral_program_text or "",
        },
        "cashback": {
            "visible": setting.cashback_visible,
            "text": setting.bonus_cashback_text or "",
        },
    }


def _get_telegram_user_or_403(init_data: str) -> dict:
    tg_user = validate_telegram_init_data(init_data)
    if not tg_user:
        raise HTTPException(status_code=403, detail="Telegram tasdiqlanmadi. Iltimos, ilovani Telegram ichidan oching.")
    return tg_user


class InitDataBody(BaseModel):
    init_data: str


class SetCityBody(BaseModel):
    init_data: str
    city_id: int


class SetBirthdayBody(BaseModel):
    init_data: str
    birth_date: str  # "YYYY-MM-DD" ko'rinishida keladi (HTML <input type="date">)


class OrderItemBody(BaseModel):
    product_id: int
    quantity: int


class ShopOrderBody(BaseModel):
    init_data: str
    partner_id: int
    delivery_address: str
    items: List[OrderItemBody]
    comment: Optional[str] = None
    # DIQQAT: shop.html'da qo'shilgan qo'shimcha maydonlar (order_type,
    # payment_method, promo_code, location, use_cashback) — bularni hozircha
    # backend qabul qiladi (xato bermaydi), lekin faol ishlatilmaydi.
    # Sabab: har biri alohida katta ish (masalan Click/Payme integratsiyasi,
    # promo-kod tizimi) — buni alohida, ehtiyotkorlik bilan qilamiz.
    order_type: Optional[str] = None
    payment_method: Optional[str] = None
    promo_code: Optional[str] = None
    location: Optional[dict] = None
    use_cashback: Optional[bool] = None


@shop_router.get("/p2p-card")
async def shop_get_p2p_card(db: AsyncSession = Depends(get_db)):
    """Mijoz checkout'da 'Karta orqali (P2P)' usulini tanlaganda shu
    endpoint chaqiriladi — qaysi kartaga pul o'tkazish kerakligini
    ko'rsatish uchun. DIQQAT: bu yerda raqam ATAYLAB TO'LIQ (maskalanmagan)
    qaytariladi — chunki mijoz AYNAN shu raqamga pul o'tkazishi kerak,
    bu boshqa foydalanuvchilarning shaxsiy kartasi emas, balki
    OWNER'ning ATAYLAB OMMAGA ko'rsatish uchun qo'shgan to'lov kartasi."""
    card = await _get_active_p2p_card(db)
    if not card:
        return {"available": False}

    digits = card_security.decrypt_card_number(card.encrypted_card_number)
    if not digits:
        return {"available": False}

    grouped = " ".join(digits[i:i + 4] for i in range(0, len(digits), 4))
    return {
        "available": True,
        "card_number": grouped,
        "bank_name": card.bank_name,
        "card_holder_name": card.card_holder_name,
    }


@app.get("/health")
async def health_check():
    """Render (yoki tashqi monitoring xizmati) serverning ishlab
    turganini tekshirishi uchun. Hech qanday bazaga ulanmaydi — shunchaki
    'server jarayoni tirik' degan tez javob."""
    return {"status": "ok"}


@app.get("/shop", response_class=HTMLResponse)
async def shop_page(request: Request):
    return templates.TemplateResponse(request=request, name="shop.html", context={})


@shop_router.post("/me")
async def shop_me(body: InitDataBody, db: AsyncSession = Depends(get_db)):
    tg_user = _get_telegram_user_or_403(body.init_data)
    telegram_id = str(tg_user["id"])

    result = await db.execute(
        select(User).where(User.telegram_id == telegram_id, User.role == UserRole.CLIENT)
    )
    client = result.scalars().first()

    if not client:
        # Mijoz hali botga /start bosib, telefon ulashmagan — Mini App
        # ichida ro'yxatdan o'tkazmaymiz (bu botning ishi), shunchaki
        # frontend'ga "avval botga qayting" deb aytamiz.
        return {"registered": False}

    cities_result = await db.execute(select(City).order_by(City.name))
    cities = [{"id": c.id, "name": c.name} for c in cities_result.scalars().all()]

    referral_code = await get_or_create_referral_code(db, client)
    await db.commit()

    bot_username = await get_bot_username()

    return {
        "registered": True,
        "full_name": client.full_name,
        "first_name": client.full_name,
        "phone": client.phone_number,
        "birth_date": client.birth_date.isoformat() if client.birth_date else "",
        "city_id": client.city_id,
        "cities": cities,
        "cashback_balance": client.cashback_balance or 0.0,
        "referral_code": referral_code,
        "bot_username": bot_username,
    }


@shop_router.post("/set-city")
async def shop_set_city(body: SetCityBody, db: AsyncSession = Depends(get_db)):
    tg_user = _get_telegram_user_or_403(body.init_data)
    telegram_id = str(tg_user["id"])

    result = await db.execute(
        select(User).where(User.telegram_id == telegram_id, User.role == UserRole.CLIENT)
    )
    client = result.scalars().first()
    if not client:
        raise HTTPException(status_code=404, detail="Mijoz topilmadi")

    client.city_id = body.city_id
    await db.commit()
    return {"ok": True}


@shop_router.post("/set-birthday")
async def shop_set_birthday(body: SetBirthdayBody, db: AsyncSession = Depends(get_db)):
    """Mijoz Mini App'da tug'ilgan sanasini kiritganda shu yerga keladi.
    Bu — avval umuman mavjud bo'lmagan endpoint edi, shuning uchun mijoz
    kiritgan sana hech qayerga saqlanmasdi (admin panelda ko'rinmasdi)."""
    tg_user = _get_telegram_user_or_403(body.init_data)
    telegram_id = str(tg_user["id"])

    result = await db.execute(
        select(User).where(User.telegram_id == telegram_id, User.role == UserRole.CLIENT)
    )
    client = result.scalars().first()
    if not client:
        raise HTTPException(status_code=404, detail="Mijoz topilmadi")

    try:
        parsed_date = datetime.strptime(body.birth_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Sana formati noto'g'ri (YYYY-MM-DD kutilgan)")

    client.birth_date = parsed_date
    await db.commit()
    return {"ok": True}


class ProfileUpdateBody(BaseModel):
    init_data: str
    first_name: Optional[str] = None
    birth_date: Optional[str] = None  # "YYYY-MM-DD"


@shop_router.post("/profile/update")
async def shop_profile_update(body: ProfileUpdateBody, db: AsyncSession = Depends(get_db)):
    """DIQQAT: shop.html frontend'i aynan shu manzilga (`/profile/update`)
    so'rov yuboradi — avval bu manzil backendda umuman yo'q edi (faqat
    alohida `/set-birthday` bor edi, boshqa nom bilan), shuning uchun
    mijoz ismini yoki tug'ilgan kunini kiritsa ham, hech qayerga
    saqlanmasdi va admin panelda hech qachon ko'rinmasdi."""
    tg_user = _get_telegram_user_or_403(body.init_data)
    telegram_id = str(tg_user["id"])

    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    client = result.scalars().first()
    if not client:
        raise HTTPException(status_code=404, detail="Mijoz topilmadi")

    if body.first_name:
        client.full_name = body.first_name

    if body.birth_date:
        try:
            client.birth_date = datetime.strptime(body.birth_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Sana formati noto'g'ri (YYYY-MM-DD kutilgan)")

    await db.commit()
    return {"ok": True, "message": "Ma'lumotlar saqlandi!"}


@shop_router.get("/orders")
async def shop_order_history(init_data: str, db: AsyncSession = Depends(get_db)):
    """Mijozning o'z buyurtmalari tarixi — Shaxsiy Kabinet bo'limida ko'rsatish uchun."""
    tg_user = _get_telegram_user_or_403(init_data)
    telegram_id = str(tg_user["id"])

    client_result = await db.execute(
        select(User).where(User.telegram_id == telegram_id, User.role == UserRole.CLIENT)
    )
    client = client_result.scalars().first()
    if not client:
        return []

    orders_result = await db.execute(
        select(Order)
        .options(selectinload(Order.partner), selectinload(Order.courier))
        .where(Order.client_id == client.id)
        .order_by(Order.created_at.desc())
        .limit(30)
    )
    orders = orders_result.scalars().all()

    # DIQQAT: frontend (shop.html) statuslarni o'zining inglizcha
    # nomlari bilan kutadi ("pending", "accepted", "on_the_way",
    # "delivered", "canceled") — bazamizdagi haqiqiy statuslardan
    # (masalan "created", "cancelled") bu ko'rinishga moslashtiramiz.
    status_map = {
        "created": "pending",
        "accepted_by_partner": "accepted",
        "preparing": "accepted",
        "looking_for_courier": "accepted",
        "on_the_way": "on_the_way",
        "delivered": "delivered",
        "cancelled": "canceled",
    }

    return [
        {
            "id": o.id,
            "status": status_map.get(o.status.value, o.status.value),
            "partner_name": o.partner.brand_name if o.partner else None,
            "total_amount": o.total_price + o.delivery_fee - (o.discount_amount or 0),
            "courier_name": o.courier.full_name if o.courier else None,
        }
        for o in orders
    ]


@shop_router.get("/orders/{order_id}/track")
async def shop_order_track(order_id: int, init_data: str, db: AsyncSession = Depends(get_db)):
    """Mijoz 'Yo'lda' bo'lgan buyurtmasini JONLI kuzatishi uchun — kuryerning
    hozirgi GPS koordinatasini qaytaradi. Frontend (shop.html) bu manzilga
    har 5-8 sekundda so'rov yuborib, xaritadagi kuryer belgisini yangilab
    turadi. Buyurtma boshqa mijozniki bo'lsa ko'rsatilmaydi."""
    tg_user = _get_telegram_user_or_403(init_data)
    telegram_id = str(tg_user["id"])

    client_result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    client = client_result.scalars().first()
    if not client:
        raise HTTPException(status_code=404, detail="Mijoz topilmadi")

    order_result = await db.execute(
        select(Order)
        .options(selectinload(Order.courier).selectinload(User.courier_profile), selectinload(Order.partner))
        .where(Order.id == order_id, Order.client_id == client.id)
    )
    order = order_result.scalars().first()
    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")

    courier_location = None
    if order.status == OrderStatus.ON_THE_WAY and order.courier and order.courier.courier_profile:
        cp = order.courier.courier_profile
        if cp.latitude is not None and cp.longitude is not None:
            courier_location = {
                "lat": cp.latitude,
                "lng": cp.longitude,
                "updated_at": cp.location_updated_at.isoformat() if cp.location_updated_at else None,
            }

    return {
        "id": order.id,
        "status": order.status.value,
        "delivery_address": order.delivery_address,
        "courier_name": order.courier.full_name if order.courier else None,
        "courier_phone": order.courier.phone_number if order.courier else None,
        "courier_transport": (
            order.courier.courier_profile.transport_type
            if order.courier and order.courier.courier_profile else None
        ),
        "partner_name": order.partner.brand_name if order.partner else None,
        "partner_location": (
            {"lat": order.partner.latitude, "lng": order.partner.longitude}
            if order.partner and order.partner.latitude is not None and order.partner.longitude is not None
            else None
        ),
        "courier_location": courier_location,
    }


@shop_router.get("/partners")
async def shop_partners(city_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PartnerProfile).where(PartnerProfile.city_id == city_id, PartnerProfile.is_open == True)
    )
    partners = result.scalars().all()
    return [
        {"id": p.id, "name": p.brand_name, "category": p.category, "address": p.address}
        for p in partners
    ]


@shop_router.get("/products")
async def shop_products(partner_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Product).where(Product.partner_id == partner_id, Product.is_available == True)
    )
    products = result.scalars().all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "price": p.price,
            "description": p.description,
            "category": p.category or "Boshqa",
            "image_url": p.image_url,
        }
        for p in products
    ]


@shop_router.post("/order")
async def shop_create_order(body: ShopOrderBody, db: AsyncSession = Depends(get_db)):
    tg_user = _get_telegram_user_or_403(body.init_data)
    telegram_id = str(tg_user["id"])

    client_result = await db.execute(
        select(User).where(User.telegram_id == telegram_id, User.role == UserRole.CLIENT)
    )
    client = client_result.scalars().first()
    if not client:
        raise HTTPException(status_code=404, detail="Mijoz topilmadi — avval botga /start bosing")

    if not body.items:
        raise HTTPException(status_code=400, detail="Savat bo'sh")

    partner_result = await db.execute(select(PartnerProfile).where(PartnerProfile.id == body.partner_id))
    partner = partner_result.scalars().first()
    if not partner:
        raise HTTPException(status_code=404, detail="Do'kon topilmadi")

    # DIQQAT: bu yerda ham (admin panel'dagi kabi) narxni frontend'dan
    # ISHONIB OLMAYMIZ — bazadagi haqiqiy narxlar bo'yicha o'zimiz hisoblaymiz.
    total_price = 0.0
    order_items_data = []
    for item in body.items:
        if item.quantity <= 0:
            continue
        prod_result = await db.execute(
            select(Product).where(Product.id == item.product_id, Product.partner_id == body.partner_id)
        )
        product = prod_result.scalars().first()
        if not product:
            raise HTTPException(status_code=400, detail=f"Mahsulot topilmadi (id={item.product_id})")
        total_price += product.price * item.quantity
        order_items_data.append((product, item.quantity))

    if not order_items_data:
        raise HTTPException(status_code=400, detail="Kamida bitta mahsulot tanlanishi kerak")

    if partner.min_order_amount and total_price < partner.min_order_amount:
        raise HTTPException(
            status_code=400,
            detail=f"Bu do'konda minimal buyurtma summasi {partner.min_order_amount:.0f} so'm",
        )

    # ---- PROMO-KOD (bazadan HAQIQIY tekshiriladi, frontend'dan ishonib olinmaydi) ----
    discount_amount = 0.0
    promo_code_obj = None
    if body.promo_code:
        promo_result = await db.execute(
            select(PromoCode).where(PromoCode.code == body.promo_code.strip().upper(), PromoCode.is_active == True)
        )
        promo_code_obj = promo_result.scalars().first()
        if promo_code_obj:
            expired = promo_code_obj.expires_at and promo_code_obj.expires_at < datetime.utcnow()
            exhausted = promo_code_obj.max_uses and promo_code_obj.used_count >= promo_code_obj.max_uses
            if expired or exhausted:
                promo_code_obj = None
            else:
                already_used = await db.execute(
                    select(PromoCodeUsage).where(
                        PromoCodeUsage.promo_code_id == promo_code_obj.id,
                        PromoCodeUsage.client_id == client.id,
                    )
                )
                if already_used.scalars().first():
                    promo_code_obj = None  # bitta mijoz bir kodni faqat bir marta ishlatadi

        if promo_code_obj:
            if promo_code_obj.discount_percent:
                discount_amount = total_price * (promo_code_obj.discount_percent / 100)
            elif promo_code_obj.discount_amount:
                discount_amount = min(promo_code_obj.discount_amount, total_price)

    # ---- KESHBEKNI ISHLATISH (mavjud balansdan ko'p ishlatib bo'lmaydi) ----
    cashback_used = 0.0
    if body.use_cashback and client.cashback_balance > 0:
        remaining_after_promo = max(total_price - discount_amount, 0)
        cashback_used = min(client.cashback_balance, remaining_after_promo)

    setting_result = await db.execute(select(SystemSetting))
    setting = setting_result.scalars().first()
    delivery_fee = (setting.base_delivery_fee * setting.weather_multiplier) if setting else 10000.0

    # Mijoz Mini App ichida yuborgan GPS koordinatasi (shop.html'dagi
    # `userLocation` — {lat, lon} shaklida keladi, ba'zi eski frontend
    # variantlarida {lat, lng} ham bo'lishi mumkin — ikkalasini ham qabul
    # qilamiz). Shu koordinata orqali admin/operator/kuryer xaritalarida
    # aynan shu buyurtmaning yetkazish nuqtasi ko'rinadi.
    delivery_lat = None
    delivery_lng = None
    if body.location and isinstance(body.location, dict):
        delivery_lat = body.location.get("lat")
        delivery_lng = body.location.get("lng", body.location.get("lon"))
        try:
            delivery_lat = float(delivery_lat) if delivery_lat is not None else None
            delivery_lng = float(delivery_lng) if delivery_lng is not None else None
        except (TypeError, ValueError):
            delivery_lat, delivery_lng = None, None

    payment_method = body.payment_method or "cash"

    # ---- P2P TO'LOV: avval FAOL karta borligini tekshiramiz ----
    # (bo'lmasa, mijoz pulni qayerga o'tkazishini bilmay qoladi)
    p2p_card = None
    if payment_method == "p2p":
        p2p_card = await _get_active_p2p_card(db)
        if not p2p_card:
            raise HTTPException(
                status_code=400,
                detail="Hozircha P2P (karta orqali) to'lov mavjud emas. Boshqa to'lov usulini tanlang.",
            )

    new_order = Order(
        client_id=client.id,
        partner_id=body.partner_id,
        status=OrderStatus.CREATED,
        total_price=total_price,
        delivery_fee=delivery_fee,
        delivery_address=body.delivery_address,
        delivery_latitude=delivery_lat,
        delivery_longitude=delivery_lng,
        client_comment=body.comment,
        order_type=body.order_type or "delivery",
        payment_method=payment_method,
        # P2P uchun — operator/OWNER botda chekni tasdiqlagunga qadar
        # "tasdiqlanmagan" holatda turadi (hamkorga ko'rinmaydi).
        payment_verified=(payment_method != "p2p"),
        promo_code_id=promo_code_obj.id if promo_code_obj else None,
        discount_amount=discount_amount + cashback_used,
        cashback_used=cashback_used,
    )
    db.add(new_order)
    await db.flush()

    if cashback_used > 0:
        client.cashback_balance -= cashback_used

    if promo_code_obj:
        promo_code_obj.used_count += 1
        db.add(PromoCodeUsage(promo_code_id=promo_code_obj.id, client_id=client.id, order_id=new_order.id))

    for product, qty in order_items_data:
        db.add(OrderItem(
            order_id=new_order.id,
            product_id=product.id,
            product_name=product.name,
            unit_price=product.price,
            quantity=qty,
        ))

    await db.commit()

    try:
        if payment_method == "p2p" and p2p_card:
            # Mijozni botda "chek kutilmoqda" holatiga o'tkazamiz — bot
            # webhook'i (_handle_telegram_text_message) keyingi rasm
            # xabarini aynan shu buyurtmaga tegishli CHEK deb qabul qiladi.
            bot_conversation_state[int(telegram_id)] = {"awaiting_receipt_for_order": new_order.id}
            card_digits = card_security.decrypt_card_number(p2p_card.encrypted_card_number) or ""
            grouped = " ".join(card_digits[i:i + 4] for i in range(0, len(card_digits), 4))
            await send_telegram_message(
                telegram_id,
                f"🧾 Buyurtma #{new_order.id} qabul qilindi — endi to'lov qiling.\n\n"
                f"<b>To'lov summasi:</b> {(total_price + delivery_fee):,.0f} so'm\n"
                f"<b>Karta raqami:</b> <code>{grouped}</code>\n"
                f"<b>Bank:</b> {p2p_card.bank_name}\n"
                f"<b>Karta egasi:</b> {p2p_card.card_holder_name}\n\n"
                f"Pulni o'tkazgach, TO'LOV CHEKINI (skrinshot) shu yerga, "
                f"SHU CHATGA rasm qilib yuboring — operator tekshirib tasdiqlagach, "
                f"buyurtmangiz avtomatik hamkorga yuboriladi.",
            )
        else:
            await send_telegram_message(
                telegram_id,
                f"✅ Buyurtmangiz qabul qilindi!\n\n"
                f"<b>Buyurtma:</b> #{new_order.id}\n"
                f"<b>Do'kon:</b> {partner.brand_name}\n"
                f"<b>Jami:</b> {total_price:,.0f} so'm + yetkazish {delivery_fee:,.0f} so'm",
            )
    except Exception as e:
        print(f"Buyurtma tasdiqlash xabarini yuborishda xatolik: {e}")

    return {"ok": True, "order_id": new_order.id, "payment_method": payment_method}


# ==================== 12. HAMKOR KABINETI (do'kon egasi) ====================
partner_router = APIRouter(prefix="/partner", tags=["Hamkor Kabineti"])


async def _get_own_partner(db: AsyncSession, current_user: User) -> PartnerProfile:
    result = await db.execute(select(PartnerProfile).where(PartnerProfile.user_id == current_user.id))
    partner = result.scalars().first()
    if not partner:
        raise HTTPException(status_code=404, detail="Sizga bog'langan do'kon topilmadi. Admin bilan bog'laning.")
    return partner


@partner_router.get("", response_class=HTMLResponse)
async def partner_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    partner = await _get_own_partner(db, current_user)

    products_result = await db.execute(
        select(Product).where(Product.partner_id == partner.id).order_by(Product.id.desc())
    )
    products = products_result.scalars().all()

    orders_result = await db.execute(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.client), selectinload(Order.courier))
        .where(
            Order.partner_id == partner.id,
            # P2P to'lov tasdiqlanmagan buyurtmalar hamkorga HALI ko'rinmaydi —
            # operator/OWNER botda chekni tasdiqlagach, bu filtr avtomatik
            # "o'tkazadi" (qarang _verify_p2p_order).
            or_(Order.payment_method != "p2p", Order.payment_verified == True),
        )
        .order_by(Order.created_at.desc())
        .limit(200)
    )
    all_orders = orders_result.scalars().all()

    # Bugungi kunni O'zbekiston vaqti bo'yicha aniqlaymiz (server UTC'da
    # ishlaganda, tun yarmidan keyingi soatlarda xato "kecha" chiqib
    # qolmasligi uchun)
    today_uzb = (datetime.utcnow() + UZB_TZ_OFFSET).date()
    today_orders = [o for o in all_orders if (o.created_at + UZB_TZ_OFFSET).date() == today_uzb]
    history_orders = [o for o in all_orders if (o.created_at + UZB_TZ_OFFSET).date() != today_uzb]

    withdrawal_result = await db.execute(
        select(WithdrawalRequest)
        .where(WithdrawalRequest.partner_id == partner.id)
        .order_by(WithdrawalRequest.requested_at.desc())
        .limit(10)
    )
    withdrawal_requests = withdrawal_result.scalars().all()

    cards_result = await db.execute(
        select(Card).where(Card.user_id == current_user.id, Card.is_active == True).order_by(Card.created_at.desc())
    )
    cards = [_card_to_dict(c) for c in cards_result.scalars().all()]
    _, partner_card_limit = await _get_card_limits(db)

    return templates.TemplateResponse(
        request=request,
        name="partner.html",
        context={
            "partner": partner,
            "products": products,
            "today_orders": today_orders,
            "history_orders": history_orders,
            "status_labels": STATUS_LABELS_UZ,
            "current_user": current_user,
            "withdrawal_requests": withdrawal_requests,
            "cards": cards,
            "MAX_CARDS": partner_card_limit,
            "MIN_WITHDRAWAL_AMOUNT": MIN_WITHDRAWAL_AMOUNT,
            "MAX_WITHDRAWAL_AMOUNT": MAX_WITHDRAWAL_AMOUNT,
        },
    )


@partner_router.get("/orders/{order_id}/track")
async def partner_order_track(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    """Hamkor kabinetida 'Yo'lda' bo'lgan buyurtmani jonli kuzatish —
    mijoz tomonidagi /api/shop/orders/{id}/track bilan bir xil mantiq,
    faqat bu yerda buyurtma albatta shu hamkorga tegishli bo'lishi
    tekshiriladi. Hamkor faqat O'ZIGA xizmat qilayotgan kuryerni ko'radi."""
    partner = await _get_own_partner(db, current_user)

    order_result = await db.execute(
        select(Order)
        .options(selectinload(Order.courier).selectinload(User.courier_profile))
        .where(Order.id == order_id, Order.partner_id == partner.id)
    )
    order = order_result.scalars().first()
    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")

    courier_location = None
    courier_transport = None
    if order.status == OrderStatus.ON_THE_WAY and order.courier and order.courier.courier_profile:
        cp = order.courier.courier_profile
        courier_transport = cp.transport_type
        if cp.latitude is not None and cp.longitude is not None:
            courier_location = {
                "lat": cp.latitude,
                "lng": cp.longitude,
                "updated_at": cp.location_updated_at.isoformat() if cp.location_updated_at else None,
            }

    return {
        "id": order.id,
        "status": order.status.value,
        "courier_name": order.courier.full_name if order.courier else None,
        "courier_phone": order.courier.phone_number if order.courier else None,
        "courier_transport": courier_transport,
        "courier_location": courier_location,
        "delivery_location": (
            {"lat": order.delivery_latitude, "lng": order.delivery_longitude}
            if order.delivery_latitude is not None and order.delivery_longitude is not None
            else None
        ),
        "shop_location": (
            {"lat": partner.latitude, "lng": partner.longitude}
            if partner.latitude is not None and partner.longitude is not None
            else None
        ),
    }


@partner_router.get("/cards")
async def partner_list_cards(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    result = await db.execute(
        select(Card).where(Card.user_id == current_user.id, Card.is_active == True).order_by(Card.created_at.desc())
    )
    return {"cards": [_card_to_dict(c) for c in result.scalars().all()]}


@partner_router.post("/cards")
async def partner_add_card(
    card_number: str = Form(...),
    card_holder_name: str = Form(...),
    expire_month: int = Form(...),
    expire_year: int = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    _, partner_limit = await _get_card_limits(db)
    await _add_card_for_user(
        db, current_user.id, card_number, card_holder_name, expire_month, expire_year, max_cards=partner_limit
    )
    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


@partner_router.post("/cards/{card_id}/delete")
async def partner_delete_card(
    card_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    await _delete_card_for_user(db, current_user.id, card_id)
    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


@partner_router.post("/withdraw")
async def partner_request_withdrawal(
    amount: float = Form(...),
    card_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    """Hamkorning 'balance' maydoni — egasi hamkorga qarzdor bo'lgan summa
    (musbat). So'ralgan summa darhol frozen_balance'ga o'tkaziladi — shunda
    (1) hamkor bir vaqtning o'zida ikkita so'rov yuborib, bor-yo'g'idan
    ko'p pul so'rab ketolmaydi (double-spend), (2) admin ro'yxatida "qancha
    pul band qilingani" ko'rinib turadi."""
    if amount < MIN_WITHDRAWAL_AMOUNT:
        raise HTTPException(status_code=400, detail=f"Minimal summa — {MIN_WITHDRAWAL_AMOUNT:,.0f} so'm")
    if amount > MAX_WITHDRAWAL_AMOUNT:
        raise HTTPException(status_code=400, detail=f"Bir martalik maksimal summa — {MAX_WITHDRAWAL_AMOUNT:,.0f} so'm")

    card_result = await db.execute(
        select(Card).where(Card.id == card_id, Card.user_id == current_user.id, Card.is_active == True)
    )
    if not card_result.scalars().first():
        raise HTTPException(status_code=400, detail="Karta topilmadi — avval kartangizni qo'shing")

    # ROW-LEVEL LOCK: shu hamkorning qatori bazada "qulflanadi" — agar
    # xuddi shu lahzada ikkinchi so'rov kelsa (masalan ikki marta tez-tez
    # bosilsa), u shu SELECT yakunlanguncha KUTADI, shunda ikkalasi ham
    # eski (yangilanmagan) balansni ko'rib, limitdan oshirib yubormaydi.
    partner_result = await db.execute(
        select(PartnerProfile).where(PartnerProfile.user_id == current_user.id).with_for_update()
    )
    partner = partner_result.scalars().first()
    if not partner:
        raise HTTPException(status_code=404, detail="Hamkor profili topilmadi")

    available = partner.balance - partner.frozen_balance
    if amount > available:
        raise HTTPException(
            status_code=400,
            detail=f"Yechib olish uchun mavjud mablag' yetarli emas (mavjud: {available:,.0f} so'm)",
        )

    partner.frozen_balance += amount
    db.add(WithdrawalRequest(partner_id=partner.id, card_id=card_id, amount=amount, status=WithdrawalStatus.PENDING))
    await db.commit()
    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


@partner_router.post("/products/create")
async def partner_create_product(
    name: str = Form(...),
    price: float = Form(...),
    description: Optional[str] = Form(None),
    category: str = Form("Boshqa"),
    image: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    partner = await _get_own_partner(db, current_user)
    image_data, image_mime = None, None
    if image and image.filename:
        image_data, image_mime = await process_uploaded_image(image)
    new_product = Product(
        partner_id=partner.id,
        name=name,
        price=price,
        description=description,
        category=category,
        image_data=image_data,
        image_mime=image_mime,
        is_available=True,
    )
    db.add(new_product)
    await db.flush()
    if image_data:
        new_product.image_url = f"/media/product/{new_product.id}"
    await db.commit()
    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


@partner_router.post("/products/{product_id}/update")
async def partner_update_product(
    product_id: int,
    name: str = Form(...),
    price: float = Form(...),
    description: Optional[str] = Form(None),
    category: str = Form("Boshqa"),
    image: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    partner = await _get_own_partner(db, current_user)
    result = await db.execute(select(Product).where(Product.id == product_id, Product.partner_id == partner.id))
    product = result.scalars().first()
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
    product.name = name
    product.price = price
    product.description = description
    product.category = category
    if image and image.filename:
        image_data, image_mime = await process_uploaded_image(image)
        product.image_data = image_data
        product.image_mime = image_mime
        product.image_url = f"/media/product/{product.id}"
    await db.commit()
    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


@partner_router.post("/products/{product_id}/toggle")
async def partner_toggle_product(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    partner = await _get_own_partner(db, current_user)
    result = await db.execute(select(Product).where(Product.id == product_id, Product.partner_id == partner.id))
    product = result.scalars().first()
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
    product.is_available = not product.is_available
    await db.commit()
    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


@partner_router.post("/products/{product_id}/delete")
async def partner_delete_product(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    partner = await _get_own_partner(db, current_user)
    result = await db.execute(select(Product).where(Product.id == product_id, Product.partner_id == partner.id))
    product = result.scalars().first()
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
    await db.delete(product)
    await db.commit()
    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


@partner_router.post("/status")
async def partner_toggle_open(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    """Do'konni 'hozir ochiq / yopiq' deb belgilash — masalan tushlik
    tanaffusida yoki ish kuni tugaganda hamkorning o'zi yopib qo'yishi uchun."""
    partner = await _get_own_partner(db, current_user)
    partner.is_open = not partner.is_open
    await db.commit()
    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


@partner_router.post("/settings/update")
async def partner_update_settings(
    brand_name: str = Form(...),
    address: str = Form(...),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    notification_sound: str = Form("chime1"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    """Hamkorning o'zi do'kon nomi, manzili, xaritadagi joylashuvi va
    bildirishnoma signalini sozlashi uchun. DIQQAT: komissiya foizi,
    ish vaqti, shahar kabi "shartnoma darajasidagi" narsalar bu yerda
    YO'Q — ular faqat OWNER orqali o'zgartiriladi (avvalgi kelishuvga
    ko'ra)."""
    partner = await _get_own_partner(db, current_user)
    partner.brand_name = brand_name
    partner.address = address
    if latitude is not None and longitude is not None:
        partner.latitude = latitude
        partner.longitude = longitude
    if notification_sound in ("chime1", "chime2", "chime3"):
        partner.notification_sound = notification_sound
    await db.commit()
    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


@partner_router.get("/api/new-orders-count")
async def partner_new_orders_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    """Kabinet sahifasi shu manzilni har necha soniyada bir so'rab turadi
    (poll qiladi) — agar 'javob kutayotgan' (CREATED) buyurtmalar soni
    ko'paygan bo'lsa, frontend signal chaladi. Sahifani qayta yuklash
    (reload) shart emas — shu orqali, hamkor forma to'ldirib turgan
    paytda ma'lumot yo'qolib ketmaydi."""
    partner = await _get_own_partner(db, current_user)
    count_result = await db.execute(
        select(func.count(Order.id)).where(
            Order.partner_id == partner.id, Order.status == OrderStatus.CREATED
        )
    )
    return {"count": count_result.scalar() or 0}


@partner_router.post("/orders/{order_id}/status")
async def partner_update_order_status(
    order_id: int,
    new_status: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    partner = await _get_own_partner(db, current_user)
    result = await db.execute(select(Order).where(Order.id == order_id, Order.partner_id == partner.id))
    order = result.scalars().first()
    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")

    # DIQQAT: hamkor faqat "o'z qismidagi" holatlarni o'zgartira oladi —
    # kuryerga bog'liq holatlarni (yo'lda, yetkazildi) faqat admin/operator
    # yoki (kelajakda) kuryerning o'zi o'zgartiradi.
    allowed_statuses = {"accepted_by_partner", "preparing", "looking_for_courier", "cancelled"}
    if new_status not in allowed_statuses:
        raise HTTPException(status_code=403, detail="Bu holatga o'zgartirishga ruxsatingiz yo'q")

    old_status = order.status
    try:
        order.status = OrderStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Noto'g'ri holat")
    await db.commit()

    # Hamkor buyurtmani "Kuryer izlash" holatiga o'tkazsa — avval ENG
    # YAQIN online kuryerga AVTOMATIK biriktirishga harakat qilamiz.
    # Topilmasa, eski usul ishlayveradi: buyurtma "Yangi buyurtmalar"
    # ro'yxatida qolib, kuryerlar o'zi qo'lda qabul qiladi.
    auto_assigned_courier = None
    if order.status == OrderStatus.LOOKING_FOR_COURIER:
        try:
            auto_assigned_courier = await auto_assign_nearest_courier(db, order)
        except Exception as e:
            print(f"Avto-belgilashda xatolik: {e}")

    try:
        if order.status != old_status:
            client_result = await db.execute(select(User).where(User.id == order.client_id))
            client = client_result.scalars().first()
            if client and client.telegram_id:
                label = STATUS_LABELS_UZ.get(order.status.value, order.status.value)
                await send_telegram_message(client.telegram_id, f"📦 Buyurtma #{order.id} holati yangilandi:\n<b>{label}</b>")

        if auto_assigned_courier and auto_assigned_courier.telegram_id:
            await send_telegram_message(
                auto_assigned_courier.telegram_id,
                f"🛵 Sizga yangi buyurtma <b>avtomatik biriktirildi</b> — #{order.id}\n"
                f"📍 {order.delivery_address}\nKuryer kabinetini oching va ko'ring.",
            )
    except Exception as e:
        print(f"Bildirishnoma yuborishda xatolik: {e}")

    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


# ==================== 13. KURYER KABINETI ====================
courier_router_app = APIRouter(prefix="/courier", tags=["Kuryer Kabineti"])


@courier_router_app.get("", response_class=HTMLResponse)
async def courier_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    profile_result = await db.execute(
        select(CourierProfile).where(CourierProfile.user_id == current_user.id)
    )
    courier_profile = profile_result.scalars().first()

    # Bo'sh (hali hech kim olmagan) buyurtmalar — FAQAT o'z shahridagilar
    available_stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.partner))
        .where(Order.status == OrderStatus.LOOKING_FOR_COURIER, Order.courier_id.is_(None))
    )
    if current_user.city_id is not None:
        available_stmt = available_stmt.join(PartnerProfile, Order.partner_id == PartnerProfile.id).where(
            PartnerProfile.city_id == current_user.city_id
        )
    available_orders = (await db.execute(available_stmt.order_by(Order.created_at))).scalars().all()

    # O'zi hozir yetkazib yurgan buyurtma(lar)
    my_active_stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.partner))
        .where(Order.courier_id == current_user.id, Order.status == OrderStatus.ON_THE_WAY)
        .order_by(Order.created_at)
    )
    my_active_orders = (await db.execute(my_active_stmt)).scalars().all()

    # O'zi yetkazib bergan so'nggi buyurtmalar (tarix)
    history_stmt = (
        select(Order)
        .where(Order.courier_id == current_user.id, Order.status == OrderStatus.DELIVERED)
        .order_by(Order.created_at.desc())
        .limit(20)
    )
    history_orders = (await db.execute(history_stmt)).scalars().all()

    withdrawal_result = await db.execute(
        select(WithdrawalRequest)
        .where(WithdrawalRequest.user_id == current_user.id)
        .order_by(WithdrawalRequest.requested_at.desc())
        .limit(10)
    )
    withdrawal_requests = withdrawal_result.scalars().all()

    cards_result = await db.execute(
        select(Card).where(Card.user_id == current_user.id, Card.is_active == True).order_by(Card.created_at.desc())
    )
    cards = [_card_to_dict(c) for c in cards_result.scalars().all()]
    courier_card_limit, _ = await _get_card_limits(db)

    return templates.TemplateResponse(
        request=request,
        name="courier.html",
        context={
            "current_user": current_user,
            "courier_profile": courier_profile,
            "available_orders": available_orders,
            "my_active_orders": my_active_orders,
            "history_orders": history_orders,
            "withdrawal_requests": withdrawal_requests,
            "cards": cards,
            "MAX_CARDS": courier_card_limit,
            "MIN_WITHDRAWAL_AMOUNT": MIN_WITHDRAWAL_AMOUNT,
            "MAX_WITHDRAWAL_AMOUNT": MAX_WITHDRAWAL_AMOUNT,
            "COURIER_SOUND_OPTIONS": COURIER_SOUND_OPTIONS,
        },
    )


@courier_router_app.post("/toggle-online")
async def courier_toggle_online(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    """Kuryer 'ish boshladim / tugatdim' deb belgilaydi. FAQAT online bo'lgan
    kuryerlar: (1) yangi buyurtmalarga avtomatik biriktiriladi, (2) admin
    xaritasida ko'rinadi."""
    result = await db.execute(select(CourierProfile).where(CourierProfile.user_id == current_user.id))
    profile = result.scalars().first()
    if not profile:
        raise HTTPException(status_code=404, detail="Kuryer profili topilmadi")
    profile.is_online = not profile.is_online
    await db.commit()
    return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)


@courier_router_app.post("/location")
async def courier_update_location(
    lat: float = Form(...),
    lng: float = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    """Brauzerning Geolocation API'sidan muntazam chaqiriladigan 'jonli
    joylashuv' manzili — mijoz/admin xaritasi va auto-assign masofa
    hisob-kitobi aynan shu yerdan oziqlanadi."""
    result = await db.execute(select(CourierProfile).where(CourierProfile.user_id == current_user.id))
    profile = result.scalars().first()
    if not profile:
        raise HTTPException(status_code=404, detail="Kuryer profili topilmadi")
    profile.latitude = lat
    profile.longitude = lng
    profile.location_updated_at = datetime.utcnow()
    await db.commit()
    return JSONResponse({"ok": True})


@courier_router_app.post("/settings/sound")
async def courier_update_sound(
    notification_sound: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    """Yangi buyurtma kelganda chalinadigan signal ovozini tanlash (3 xil)."""
    if notification_sound not in COURIER_SOUND_OPTIONS:
        raise HTTPException(status_code=400, detail="Noto'g'ri ovoz varianti")
    result = await db.execute(select(CourierProfile).where(CourierProfile.user_id == current_user.id))
    profile = result.scalars().first()
    if not profile:
        raise HTTPException(status_code=404, detail="Kuryer profili topilmadi")
    profile.notification_sound = notification_sound
    await db.commit()
    return JSONResponse({"ok": True, "notification_sound": notification_sound})


@courier_router_app.get("/orders/available.json")
async def courier_available_orders_json(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    """Kuryer kabineti sahifasi bir necha sekundda bir marta shu yerga
    so'rov yuborib turadi (poll qiladi) — yangi buyurtma ID'si oldingi
    ro'yxatda bo'lmasa, frontend tanlangan signal ovozini chaladi."""
    available_stmt = select(Order.id).where(
        Order.status == OrderStatus.LOOKING_FOR_COURIER, Order.courier_id.is_(None)
    )
    if current_user.city_id is not None:
        available_stmt = available_stmt.join(PartnerProfile, Order.partner_id == PartnerProfile.id).where(
            PartnerProfile.city_id == current_user.city_id
        )
    ids = [row[0] for row in (await db.execute(available_stmt)).all()]

    my_active_stmt = select(Order.id).where(
        Order.courier_id == current_user.id, Order.status == OrderStatus.ON_THE_WAY
    )
    my_ids = [row[0] for row in (await db.execute(my_active_stmt)).all()]

    return JSONResponse({"available_ids": ids, "my_active_ids": my_ids})


@courier_router_app.get("/cards")
async def courier_list_cards(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    result = await db.execute(
        select(Card).where(Card.user_id == current_user.id, Card.is_active == True).order_by(Card.created_at.desc())
    )
    return {"cards": [_card_to_dict(c) for c in result.scalars().all()]}


@courier_router_app.post("/cards")
async def courier_add_card(
    card_number: str = Form(...),
    card_holder_name: str = Form(...),
    expire_month: int = Form(...),
    expire_year: int = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    courier_limit, _ = await _get_card_limits(db)
    await _add_card_for_user(
        db, current_user.id, card_number, card_holder_name, expire_month, expire_year, max_cards=courier_limit
    )
    return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)


@courier_router_app.post("/cards/{card_id}/delete")
async def courier_delete_card(
    card_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    await _delete_card_for_user(db, current_user.id, card_id)
    return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)


@courier_router_app.post("/withdraw")
async def courier_request_withdrawal(
    amount: float = Form(...),
    card_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    """Kuryerning 'balance' maydoni — kuryerning NAQD QARZI (musbat =
    egasiga qarzdor). Pul yechish FAQAT balance MANFIY bo'lganda mumkin
    (ya'ni egasi kuryerga qarzdor — masalan bonus/haq). So'ralgan summa
    darhol frozen_balance'ga o'tkaziladi (double-spend himoyasi)."""
    if amount < MIN_WITHDRAWAL_AMOUNT:
        raise HTTPException(status_code=400, detail=f"Minimal summa — {MIN_WITHDRAWAL_AMOUNT:,.0f} so'm")
    if amount > MAX_WITHDRAWAL_AMOUNT:
        raise HTTPException(status_code=400, detail=f"Bir martalik maksimal summa — {MAX_WITHDRAWAL_AMOUNT:,.0f} so'm")

    card_result = await db.execute(
        select(Card).where(Card.id == card_id, Card.user_id == current_user.id, Card.is_active == True)
    )
    if not card_result.scalars().first():
        raise HTTPException(status_code=400, detail="Karta topilmadi — avval kartangizni qo'shing")

    # ROW-LEVEL LOCK — qarang partner_request_withdrawal'dagi izoh.
    courier_result = await db.execute(
        select(CourierProfile).where(CourierProfile.user_id == current_user.id).with_for_update()
    )
    courier_profile = courier_result.scalars().first()
    if not courier_profile:
        raise HTTPException(status_code=404, detail="Kuryer profili topilmadi")

    available = -courier_profile.balance - courier_profile.frozen_balance
    if available <= 0:
        raise HTTPException(status_code=400, detail="Hozircha yechib olish uchun mavjud mablag' yo'q")
    if amount > available:
        raise HTTPException(
            status_code=400,
            detail=f"Yechib olish uchun mavjud mablag' yetarli emas (mavjud: {available:,.0f} so'm)",
        )

    courier_profile.frozen_balance += amount
    db.add(WithdrawalRequest(user_id=current_user.id, card_id=card_id, amount=amount, status=WithdrawalStatus.PENDING))
    await db.commit()
    return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)


@courier_router_app.post("/orders/{order_id}/accept")
async def courier_accept_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    courier_profile_result = await db.execute(
        select(CourierProfile).where(CourierProfile.user_id == current_user.id)
    )
    courier_profile = courier_profile_result.scalars().first()
    if courier_profile and courier_profile.is_blocked:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Sizda {courier_profile.balance:,.0f} so'm naqd pul qarzi bor — bu ruxsat etilgan "
                f"chegaradan ({courier_profile.credit_limit:,.0f} so'm) oshib ketgan. Yangi buyurtma "
                f"qabul qilishdan oldin, yig'gan naqd pulingizni egasiga topshiring."
            ),
        )

    order_result = await db.execute(
        select(Order).where(Order.id == order_id, Order.status == OrderStatus.LOOKING_FOR_COURIER)
    )
    order = order_result.scalars().first()
    if not order:
        raise HTTPException(status_code=404, detail="Bu buyurtma allaqachon boshqa kuryer tomonidan olingan")

    if order.courier_id is not None:
        raise HTTPException(status_code=400, detail="Bu buyurtmani allaqachon boshqa kuryer oldi")

    order.courier_id = current_user.id
    order.status = OrderStatus.ON_THE_WAY
    await db.commit()

    try:
        client_result = await db.execute(select(User).where(User.id == order.client_id))
        client = client_result.scalars().first()
        if client and client.telegram_id:
            await send_telegram_message(
                client.telegram_id,
                f"🛵 Buyurtma #{order.id} kuryerga topshirildi — <b>Yo'lda</b>!",
            )
    except Exception as e:
        print(f"Bildirishnoma xatoligi: {e}")

    return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)


@courier_router_app.post("/orders/{order_id}/delivered")
async def courier_mark_delivered(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    order_result = await db.execute(
        select(Order)
        .options(selectinload(Order.partner))
        .where(Order.id == order_id, Order.courier_id == current_user.id)
    )
    order = order_result.scalars().first()
    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")

    if order.status == OrderStatus.DELIVERED:
        return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)

    order.status = OrderStatus.DELIVERED
    await apply_cod_delivery_financials(db, order)
    await db.commit()

    try:
        client_result = await db.execute(select(User).where(User.id == order.client_id))
        client = client_result.scalars().first()
        if client and client.telegram_id:
            await send_telegram_message(
                client.telegram_id, 
                f"✅ Buyurtma #{order.id} — <b>Yetkazildi</b>! Xaridingiz uchun rahmat."
            )
    except Exception as e:
        print(f"Bildirishnoma xatoligi: {e}")

    return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)

# Routerlarni ulash
app.include_router(courier_router_app)
app.include_router(partner_router)
app.include_router(shop_router)
app.include_router(finance_router)

@app.get("/")
async def root():
    return {"status": "ok", "message": "Eltuvchi Express API is running"}