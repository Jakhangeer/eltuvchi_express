import re
import json
import os
import html
import uuid
import math
import secrets
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
from sqlalchemy import select, func, text, extract, or_, and_, delete, inspect as sa_inspect
from sqlalchemy.schema import CreateColumn
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

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
    CityRatingBonus,
    OrderRating,
    SupportMessage,
    Announcement,
    AnnouncementAck,
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
    BonusRule,
    BannerRequest,
    FavoriteProduct,
    Card,
    OperatorPermission,
    PaymentReceipt,
    ParcelOrder,
    ExpressDriver, ExpressFare, Terminal, TerminalOperator, LinehaulTrip, IntercityOrder, IntercityEvent, TerminalCashLog, MagRoute, MagQueueEntry, MagBooking,
    LedAd,
    AIMessage,
    AIMessageLog,
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
    RedirectToGate,
    is_scoped_to_city,
)
from sec2fa import sec_router, secure_action
from pg_enum_sync import sync_pg_enums

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
              with sync_conn.begin_nested():
                sync_conn.execute(text(f'ALTER TABLE "{table.name}" ALTER COLUMN "{col["name"]}" DROP NOT NULL'))
              print(f"[AUTO-MIGRATE] NOT NULL olib tashlandi (eski ustun): {table.name}.{col['name']}")
            except Exception as e:
                print(f"[AUTO-MIGRATE OGOHLANTIRISH] {table.name}.{col['name']}: {e}")


def _backfill_null_defaults(sync_conn):
    """SABAB: auto_sync_missing_columns yangi ustunni DEFAULT'SIZ qo'shadi, shuning
    uchun jadvalda ALLAQACHON mavjud qatorlarda bu ustun NULL bo'lib qoladi
    (SQLAlchemy'dagi `default=...` faqat YANGI yozuvlarga qo'llanadi). Natijada
    eski kuryerlarda credit_limit/frozen_balance/is_blocked = None bo'lib,
    `"%.0f"|format(None)` yoki `balance > None` kabi joylar "TypeError: must be
    real number, not NoneType" bilan yiqilardi (admin panel ochilmay qolardi).

    Bu funksiya HAR safar ishga tushganda modeldagi oddiy (son/mantiqiy) standart
    qiymatlarni NULL turgan qatorlarga yozib chiqadi. Idempotent va xavfsiz:
    faqat NULL qatorlarga tegadi, mavjud qiymatlarni o'zgartirmaydi."""
    from sqlalchemy import Float as _Float, Integer as _Integer, Boolean as _Boolean
    inspector = sa_inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        db_columns = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.primary_key or column.name not in db_columns:
                continue
            default = column.default
            if default is None or not getattr(default, "is_scalar", False):
                continue
            if not isinstance(column.type, (_Float, _Integer, _Boolean)):
                continue
            try:
                with sync_conn.begin_nested():  # savepoint — bitta xato qolganlarni buzmasin
                    result = sync_conn.execute(
                        text(f'UPDATE "{table.name}" SET "{column.name}" = :v WHERE "{column.name}" IS NULL'),
                        {"v": default.arg},
                    )
                    if result.rowcount:
                        print(f"[AUTO-MIGRATE] NULL to'ldirildi: {table.name}.{column.name} = {default.arg!r} ({result.rowcount} qator)")
            except Exception as e:
                print(f"[AUTO-MIGRATE OGOHLANTIRISH] backfill {table.name}.{column.name}: {e}")


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
                    with sync_conn.begin_nested():  # savepoint: bitta xato butun tranzaksiyani buzmasin (Postgres)
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
    await conn.run_sync(_backfill_null_defaults)


async def _migrate_courier_wallet_v2():
    """BIR MARTALIK: eski modelda kuryer `balance` = QARZ (musbat = kuryer
    egasiga qarzdor edi). Yangi modelda `balance` = kuryerning OLDINDAN
    TO'LDIRILGAN mablag'i (musbat = pul bor). Shuning uchun birinchi ishga
    tushishda hamma kuryer balansining ishorasi teskarilanadi va
    SystemSetting.courier_wallet_v2 = True qo'yiladi (qayta aylanmaydi).
    Qarz tufayli avtomatik bloklangan kuryerlar blokdan chiqariladi."""
    async with AsyncSessionLocal() as session:
        setting = (await session.execute(select(SystemSetting))).scalars().first()
        if not setting:
            setting = SystemSetting()
            session.add(setting)
            await session.flush()
        if setting.courier_wallet_v2:
            return
        profiles = (await session.execute(select(CourierProfile))).scalars().all()
        for prof in profiles:
            old = prof.balance or 0.0
            prof.balance = -old if old else 0.0
            if prof.is_blocked and old > (prof.credit_limit or 0):
                prof.is_blocked = False
        setting.courier_wallet_v2 = True
        await session.commit()
        print(f"[WALLET MIGRATSIYA] {len(profiles)} ta kuryer balansi yangi modelga o'tkazildi.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        # 1-qadam: umuman yo'q jadvallarni yaratish (yangi model qo'shilganda)
        await conn.run_sync(Base.metadata.create_all)
        # 2-qadam: mavjud jadvallardagi yetishmayotgan ustunlarni avtomatik qo'shish
        await auto_sync_missing_columns(conn)

    await sync_pg_enums(engine, Base)
    print("PostgreSQL jadvallari tayyor va sinxronlashtirildi.")
    try:
        # Eski yetkazilgan buyurtmalar uchun pul allaqachon hisoblangan — qayta hisoblanmasin
        async with AsyncSessionLocal() as _s:
            await _s.execute(text("UPDATE orders SET financials_applied = TRUE WHERE status = 'DELIVERED' AND financials_applied IS NULL"))
            await _s.execute(text("UPDATE orders SET financials_applied = FALSE WHERE financials_applied IS NULL"))
            await _s.commit()
    except Exception as e:
        print(f"[FINANCIALS BACKFILL XATOSI] {e!r}")
    await seed_default_data()
    try:
        await _migrate_courier_wallet_v2()
    except Exception as e:
        print(f"[WALLET MIGRATSIYA XATOSI] {e!r}")

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
            if isinstance(wh, dict) and wh.get("ok"):
                globals()["_WH_SECRET_ON"] = True
            print(f"[TELEGRAM WEBHOOK] {public_url}/telegram/webhook -> {wh}")
        elif os.getenv("TELEGRAM_BOT_TOKEN"):
            print("[TELEGRAM WEBHOOK] O'TKAZIB YUBORILDI: PUBLIC_BASE_URL (https) topilmadi. "
                  "Render'da RENDER_EXTERNAL_URL avtomatik bo'ladi; boshqa joyda PUBLIC_BASE_URL kiriting "
                  "yoki brauzerda /telegram/set-webhook ni oching.")
        else:
            print("[TELEGRAM WEBHOOK] TELEGRAM_BOT_TOKEN topilmadi — bot ishlamaydi.")
    except Exception as e:
        print(f"[TELEGRAM WEBHOOK XATOSI] {e!r}")
    import asyncio as _asyncio
    _ai_task = _asyncio.create_task(_ai_dispatcher_loop())
    yield
    _ai_task.cancel()
    # Server to'xtatilganda Telegram uchun ochilgan HTTP ulanishlarni yopamiz
    await close_telegram_bot_client()


app = FastAPI(title="ZooMo API", lifespan=lifespan)

# Sessiya (login holatini "eslab qolish") uchun. SESSION_SECRET_KEY albatta
# .env faylida bo'lishi kerak — aks holda server qayta ishga tushganda barcha
# foydalanuvchilar tizimdan chiqarilib yuboriladi, va agar kimdir bu kalitni
# bilib olsa, sessiyani soxtalashtirishi mumkin bo'ladi.
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY")
if not SESSION_SECRET_KEY:
    print("DIQQAT: SESSION_SECRET_KEY .env'da yo'q — barqaror zaxira kalit ishlatilyapti (bazadan hisoblanadi). "
          ".env ga alohida SESSION_SECRET_KEY qo'yish tavsiya etiladi.")
    from stable_keys import derive_hex as _dh
    SESSION_SECRET_KEY = _dh("session")

# DIQQAT: https_only (Secure cookie) ATAYLAB qo'yilmagan — http orqali (lokal tarmoq, proksi ortida) ochilganda
# brauzer cookie'ni rad etib, foydalanuvchini qayta-qayta login oynasiga qaytarar edi.
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY, same_site="lax", max_age=60 * 60 * 24 * 30)


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
async def static_no_stale_cache_middleware(request: Request, call_next):
    """JS/CSS/til fayllari yangilanganda brauzer eski nusxani ushlab qolmasin."""
    response = await call_next(request)
    p = request.url.path
    if p.startswith("/static/") and p.endswith((".js", ".css", ".json")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


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
    try:   # diagnostika: nega login'ga qaytarildi (cookie yo'qmi, yaroqsizmi, foydalanuvchi o'chirilganmi)
        _ck = "session" in (request.cookies or {})
        _uid = bool(request.session.get("user_id"))
        print(f"[LOGIN'GA QAYTARILDI] {request.method} {request.url.path} cookie={'bor' if _ck else 'YOQ'} sessiyada_user={'bor' if _uid else 'YOQ'}")
    except Exception:
        pass
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


@app.exception_handler(RedirectToGate)
async def redirect_to_gate_handler(request: Request, exc: RedirectToGate):
    if request.method == "GET" and "application/json" not in (request.headers.get("accept") or "") \
            and request.headers.get("x-requested-with") is None:
        from urllib.parse import quote
        return RedirectResponse(url="/security/gate?next=" + quote(request.url.path), status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(status_code=428, content={"detail": {"code": "sec_admin", "message": "Operator maxfiy javobini kiriting"}})


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
    "xabarlar": "💬 Chat va e'lonlar (hamkor/kuryer/mijoz xabarlari)",
    "mahsulot_qoshish": "🍕 Do'konga yangi mahsulot qo'shish (o'z shahrida)",
    "banner_sorovlari": "📣 Hamkorlarning banner reklama so'rovlarini ko'rish (o'z shahrida)",
    "pochta": "📮 Pochta (shaxsiy buyum yuborish) — o'z shahri buyurtmalari",
    "taksi": "🚕 Taksi bo'limi",
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
    "xabarlar": True,
    "banner_sorovlari": False,   # standart YOPIQ — egasi ruxsat bergandagina operator ko'radi
    "mahsulot_qoshish": False,   # standart YOPIQ — egasi ruxsat bergandagina operator mahsulot qo'sha oladi
    "pochta": True,              # pochta buyurtmalarini operator o'z shahrida boshqaradi (egasi xohlasa yopadi)
    "taksi": False,
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
    can_partner = user.partner_profile is not None and user.partner_profile.application_status != "pending"
    can_courier = user.courier_profile is not None and user.courier_profile.application_status != "pending"

    can_driver = (await db.execute(select(ExpressDriver.id).where(ExpressDriver.user_id == user.id, ExpressDriver.is_active == True))).first() is not None
    can_terminal = (await db.execute(select(TerminalOperator.id).where(TerminalOperator.user_id == user.id, TerminalOperator.is_active == True))).first() is not None

    if not (can_admin or can_partner or can_courier or can_driver or can_terminal):
        if (user.partner_profile and user.partner_profile.application_status == "pending") or (
            user.courier_profile and user.courier_profile.application_status == "pending"
        ):
            return JSONResponse(status_code=403, content={"ok": False, "detail": "Arizangiz ko'rib chiqilmoqda"})
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
    elif can_driver:
        redirect_to = "/driver"
    elif can_terminal:
        redirect_to = "/terminal"

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
    _lg_phone = normalize_phone(phone_number) or phone_number
    _lg_ip = request.client.host if request.client else "?"
    _lg_keys = [f"lp:{phone_tail(_lg_phone)}", f"li:{_lg_ip}"]
    if any(_rate_limited(k, limit=(8 if k.startswith("lp:") else 100)) for k in _lg_keys):
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Juda ko'p urinish. 15 daqiqadan keyin qayta urinib ko'ring."},
            status_code=429,
        )
    user = await find_user_by_phone(
        db, phone_number,
        options=(selectinload(User.courier_profile), selectinload(User.partner_profile)),
    )

    if not user or not user.is_active or not user.password_hash or not verify_password(password, user.password_hash):
        for _k in _lg_keys:
            _rate_hit(_k)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Telefon raqami yoki parol/PIN noto'g'ri"},
        )

    for _k in _lg_keys:
        _login_attempts.pop(_k, None)
    request.session["user_id"] = user.id

    # Qaysi panellarga kira olishini aniqlaymiz
    can_admin = user.role in (UserRole.OWNER, UserRole.ADMIN)
    can_partner = user.partner_profile is not None and user.partner_profile.application_status != "pending"
    can_courier = user.courier_profile is not None and user.courier_profile.application_status != "pending"
    can_driver = (await db.execute(select(ExpressDriver.id).where(ExpressDriver.user_id == user.id, ExpressDriver.is_active == True))).first() is not None
    can_terminal = (await db.execute(select(TerminalOperator.id).where(TerminalOperator.user_id == user.id, TerminalOperator.is_active == True))).first() is not None
    available = [can_admin, can_partner, can_courier, can_driver, can_terminal].count(True)

    if available == 0 and (
        (user.partner_profile and user.partner_profile.application_status == "pending")
        or (user.courier_profile and user.courier_profile.application_status == "pending")
    ):
        request.session.clear()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Arizangiz hali ko'rib chiqilmoqda — administratsiya tez orada siz bilan bog'lanadi."},
        )

    if available > 1:
        # Bir nechta panelga kira oladi — tanlov sahifasini ko'rsatamiz
        return RedirectResponse(url="/choose-panel", status_code=status.HTTP_303_SEE_OTHER)
    if can_admin:
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    if can_partner:
        return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)
    if can_courier:
        return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)
    if can_driver:
        return RedirectResponse(url="/driver", status_code=status.HTTP_303_SEE_OTHER)
    if can_terminal:
        return RedirectResponse(url="/terminal", status_code=status.HTTP_303_SEE_OTHER)

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
    if (await db.execute(select(ExpressDriver.id).where(ExpressDriver.user_id == user.id, ExpressDriver.is_active == True))).first():
        panels.append({"url": "/driver", "label": "🚗 Magistral haydovchi"})
    if (await db.execute(select(TerminalOperator.id).where(TerminalOperator.user_id == user.id, TerminalOperator.is_active == True))).first():
        panels.append({"url": "/terminal", "label": "🏢 Terminal operatori"})

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
    partners_stmt = select(PartnerProfile).options(selectinload(PartnerProfile.city)).where(
        or_(PartnerProfile.application_status.is_(None), PartnerProfile.application_status != "pending")
    )
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

    # ---- YANGI ARIZALAR (ilova orqali ro'yxatdan o'tgan hamkor/kuryerlar) ----
    app_partner_stmt = select(PartnerProfile).options(
        selectinload(PartnerProfile.user), selectinload(PartnerProfile.city)
    ).where(PartnerProfile.application_status == "pending").order_by(PartnerProfile.id)
    app_courier_stmt = select(User).join(CourierProfile, CourierProfile.user_id == User.id).options(
        selectinload(User.courier_profile), selectinload(User.city)
    ).where(CourierProfile.application_status == "pending").order_by(User.id)
    if active_city_id is not None:
        app_partner_stmt = app_partner_stmt.where(PartnerProfile.city_id == active_city_id)
        app_courier_stmt = app_courier_stmt.where(User.city_id == active_city_id)
    pending_partner_apps = (await db.execute(app_partner_stmt)).scalars().all()
    pending_courier_apps = (await db.execute(app_courier_stmt)).scalars().all()

    # ---- CHEKLAR (kuryer/hamkor/mijoz → admin) ----
    pending_receipts = []
    recent_receipts = []
    receipt_by_order = {}
    if can_see_finance:
        rcp_opts = (selectinload(PaymentReceipt.user), selectinload(PaymentReceipt.partner))
        pending_receipts = [_receipt_to_dict(r) for r in (await db.execute(
            select(PaymentReceipt).options(*rcp_opts).where(PaymentReceipt.status == "pending")
            .order_by(PaymentReceipt.created_at)
        )).scalars().all()]
        recent_receipts = [_receipt_to_dict(r) for r in (await db.execute(
            select(PaymentReceipt).options(*rcp_opts).where(PaymentReceipt.status != "pending")
            .order_by(PaymentReceipt.reviewed_at.desc()).limit(20)
        )).scalars().all()]
        for rr in pending_receipts:
            if rr["order_id"]:
                receipt_by_order[rr["order_id"]] = rr["id"]

    # ---- ANALITIKA (faqat OWNER) ----
    # DIQQAT: komissiya foizi har bir do'kon uchun boshqacha bo'lishi mumkin
    # (partner.commission_rate), shuning uchun buni SQL darajasida bitta
    # formula bilan hisoblab bo'lmaydi — har bir buyurtmani alohida ko'rib,
    # o'sha buyurtmaning o'z hamkoriga tegishli foizi bilan hisoblaymiz.
    _bq = select(Banner).order_by(Banner.display_order, Banner.id)
    if is_scoped_to_city(current_user):
        _bq = _bq.where(or_(Banner.city_id.is_(None), Banner.city_id == current_user.city_id))
    banners = (await db.execute(_bq)).scalars().all()

    _pq = select(PromoCode).order_by(PromoCode.id.desc())
    _rq = select(BonusRule).order_by(BonusRule.id.desc())
    if is_scoped_to_city(current_user):
        _pq = _pq.where(PromoCode.city_id == current_user.city_id)
        _rq = _rq.where(BonusRule.city_id == current_user.city_id)
    promo_codes = (await db.execute(_pq)).scalars().all()
    bonus_rules = (await db.execute(_rq)).scalars().all()

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
                c_pay = _courier_earning(o, courier_pct)
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
            "pending_receipts": pending_receipts,
            "pending_partner_apps": pending_partner_apps,
            "pending_courier_apps": pending_courier_apps,
            "recent_receipts": recent_receipts,
            "receipt_by_order": receipt_by_order,
            "birthday_clients_today": birthday_clients_today,
            "banners": banners,
            "now_utc": datetime.utcnow(),
            "promo_codes": promo_codes,
            "bonus_rules": bonus_rules,
            "current_city_scope": current_user.city_id if is_scoped_to_city(current_user) else None,
            "analytics": analytics,
            "operator_perms": operator_perms,
            "section_labels": SECTION_LABELS_UZ,
            "ai_help_on": _ai_flags(await _get_or_create_setting(db))["help"],
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
    base_fee: float = Form(..., allow_inf_nan=False),
    weather_condition: str = Form(...),
    weather_multiplier: float = Form(..., allow_inf_nan=False),
    service_commission_percent: float = Form(..., allow_inf_nan=False),
    courier_share_percent: float = Form(..., allow_inf_nan=False),
    max_cards_per_courier: int = Form(3),
    max_cards_per_partner: int = Form(3),
    km_form: Optional[str] = Form(None),
    km_pricing_enabled: Optional[str] = Form(None),
    km_base_radius: Optional[float] = Form(None, allow_inf_nan=False),
    km_base_price: Optional[float] = Form(None, allow_inf_nan=False),
    km_extra_price: Optional[float] = Form(None, allow_inf_nan=False),
    km_road_factor: Optional[float] = Form(None, allow_inf_nan=False),
    courier_delivery_per_km: Optional[float] = Form(None, allow_inf_nan=False),
    courier_pickup_per_km: Optional[float] = Form(None, allow_inf_nan=False),
    ai_form: Optional[str] = Form(None),
    rp_form: Optional[str] = Form(None),
    rating_priority_enabled: Optional[str] = Form(None),
    ai_enabled: Optional[str] = Form(None),
    ai_client_enabled: Optional[str] = Form(None),
    ai_courier_enabled: Optional[str] = Form(None),
    ai_partner_enabled: Optional[str] = Form(None),
    ai_dispatcher_enabled: Optional[str] = Form(None),
    ai_led_enabled: Optional[str] = Form(None),
    ai_help_enabled: Optional[str] = Form(None),
    ai_campaigns_enabled: Optional[str] = Form(None),
    ai_delay_promo_amount: Optional[float] = Form(None, allow_inf_nan=False),
    ai_delay_promo_enabled: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    try:
        weather_enum = WeatherCondition(weather_condition)
    except ValueError:
        raise HTTPException(status_code=400, detail="Noto'g'ri ob-havo qiymati")

    if not (0 <= courier_share_percent <= 100):
        raise HTTPException(status_code=400, detail="Kuryer ulushi 0-100 oralig'ida bo'lishi kerak")
    if not (0 <= service_commission_percent <= 100):
        raise HTTPException(status_code=400, detail="Xizmat komissiyasi 0-100 oralig'ida bo'lishi kerak")
    if base_fee < 0 or not (0.1 <= weather_multiplier <= 10):
        raise HTTPException(status_code=400, detail="Yetkazish narxi manfiy bo'lmasligi, ob-havo koeffitsienti 0.1–10 oralig'ida bo'lishi kerak")
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

    # ---- Masofa (km) sozlamalari — mavjud sozlamalar yonida YANGI blok ----
    if km_form:
        for label, val in (("Radius", km_base_radius), ("Radius ichidagi narx", km_base_price),
                           ("Har keyingi km narxi", km_extra_price), ("Kuryer: yetkazish km haqi", courier_delivery_per_km),
                           ("Kuryer: olib borish km haqi", courier_pickup_per_km)):
            if val is not None and val < 0:
                raise HTTPException(status_code=400, detail=f"{label} manfiy bo'lishi mumkin emas")
        if km_road_factor is not None and not (1.0 <= km_road_factor <= 3.0):
            raise HTTPException(status_code=400, detail="Yo'l koeffitsienti 1.0 dan 3.0 gacha bo'lishi kerak")
        setting.km_pricing_enabled = bool(km_pricing_enabled)
        if km_base_radius is not None: setting.km_base_radius = km_base_radius
        if km_base_price is not None: setting.km_base_price = km_base_price
        if km_extra_price is not None: setting.km_extra_price = km_extra_price
        if km_road_factor is not None: setting.km_road_factor = km_road_factor
        if courier_delivery_per_km is not None: setting.courier_delivery_per_km = courier_delivery_per_km
        if courier_pickup_per_km is not None: setting.courier_pickup_per_km = courier_pickup_per_km

    if rp_form:
        setting.rating_priority_enabled = bool(rating_priority_enabled)

    # ---- ZooMo AI yoqish/o'chirish ----
    if ai_form:
        setting.ai_enabled = bool(ai_enabled)
        setting.ai_client_enabled = bool(ai_client_enabled)
        setting.ai_courier_enabled = bool(ai_courier_enabled)
        setting.ai_partner_enabled = bool(ai_partner_enabled)
        setting.ai_dispatcher_enabled = bool(ai_dispatcher_enabled)
        setting.ai_led_enabled = bool(ai_led_enabled)
        setting.ai_help_enabled = bool(ai_help_enabled)
        setting.ai_campaigns_enabled = bool(ai_campaigns_enabled)
        setting.ai_delay_promo_enabled = bool(ai_delay_promo_enabled)
        if ai_delay_promo_amount is not None and ai_delay_promo_amount >= 0:
            setting.ai_delay_promo_amount = ai_delay_promo_amount

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


async def effective_bonus_value(db: AsyncSession, kind: str, city_id, default: float) -> float:
    """Mijoz shahri va hozirgi vaqt bo'yicha amaldagi qiymat: shahar qoidasi > umumiy qoida > asosiy sozlama."""
    now = datetime.utcnow()
    rows = (await db.execute(
        select(BonusRule).where(
            BonusRule.kind == kind,
            BonusRule.is_active == True,
            or_(BonusRule.starts_at.is_(None), BonusRule.starts_at <= now),
            or_(BonusRule.ends_at.is_(None), BonusRule.ends_at >= now),
            or_(BonusRule.city_id.is_(None), BonusRule.city_id == city_id) if city_id is not None else BonusRule.city_id.is_(None),
        ).order_by(BonusRule.id.desc())
    )).scalars().all()
    for r in rows:
        if r.city_id is not None and r.city_id == city_id:
            return r.value or 0.0
    for r in rows:
        if r.city_id is None:
            return r.value or 0.0
    return default or 0.0


async def validate_promo_for_client(db: AsyncSession, code: str, client: User, lock: bool = False):
    """Promo-kodni TO'LIQ tekshiradi (faol, muddat, shahar, limit, mijoz avval ishlatmaganmi).
    (promo, None) yoki (None, xato_matni) qaytaradi."""
    code = (code or "").strip().upper()
    if not code:
        return None, "Promo-kodni kiriting"
    q = select(PromoCode).where(PromoCode.code == code)
    if lock:
        q = q.with_for_update()
    promo = (await db.execute(q)).scalars().first()
    if not promo or not promo.is_active:
        return None, "Bunday promo-kod topilmadi yoki o'chirilgan"
    now = datetime.utcnow()
    if promo.starts_at and promo.starts_at > now:
        return None, "Bu promo-kod hali boshlanmagan"
    if promo.expires_at and promo.expires_at < now:
        return None, "Bu promo-kodning muddati tugagan"
    if promo.city_id is not None and promo.city_id != client.city_id:
        return None, "Bu promo-kod sizning shahringizda amal qilmaydi"
    if promo.max_uses and (promo.used_count or 0) >= promo.max_uses:
        return None, "Bu promo-kod limiti tugagan"
    used = (await db.execute(
        select(PromoCodeUsage.id).where(PromoCodeUsage.promo_code_id == promo.id, PromoCodeUsage.client_id == client.id)
    )).first()
    if used:
        return None, "Siz bu promo-koddan allaqachon foydalangansiz"
    return promo, None


async def _get_or_create_setting(db: AsyncSession) -> SystemSetting:
    result = await db.execute(select(SystemSetting))
    setting = result.scalars().first()
    if not setting:
        setting = SystemSetting()
        db.add(setting)
    return setting


async def _validate_assignable_courier(db: AsyncSession, courier_id: int) -> None:
    cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == courier_id))).scalars().first()
    if not cp or cp.application_status == "pending" or cp.is_blocked:
        raise HTTPException(status_code=400, detail="Bu kuryer tasdiqlanmagan, bloklangan yoki mavjud emas")


def _guard_status_change(order: Order, old_status) -> None:
    """Pul allaqachon hisoblangan (yetkazilgan) buyurtmani boshqa holatga qaytarib bo'lmaydi."""
    if old_status == OrderStatus.DELIVERED and order.status != OrderStatus.DELIVERED:
        order.status = old_status
        raise HTTPException(status_code=400, detail="Yetkazilgan buyurtma holatini o'zgartirib bo'lmaydi (pul allaqachon hisoblangan)")
    if old_status == OrderStatus.CANCELLED and getattr(order, "cancel_stage", None) and order.status != OrderStatus.CANCELLED:
        order.status = old_status
        raise HTTPException(status_code=400, detail="Mijoz rad etgan buyurtma bo'yicha pul allaqachon taqsimlangan — holatini o'zgartirib bo'lmaydi")
    if old_status == OrderStatus.CANCELLED and order.status == OrderStatus.DELIVERED:
        order.status = old_status
        raise HTTPException(status_code=400, detail="Bekor qilingan buyurtmani yetkazildi qilib bo'lmaydi")


def _assert_can_deliver(order: Order) -> None:
    """Pul hisoblanishidan oldin: bekor qilingan yoki to'lanmagan P2P buyurtma 'Yetkazildi' bo'lolmaydi."""
    if order.payment_method == "p2p" and not order.payment_verified:
        raise HTTPException(status_code=400, detail="P2P to'lov hali tasdiqlanmagan — buyurtmani yetkazildi qilib bo'lmaydi")


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
    # IDEMPOTENT: bir buyurtma uchun pul faqat BIR MARTA hisoblanadi
    # (DELIVERED -> boshqa holat -> DELIVERED qilinsa ham ikkinchi marta yozilmaydi).
    if getattr(order, "financials_applied", False):
        return
    order.financials_applied = True
    setting = await _get_or_create_setting(db)
    courier_pct = (setting.courier_share_percent or 0) / 100
    if order.courier_id and order.courier_rating_bonus is None:
        order.courier_rating_bonus = await courier_rating_bonus_for(db, order.courier_id)

    courier_earning = _courier_earning(order, courier_pct)
    courier_owes = _cash_due(order) - courier_earning

    # YANGI MODEL (oldindan to'ldiriladigan balans):
    # - NAQD buyurtma: mijoz pulni kuryerga bergan, pul kuryer cho'ntagida
    #   qoladi. Platformaga (hamkor + egasi) tegishli ulush (courier_owes)
    #   kuryerning BALANSIDAN yechiladi.
    # - P2P (karta) bilan to'langan va TASDIQLANGAN buyurtma: kuryer naqd pul
    #   olmagan; uning yetkazish haqi (ulushi) balansiga QO'SHILADI.
    is_prepaid_p2p = (order.payment_method == "p2p" and bool(order.payment_verified))

    if order.courier_id and (courier_owes > 0 or (is_prepaid_p2p and courier_earning > 0)):
        courier_result = await db.execute(
            select(User)
            .where(User.id == order.courier_id)
            .options(selectinload(User.courier_profile))
        )
        courier = courier_result.scalars().first()
        if courier and courier.courier_profile:
            if is_prepaid_p2p:
                if courier_earning > 0:
                    courier.courier_profile.balance = (courier.courier_profile.balance or 0) + courier_earning
                    db.add(Transaction(
                        user_id=courier.id,
                        type=TransactionType.ORDER_FEE,
                        amount=courier_earning,
                        note=f"Yetkazish haqi — buyurtma #{order.id} (mijoz karta orqali to'lagan, balansingizga qo'shildi)",
                        created_by_id=None,
                    ))
            else:
                courier.courier_profile.balance = (courier.courier_profile.balance or 0) - courier_owes
                db.add(Transaction(
                    user_id=courier.id,
                    type=TransactionType.WITHDRAWAL,
                    amount=courier_owes,
                    note=f"Naqd buyurtma #{order.id} — platforma ulushi balansingizdan yechildi",
                    created_by_id=None,
                ))

    if order.partner_id:
        partner_result = await db.execute(select(PartnerProfile).where(PartnerProfile.id == order.partner_id))
        partner = partner_result.scalars().first()
        if partner:
            commission_rate = (partner.commission_rate or 0) / 100
            partner_due = order.total_price * (1 - commission_rate)
            if partner_due > 0:
                partner.balance = (partner.balance or 0) + partner_due
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
        _cb_pct = await effective_bonus_value(db, "cashback", client.city_id, setting.cashback_earn_percent)
        if _cb_pct and _cb_pct > 0:
            cashback_earned = order.total_price * (_cb_pct / 100)
            client.cashback_balance = (client.cashback_balance or 0) + cashback_earned
            order.cashback_earned = cashback_earned

        # Referal bonusi — faqat BIRINCHI marta, faqat kimdir taklif qilgan bo'lsa
        _ref_amt = await effective_bonus_value(db, "referral", client.city_id, setting.referral_bonus_amount)
        if client.referred_by_id and client.referred_by_id != client.id and not client.referral_bonus_given and _ref_amt > 0:
            delivered_count_result = await db.execute(
                select(func.count(Order.id)).where(Order.client_id == client.id, Order.status == OrderStatus.DELIVERED)
            )
            delivered_count = delivered_count_result.scalar() or 0
            if delivered_count <= 1:  # aynan shu buyurtma — birinchisi
                referrer_result = await db.execute(select(User).where(User.id == client.referred_by_id))
                referrer = referrer_result.scalars().first()
                if referrer:
                    bonus = _ref_amt
                    client.cashback_balance = (client.cashback_balance or 0) + bonus
                    referrer.cashback_balance = (referrer.cashback_balance or 0) + bonus
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


# ==================== MASOFA (KM) BO'YICHA NARX VA KURYER HAQI ====================
def _road_km(setting, lat1, lon1, lat2, lon2) -> Optional[float]:
    """Ikki nuqta orasidagi TAXMINIY yo'l masofasi (km) — to'g'ri chiziq x admin
    belgilagan koeffitsient. Koordinata yo'q/noto'g'ri bo'lsa None."""
    try:
        if None in (lat1, lon1, lat2, lon2):
            return None
        lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
    except (TypeError, ValueError):
        return None
    if (lat1 == 0 and lon1 == 0) or (lat2 == 0 and lon2 == 0):
        return None
    factor = (getattr(setting, "km_road_factor", None) or 1.3) if setting else 1.3
    return round(_haversine_km(lat1, lon1, lat2, lon2) * max(factor, 1.0), 2)


def _round100(x: float) -> float:
    return float(round(x / 100.0) * 100)


def compute_delivery_fee(setting, distance_km: Optional[float]) -> float:
    """Mijoz to'laydigan yetkazish narxi. Km tizimi yoqilgan va masofa ma'lum
    bo'lsa: radius ichida belgilangan summa, undan keyingi har km uchun qo'shimcha.
    Aks holda eski tekis narx. Ob-havo koeffitsienti ikkala holatda ham qo'llanadi."""
    if not setting:
        return 10000.0
    weather = setting.weather_multiplier or 1.0
    flat = (setting.base_delivery_fee or 0.0) * weather
    if not getattr(setting, "km_pricing_enabled", False) or distance_km is None:
        return flat
    radius = setting.km_base_radius or 0.0
    base = setting.km_base_price or 0.0
    extra = setting.km_extra_price or 0.0
    fee = base + max(0.0, distance_km - radius) * extra
    return _round100(fee * weather)


def compute_courier_km_bonus(setting, distance_km: Optional[float]) -> float:
    """Mijoz qanchalik uzoqda bo'lsa, kuryerga shunchalik qo'shimcha haq (hamkor -> mijoz)."""
    if not setting or distance_km is None:
        return 0.0
    return _round100(distance_km * (getattr(setting, "courier_delivery_per_km", 0.0) or 0.0))


def compute_courier_pickup_bonus(setting, pickup_km: Optional[float]) -> float:
    """Kuryer buyurtmani olish uchun boradigan masofa uchun haq (kuryer -> hamkor)."""
    if not setting or pickup_km is None:
        return 0.0
    return _round100(pickup_km * (getattr(setting, "courier_pickup_per_km", 0.0) or 0.0))


def _courier_earning(order, courier_pct: float) -> float:
    """Kuryerning shu buyurtmadan JAMI daromadi: yetkazish narxidagi ulush + masofa uchun qo'shimcha haqlar."""
    return (
        (order.delivery_fee or 0.0) * courier_pct
        + (getattr(order, "courier_km_bonus", 0.0) or 0.0)
        + (getattr(order, "courier_pickup_bonus", 0.0) or 0.0)
        + (getattr(order, "courier_rating_bonus", 0.0) or 0.0)
    )


async def courier_rating_bonus_for(db: AsyncSession, courier_user_id: int, fallback_city_id: Optional[int] = None) -> float:
    """Kuryer shahridagi sozlamaga ko'ra reyting bonusi (so'm). Shart: yoqilgan, yetarli baho soni va o'rtacha baho."""
    try:
        u = (await db.execute(select(User).where(User.id == courier_user_id))).scalars().first()
        city_id = (u.city_id if u else None) or fallback_city_id
        if city_id is None:
            return 0.0
        cfg = (await db.execute(select(CityRatingBonus).where(CityRatingBonus.city_id == city_id))).scalars().first()
        if not cfg or not cfg.enabled or (cfg.bonus_amount or 0) <= 0:
            return 0.0
        avg, n = (await db.execute(select(func.avg(OrderRating.courier_stars), func.count(OrderRating.courier_stars))
                                   .where(OrderRating.courier_user_id == courier_user_id, OrderRating.courier_stars.isnot(None)))).first()
        if (n or 0) >= (cfg.min_count or 0) and avg is not None and float(avg) >= (cfg.min_rating or 0):
            return float(cfg.bonus_amount)
    except Exception as e:
        print(f"[REYTING BONUS XATOSI] {e!r}")
    return 0.0


async def _apply_pickup_bonus(db: AsyncSession, order: Order, courier_id: int, setting=None):
    """Kuryer buyurtmani olganda — uning oxirgi joylashuvidan hamkorgacha masofani hisoblab, haqni yozib qo'yadi.
    Shu bilan birga yuqori reytingli kuryerga shahar bo'yicha reyting bonusi ham yoziladi (bir marta)."""
    if order.courier_rating_bonus is None:
        try:
            _pcity = None
            if order.partner_id:
                _pcity = (await db.execute(select(PartnerProfile.city_id).where(PartnerProfile.id == order.partner_id))).scalars().first()
            order.courier_rating_bonus = await courier_rating_bonus_for(db, courier_id, _pcity)
        except Exception as e:
            print(f"[REYTING BONUS] {e!r}")
    try:
        setting = setting or await _get_or_create_setting(db)
        cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == courier_id))).scalars().first()
        partner = order.partner if "partner" in order.__dict__ else (
            await db.execute(select(PartnerProfile).where(PartnerProfile.id == order.partner_id))
        ).scalars().first()
        if not cp or not partner:
            return
        km = _road_km(setting, cp.latitude, cp.longitude, partner.latitude, partner.longitude)
        if km is None:
            return
        order.pickup_distance_km = km
        order.courier_pickup_bonus = compute_courier_pickup_bonus(setting, km)
    except Exception as e:
        print(f"[PICKUP BONUS XATOSI] {e!r}")




def _cash_due(order: Order) -> float:
    """Mijoz kuryerga NAQD to'laydigan summa: mahsulotlar + yetkazish - chegirma/keshbek."""
    total = (order.total_price or 0) + (order.delivery_fee or 0) - (order.discount_amount or 0)
    return max(0.0, total)


def _courier_cash_hold(order: Order, courier_pct: float) -> float:
    """Shu buyurtmani yetkazish uchun kuryer balansida KAMIDA qancha pul
    bo'lishi kerak. Naqd buyurtmada = platforma ulushi (jami - kuryer haqi);
    tasdiqlangan P2P buyurtmada = 0 (pul allaqachon platformada)."""
    if order.payment_method == "p2p" and order.payment_verified:
        return 0.0
    return max(0.0, _cash_due(order) - _courier_earning(order, courier_pct))


async def _courier_reserved_hold(db: AsyncSession, courier_id: int, courier_pct: float) -> float:
    """Kuryerning hozir yo'lda yurgan naqd buyurtmalari uchun band qilingan summa."""
    res = await db.execute(
        select(Order).where(Order.courier_id == courier_id, Order.status == OrderStatus.ON_THE_WAY)
    )
    held = sum(_courier_cash_hold(o, courier_pct) for o in res.scalars().all())
    # Pochta: yo'ldagi pochta buyurtmalarining platforma ulushi ham balansdan band
    pr = await db.execute(select(func.coalesce(func.sum(ParcelOrder.platform_share), 0.0)).where(
        ParcelOrder.courier_id == courier_id, ParcelOrder.status.in_(["accepted", "picked_up"])))
    # Express pochta: naqd yig'adigan kuryer uchun kutilayotgan ulush (qabul qilingandan yetkazilgungacha)
    ex1 = await db.execute(select(func.coalesce(func.sum(IntercityOrder.total), 0.0)).where(
        IntercityOrder.f1_id == courier_id, IntercityOrder.payer == "sender", IntercityOrder.status == "f1_accepted"))
    ex2 = await db.execute(select(func.coalesce(func.sum(IntercityOrder.total), 0.0)).where(
        IntercityOrder.f2_id == courier_id, IntercityOrder.payer == "receiver", IntercityOrder.status.in_(["at_b", "out_for_delivery"])))
    return held + float(pr.scalar() or 0.0) + float(ex1.scalar() or 0.0) + float(ex2.scalar() or 0.0)


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
            CourierProfile.is_blocked == False,  # admin bloklagan kuryerga avtomatik biriktirilmaydi
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

    # NAQD buyurtma faqat balansi yetadigan kuryerga biriktiriladi.
    setting_for_hold = await _get_or_create_setting(db)
    hold = _courier_cash_hold(order, (setting_for_hold.courier_share_percent or 0) / 100)
    free_candidates = [c for c in free_candidates if (c.courier_profile.balance or 0) >= hold]
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
    order.on_the_way_at = datetime.utcnow()
    await _apply_pickup_bonus(db, order, nearest.id)
    await db.commit()
    return nearest


@admin_router.post("/admin/settings/birthday")
async def update_birthday_setting(
    birthday_bonus_amount: float = Form(..., allow_inf_nan=False),
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
    referral_bonus_amount: float = Form(0.0, allow_inf_nan=False),
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
    cashback_earn_percent: float = Form(0.0, allow_inf_nan=False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("bonuscashback")),
):
    setting = await _get_or_create_setting(db)
    setting.bonus_cashback_text = bonus_cashback_text
    setting.cashback_visible = cashback_visible
    setting.cashback_earn_percent = cashback_earn_percent
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)



# ==================== PROMO-KODLAR va SHAHAR/MUDDATLI BONUS QOIDALARI ====================
def _city_scope_value(city_id: str, current_user: User):
    cid = None
    if (city_id or "").strip():
        try:
            cid = int(city_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Shahar noto'g'ri")
    if is_scoped_to_city(current_user):
        cid = current_user.city_id  # operator faqat o'z shahri uchun
    return cid


def _opt_float(v: str, name: str, minimum: float = 0.0):
    if not (v or "").strip():
        return None
    try:
        f = float(v.replace(",", "."))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} son bo'lishi kerak")
    if f != f or f in (float("inf"), float("-inf")) or f < minimum:
        raise HTTPException(status_code=400, detail=f"{name} noto'g'ri")
    return f


def _scope_guard(obj, current_user: User):
    if is_scoped_to_city(current_user) and obj.city_id != current_user.city_id:
        raise HTTPException(status_code=403, detail="Bu yozuv sizning shahringizga tegishli emas")


@admin_router.post("/admin/promo/create")
async def promo_create(
    code: str = Form(...),
    discount_percent: str = Form(""),
    discount_amount: str = Form(""),
    max_uses: str = Form(""),
    city_id: str = Form(""),
    starts_at: str = Form(""),
    expires_at: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("bonuscashback")),
):
    code = (code or "").strip().upper().replace(" ", "")
    if len(code) < 3 or len(code) > 32 or not code.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Kod 3–32 belgi: harf, raqam, - yoki _")
    pct = _opt_float(discount_percent, "Foiz")
    amt = _opt_float(discount_amount, "Summa")
    if bool(pct) == bool(amt):
        raise HTTPException(status_code=400, detail="Faqat bittasini kiriting: foiz YOKI summa")
    if pct and pct > 100:
        raise HTTPException(status_code=400, detail="Foiz 100 dan oshmasin")
    mu = _opt_float(max_uses, "Foydalanish soni")
    mu_i = int(mu) if mu else None
    st, en = _parse_local_dt(starts_at), _parse_local_dt(expires_at)
    if st and en and en <= st:
        raise HTTPException(status_code=400, detail="Tugash vaqti boshlanishdan keyin bo'lsin")
    exists = (await db.execute(select(PromoCode.id).where(PromoCode.code == code))).first()
    if exists:
        raise HTTPException(status_code=400, detail="Bunday kod allaqachon bor")
    db.add(PromoCode(code=code, discount_percent=pct, discount_amount=amt, max_uses=mu_i, used_count=0,
                     is_active=True, city_id=_city_scope_value(city_id, current_user), starts_at=st, expires_at=en))
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/admin/promo/{promo_id}/toggle")
async def promo_toggle(promo_id: int, db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(require_section("bonuscashback"))):
    p = await db.get(PromoCode, promo_id)
    if not p:
        raise HTTPException(status_code=404, detail="Topilmadi")
    _scope_guard(p, current_user)
    p.is_active = not p.is_active
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/admin/promo/{promo_id}/delete")
async def promo_delete(promo_id: int, db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(require_section("bonuscashback"))):
    p = await db.get(PromoCode, promo_id)
    if not p:
        raise HTTPException(status_code=404, detail="Topilmadi")
    _scope_guard(p, current_user)
    # Buyurtmalardagi havola va foydalanish tarixi saqlanadi: ishlatilgan kod o'chirilmaydi, o'chiriladi = nofaol
    used = (await db.execute(select(PromoCodeUsage.id).where(PromoCodeUsage.promo_code_id == p.id).limit(1))).first()
    ordered = (await db.execute(select(Order.id).where(Order.promo_code_id == p.id).limit(1))).first()
    if used or ordered:
        p.is_active = False
    else:
        await db.delete(p)
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


_RULE_SECTION = {"cashback": "bonuscashback", "referral": "referal", "birthday": "tugilgankun"}


async def _rule_section_check(db: AsyncSession, kind: str, current_user: User):
    sec = _RULE_SECTION.get(kind)
    if not sec:
        raise HTTPException(status_code=400, detail="Qoida turi noto'g'ri")
    if not await operator_can_see(db, current_user, sec):
        raise HTTPException(status_code=403, detail="Bu bo'limga huquqingiz yo'q")


@admin_router.post("/admin/bonus-rules/create")
async def bonus_rule_create(
    kind: str = Form(...),
    value: str = Form(...),
    note: str = Form(""),
    city_id: str = Form(""),
    starts_at: str = Form(""),
    ends_at: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    await _rule_section_check(db, kind, current_user)
    v = _opt_float(value, "Qiymat")
    if v is None:
        raise HTTPException(status_code=400, detail="Qiymatni kiriting")
    if kind == "cashback" and v > 100:
        raise HTTPException(status_code=400, detail="Cashback foizi 100 dan oshmasin")
    st, en = _parse_local_dt(starts_at), _parse_local_dt(ends_at)
    if st and en and en <= st:
        raise HTTPException(status_code=400, detail="Tugash vaqti boshlanishdan keyin bo'lsin")
    db.add(BonusRule(kind=kind, value=v, note=(note or "").strip()[:200] or None,
                     city_id=_city_scope_value(city_id, current_user), starts_at=st, ends_at=en,
                     is_active=True, created_by_id=current_user.id))
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/admin/bonus-rules/{rule_id}/toggle")
async def bonus_rule_toggle(rule_id: int, db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(get_current_admin_user)):
    r = await db.get(BonusRule, rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="Topilmadi")
    await _rule_section_check(db, r.kind, current_user)
    _scope_guard(r, current_user)
    r.is_active = not r.is_active
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/admin/bonus-rules/{rule_id}/delete")
async def bonus_rule_delete(rule_id: int, db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(get_current_admin_user)):
    r = await db.get(BonusRule, rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="Topilmadi")
    await _rule_section_check(db, r.kind, current_user)
    _scope_guard(r, current_user)
    await db.delete(r)
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


def _parse_local_dt(v: str):
    """«2026-10-20T14:30» (O'zbekiston vaqti, datetime-local) -> UTC naive datetime; bo'sh bo'lsa None."""
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt) - UZB_TZ_OFFSET
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail="Sana/vaqt formati noto'g'ri")


def _banner_schedule_values(city_id: str, starts_at: str, ends_at: str, slide_seconds: str, current_user: User):
    """Banner ko'rsatish sozlamalarini tekshirib qaytaradi. Operator — faqat o'z shahri uchun."""
    cid = None
    if (city_id or "").strip():
        try:
            cid = int(city_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Shahar noto'g'ri")
    if is_scoped_to_city(current_user):
        cid = current_user.city_id          # operator umumiy (hamma shahar) banner qo'sha olmaydi
    st, en = _parse_local_dt(starts_at), _parse_local_dt(ends_at)
    if st and en and en <= st:
        raise HTTPException(status_code=400, detail="Tugash vaqti boshlanishdan keyin bo'lsin")
    secs = None
    if (slide_seconds or "").strip():
        try:
            secs = float(slide_seconds)
        except ValueError:
            raise HTTPException(status_code=400, detail="Soniya son bo'lishi kerak")
        if not (1.0 <= secs <= 60.0):
            raise HTTPException(status_code=400, detail="Slayd vaqti 1–60 soniya oralig'ida bo'lsin")
    return cid, st, en, secs


def _banner_guard(banner: "Banner", current_user: User):
    """Operator faqat o'z shahri bannerlarini o'zgartira oladi (umumiy bannerlarni — faqat egasi)."""
    if is_scoped_to_city(current_user) and banner.city_id != current_user.city_id:
        raise HTTPException(status_code=403, detail="Bu banner sizning shahringizga tegishli emas")


@app.post("/admin/banners/create")
async def create_banner(
    title: str = Form(""),
    text_content: str = Form(""),
    link_url: str = Form(""),
    display_order: int = Form(0),
    banner_image: Optional[UploadFile] = File(None),
    bg_color: str = Form(""),
    city_id: str = Form(""),
    starts_at: str = Form(""),
    ends_at: str = Form(""),
    slide_seconds: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("banner")),
):
    _cid, _st, _en, _secs = _banner_schedule_values(city_id, starts_at, ends_at, slide_seconds, current_user)
    bg_color = bg_color if re.fullmatch(r"#[0-9a-fA-F]{6}", bg_color or "") else None
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
            bg_color=bg_color,
            is_active=True,
            city_id=_cid, starts_at=_st, ends_at=_en, slide_seconds=_secs,
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


@app.post("/admin/banners/{banner_id}/schedule")
async def banner_set_schedule(
    banner_id: int,
    city_id: str = Form(""), starts_at: str = Form(""), ends_at: str = Form(""), slide_seconds: str = Form(""),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("banner")),
):
    banner = (await db.execute(select(Banner).where(Banner.id == banner_id))).scalars().first()
    if not banner:
        raise HTTPException(status_code=404, detail="Banner topilmadi")
    _banner_guard(banner, current_user)
    banner.city_id, banner.starts_at, banner.ends_at, banner.slide_seconds = _banner_schedule_values(city_id, starts_at, ends_at, slide_seconds, current_user)
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/banners/{banner_id}/color")
async def banner_set_color(banner_id: int, bg_color: str = Form(""), db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("banner"))):
    banner = (await db.execute(select(Banner).where(Banner.id == banner_id))).scalars().first()
    if not banner:
        raise HTTPException(status_code=404, detail="Banner topilmadi")
    _banner_guard(banner, current_user)
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", bg_color or ""):
        raise HTTPException(status_code=400, detail="Rang noto'g'ri")
    banner.bg_color = bg_color
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/banners/{banner_id}/toggle")
async def toggle_banner(banner_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("banner"))):
    result = await db.execute(select(Banner).where(Banner.id == banner_id))
    banner = result.scalars().first()
    if not banner:
        raise HTTPException(status_code=404, detail="Banner topilmadi")
    _banner_guard(banner, current_user)
    banner.is_active = not banner.is_active
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/banners/{banner_id}/delete")
async def delete_banner(banner_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("banner"))):
    result = await db.execute(select(Banner).where(Banner.id == banner_id))
    banner = result.scalars().first()
    if not banner:
        raise HTTPException(status_code=404, detail="Banner topilmadi")
    _banner_guard(banner, current_user)
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
    current_user: User = Depends(get_current_admin_user),
):
    order_query = await db.execute(
        select(Order)
        .options(selectinload(Order.partner))
        .where(Order.id == order_id)
        .with_for_update(of=Order)
    )
    order = order_query.scalars().first()

    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    if is_scoped_to_city(current_user) and order.partner is not None and order.partner.city_id != current_user.city_id:
        raise HTTPException(status_code=403, detail="Bu buyurtma boshqa shaharga tegishli")

    old_status = order.status
    old_courier_id = order.courier_id

    try:
        order.status = OrderStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Noto'g'ri buyurtma holati")
    _guard_status_change(order, old_status)
    _staff_cancelled = False
    if order.status == OrderStatus.CANCELLED and old_status != OrderStatus.CANCELLED:
        _staff_cancelled = await _apply_staff_cancel(db, order, "by_admin")

    if courier_id:
        await _validate_assignable_courier(db, courier_id)
        order.courier_id = courier_id
        if not order.courier_pickup_bonus:
            await _apply_pickup_bonus(db, order, courier_id)

    # Avtomatik moliyaviy hisob-kitob — FAQAT buyurtma ENDI, shu safar
    # birinchi marta "Yetkazildi" bo'layotgan bo'lsa (aks holda takroriy
    # saqlashda pul ikki marta hisoblanib ketardi).
    if order.status == OrderStatus.DELIVERED and old_status != OrderStatus.DELIVERED:
        _assert_can_deliver(order)
        await apply_cod_delivery_financials(db, order)

    await db.commit()
    if _staff_cancelled:
        await _notify_staff_cancel(db, order)

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
    current_user: User = Depends(get_current_admin_user),
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
        .with_for_update(of=Order)
    )
    order = order_query.scalars().first()

    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    if is_scoped_to_city(current_user) and order.partner is not None and order.partner.city_id != current_user.city_id:
        raise HTTPException(status_code=403, detail="Bu buyurtma boshqa shaharga tegishli")

    old_status = order.status
    old_courier_id = order.courier_id

    try:
        order.status = OrderStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Noto'g'ri buyurtma holati")
    _guard_status_change(order, old_status)
    _staff_cancelled = False
    if order.status == OrderStatus.CANCELLED and old_status != OrderStatus.CANCELLED:
        _staff_cancelled = await _apply_staff_cancel(db, order, "by_admin")

    if courier_id:
        await _validate_assignable_courier(db, courier_id)
        order.courier_id = courier_id
        if not order.courier_pickup_bonus:
            await _apply_pickup_bonus(db, order, courier_id)

    if order.status == OrderStatus.DELIVERED and old_status != OrderStatus.DELIVERED:
        _assert_can_deliver(order)
        await apply_cod_delivery_financials(db, order)

    await db.commit()
    if _staff_cancelled:
        await _notify_staff_cancel(db, order)

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
PARTNER_CATEGORIES = ["Supermarket", "Oziq-ovqat do'konlari", "Kafe va restoranlar", "Dorixona", "Kosmetika", "Qurilish mollari", "Boshqa"]


def _norm_partner_category(raw: Optional[str]) -> str:
    """Kategoriya nomini bir xil ko'rinishga keltiradi: ortiqcha bo'sh joylar olinadi, standart nomlar
    registrdan qat'i nazar moslashtiriladi ("supermarket" == "Supermarket"), yangisi bosh harf bilan saqlanadi."""
    t = " ".join((raw or "").split())[:60]
    if not t:
        return "Boshqa"
    low = t.casefold()
    for c in PARTNER_CATEGORIES:
        if c.casefold() == low:
            return c
    return t[0].upper() + t[1:]


@partners_router.post("/create")
async def create_partner(
    request: Request,
    brand_name: str = Form(...),
    category: str = Form(...),
    address: str = Form(...),
    city_id: int = Form(...),
    commission_rate: float = Form(10.0, allow_inf_nan=False),
    opening_time: str = Form("09:00"),
    closing_time: str = Form("23:00"),
    min_order_amount: float = Form(0.0, allow_inf_nan=False),
    login_phone: Optional[str] = Form(None),
    login_password: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    owner: User = Depends(get_current_admin_user),
):
    operator_mode = owner.role != UserRole.OWNER
    if operator_mode:
        # Operator: faqat o'z shahri; komissiyani o'zi belgilay olmaydi (tizim standarti);
        # hamkor PIN-ni operator kiritmaydi — hamkor o'zi o'rnatadi.
        if not is_scoped_to_city(owner) or owner.city_id is None:
            raise HTTPException(status_code=403, detail="Shahringiz belgilanmagan — egasi bilan bog'laning")
        city_id = owner.city_id
        _st = await _get_or_create_setting(db)
        commission_rate = _st.service_commission_percent if _st and _st.service_commission_percent is not None else 10.0
        login_password = None
        if not (login_phone or "").strip():
            raise HTTPException(status_code=400, detail="Hamkorning telefon raqamini kiriting")
    new_partner = PartnerProfile(
        brand_name=brand_name,
        category=_norm_partner_category(category),
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
    elif operator_mode:
        # Faqat raqam: mavjud akkauntning parolini O'ZGARTIRMAYMIZ; yangi bo'lsa tasodifiy
        # (hech kim bilmaydigan) parol bilan yaratiladi — hamkor PIN-ni keyin o'zi o'rnatadi.
        import secrets as _sec
        _ph = normalize_phone(login_phone) or login_phone.strip()
        _u = await find_user_by_phone(db, _ph)
        if _u:
            if _u.role in (UserRole.OWNER, UserRole.ADMIN):
                raise HTTPException(status_code=400, detail="Bu raqam admin/operator akkauntiga tegishli")
            partner_user = _u
        else:
            partner_user = User(full_name=brand_name, phone_number=_ph,
                                password_hash=hash_password(_sec.token_hex(8)),
                                role=UserRole.CLIENT, city_id=city_id, is_active=True)
            db.add(partner_user)
            await db.flush()
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
    commission_rate: float = Form(..., allow_inf_nan=False),
    opening_time: str = Form(...),
    closing_time: str = Form(...),
    min_order_amount: float = Form(0.0, allow_inf_nan=False),
    db: AsyncSession = Depends(get_db),
    owner: User = Depends(require_owner),
):
    partner_query = await db.execute(select(PartnerProfile).where(PartnerProfile.id == partner_id))
    partner = partner_query.scalars().first()

    if not partner:
        raise HTTPException(status_code=404, detail="Do'kon topilmadi")

    partner.brand_name = brand_name
    partner.category = _norm_partner_category(category)
    partner.address = address
    partner.city_id = city_id
    partner.commission_rate = commission_rate
    partner.opening_time = opening_time
    partner.closing_time = closing_time
    partner.min_order_amount = min_order_amount

    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@partners_router.get("/{partner_id}/legal", dependencies=[Depends(require_owner), Depends(secure_action)])
async def owner_view_partner_legal(partner_id: int, db: AsyncSession = Depends(get_db)):
    """Hamkor yuridik rekvizitlari — FAQAT egasi va faqat maxfiy javob tasdiqlangandan keyin (sahifada ular umuman yozilmaydi)."""
    p = (await db.execute(select(PartnerProfile).where(PartnerProfile.id == partner_id))).scalars().first()
    if not p:
        raise HTTPException(status_code=404, detail="Do'kon topilmadi")
    return JSONResponse({"legal_type": p.legal_type or "legal", "legal_name": p.legal_name or "", "stir": p.stir or "",
                         "pinfl": p.pinfl or "", "bank_account": p.bank_account or "", "mfo": p.mfo or "",
                         "bank_name": p.bank_name or "", "director_name": p.director_name or "",
                         "director_phone": p.director_phone or ""},
                        headers={"Cache-Control": "no-store"})


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

    has_orders = (await db.execute(select(func.count(Order.id)).where(Order.partner_id == partner_id))).scalar() or 0
    if has_orders or abs(partner.balance or 0) > 0.5 or abs(getattr(partner, "frozen_balance", 0) or 0) > 0.5:
        raise HTTPException(
            status_code=400,
            detail="Bu do'konda buyurtmalar tarixi yoki balans bor — o'chirib bo'lmaydi. Uni yopib qo'ying (o'chirish tugmasi o'rniga «yopiq» qiling).",
        )
    try:
        await db.delete(partner)
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Do'konni o'chirib bo'lmadi — unga bog'langan yozuvlar bor. Uni yopib qo'ying.")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# ==================== 5. MAHSULOTLAR (MENU) BOSHQARUVI ====================
@products_router.post("/create")
async def create_product(
    partner_id: int = Form(...),
    name: str = Form(...),
    price: float = Form(..., allow_inf_nan=False),
    description: Optional[str] = Form(None),
    category: str = Form("Boshqa"),
    image: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
    owner: User = Depends(get_current_admin_user),
):
    partner_query = await db.execute(select(PartnerProfile).where(PartnerProfile.id == partner_id))
    _pp = partner_query.scalars().first()
    if not _pp:
        raise HTTPException(status_code=404, detail="Bunday do'kon topilmadi")
    if owner.role != UserRole.OWNER:
        if not await operator_can_see(db, owner, "mahsulot_qoshish"):
            raise HTTPException(status_code=403, detail="Mahsulot qo'shishga egasi ruxsat bermagan")
        if _pp.city_id != owner.city_id:
            raise HTTPException(status_code=403, detail="Bu do'kon sizning shahringizga tegishli emas")

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
    price: float = Form(..., allow_inf_nan=False),
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

    used = (await db.execute(select(func.count(OrderItem.id)).where(OrderItem.product_id == product_id))).scalar() or 0
    if used:
        # Buyurtma tarixida bor — tarix buzilmasligi uchun o'chirmaymiz, faqat yashiramiz
        product.is_available = False
        await db.commit()
    else:
        try:
            await db.delete(product)
            await db.commit()
        except Exception:
            await db.rollback()
            raise HTTPException(status_code=400, detail="Mahsulotni o'chirib bo'lmadi — u bilan bog'langan yozuvlar bor.")
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
    # ONLINE va joylashuvi yangi kuryerlar + yo'lda buyurtmasi bor kuryerlar
    # (signali biroz eskirgan bo'lsa ham — "stale" belgisi bilan, xaritadan yo'qolib qolmasin).
    busy_courier_ids = select(Order.courier_id).where(
        Order.status == OrderStatus.ON_THE_WAY, Order.courier_id.is_not(None)
    )
    courier_stmt = (
        select(User)
        .join(CourierProfile, CourierProfile.user_id == User.id)
        .options(selectinload(User.courier_profile))
        .where(
            User.is_active == True,
            CourierProfile.latitude.is_not(None),
            CourierProfile.longitude.is_not(None),
            or_(
                and_(CourierProfile.is_online == True, CourierProfile.location_updated_at >= freshness_cutoff),
                User.id.in_(busy_courier_ids),
            ),
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

    now_utc = datetime.utcnow()
    couriers_data = []
    for c in couriers:
        cp = c.courier_profile
        active_order = active_orders_map.get(c.id)
        age = (now_utc - cp.location_updated_at).total_seconds() if cp.location_updated_at else None
        couriers_data.append({
            "courier_id": c.id,
            "full_name": c.full_name,
            "phone_number": c.phone_number,
            "transport_type": cp.transport_type,
            "lat": cp.latitude,
            "lng": cp.longitude,
            "updated_at": cp.location_updated_at.isoformat() if cp.location_updated_at else None,
            "age_seconds": int(age) if age is not None else None,
            "stale": (age is None or age > MAP_LOCATION_FRESHNESS_MINUTES * 60),
            "online": bool(cp.is_online),
            "busy": active_order is not None,
            "active_order_id": active_order.id if active_order else None,
            "active_order_address": active_order.delivery_address if active_order else None,
            "dest_lat": active_order.delivery_latitude if active_order else None,
            "dest_lng": active_order.delivery_longitude if active_order else None,
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
            "courier_id": o.courier_id,
            "client_phone": o.client.phone_number if o.client else None,
        }
        for o in active_orders
    ]

    return JSONResponse({
        "couriers": couriers_data, "partners": partners_data, "active_clients": clients_data,
        "server_time": datetime.utcnow().isoformat(),
    })


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
    credit_limit: Optional[float] = Form(None, allow_inf_nan=False),
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
            # Yangi modelda limit avtomatik bloklamaydi (balans yetmasa naqd
            # buyurtma shunchaki berilmaydi) — maydon faqat eski ma'lumot sifatida saqlanadi.
            courier_user.courier_profile.credit_limit = credit_limit

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

    n_orders = (await db.execute(select(func.count(Order.id)).where(Order.courier_id == user_id))).scalar() or 0
    if n_orders:
        # Tarix (buyurtmalar, pul harakatlari) saqlanishi uchun o'chirmaymiz — faqat faolsizlantiramiz
        courier_user.is_active = False
        cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == user_id))).scalars().first()
        if cp:
            cp.is_online = False
        await db.commit()
    else:
        try:
            await db.delete(courier_user)
            await db.commit()
        except Exception:
            await db.rollback()
            raise HTTPException(status_code=400, detail="Kuryerni o'chirib bo'lmadi — unga bog'langan yozuvlar bor. Bloklab qo'ying.")
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
        amount=await effective_bonus_value(db, "birthday", client.city_id, setting.birthday_bonus_amount),
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
    amount: float = Form(..., allow_inf_nan=False),
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
    amount: float = Form(..., allow_inf_nan=False),
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
            profile.balance = (profile.balance or 0) - wd.amount
            profile.frozen_balance = max(0.0, (profile.frozen_balance or 0) - wd.amount)
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
            partner.frozen_balance = max(0.0, (partner.frozen_balance or 0) - wd.amount)
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
            profile.frozen_balance = max(0.0, (profile.frozen_balance or 0) - wd.amount)
    elif wd.partner_id:
        partner_result = await db.execute(
            select(PartnerProfile).where(PartnerProfile.id == wd.partner_id).with_for_update()
        )
        partner = partner_result.scalars().first()
        if partner:
            partner.frozen_balance = max(0.0, (partner.frozen_balance or 0) - wd.amount)

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
        await _apply_staff_cancel(db, order, "payment_rejected")
    await db.commit()
    await _mark_order_receipts(db, order.id, approved, verified_by_id, reason)

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


# ==================== CHEKLAR (to'lov cheki rasmi) — tasdiqlash tizimi ====================
RECEIPT_KIND_LABELS = {
    "courier_topup": "Kuryer → Admin (balans to'ldirish)",
    "partner_topup": "Hamkor → Admin (hisob to'ldirish)",
    "client_order": "Mijoz → Admin (P2P buyurtma)",
}
MIN_TOPUP_AMOUNT = 1000.0
MAX_TOPUP_AMOUNT = 100_000_000.0
MAX_PENDING_RECEIPTS_PER_USER = 5


async def _get_topup_card_info(db: AsyncSession) -> Optional[dict]:
    """Admin'ning faol P2P kartasi (kuryer/hamkor/mijoz pul o'tkazadigan karta)."""
    card = await _get_active_p2p_card(db)
    if not card:
        return None
    digits = card_security.decrypt_card_number(card.encrypted_card_number)
    if not digits:
        return None
    return {
        "card_number": " ".join(digits[i:i + 4] for i in range(0, len(digits), 4)),
        "bank_name": card.bank_name,
        "card_holder_name": card.card_holder_name,
    }


def _receipt_to_dict(r: PaymentReceipt) -> dict:
    who = "—"
    if r.kind == "partner_topup" and r.partner:
        who = r.partner.brand_name
    elif r.user:
        who = r.user.full_name or r.user.phone_number
    return {
        "id": r.id,
        "kind": r.kind,
        "kind_label": RECEIPT_KIND_LABELS.get(r.kind, r.kind),
        "who": who,
        "phone": r.user.phone_number if r.user else "",
        "amount": r.amount or 0,
        "order_id": r.order_id,
        "comment": r.comment or "",
        "status": r.status or "pending",
        "reject_reason": r.reject_reason or "",
        "created_at": r.created_at,
        "reviewed_at": r.reviewed_at,
    }


async def _create_receipt(
    db: AsyncSession, *, kind: str, user: User, amount: float, file: UploadFile,
    partner_id: Optional[int] = None, order_id: Optional[int] = None, comment: str = "",
) -> PaymentReceipt:
    if amount < MIN_TOPUP_AMOUNT or amount > MAX_TOPUP_AMOUNT:
        raise HTTPException(
            status_code=400,
            detail=f"Summa {MIN_TOPUP_AMOUNT:,.0f} dan {MAX_TOPUP_AMOUNT:,.0f} so'mgacha bo'lishi kerak",
        )
    pending_count = (await db.execute(
        select(func.count(PaymentReceipt.id)).where(
            PaymentReceipt.user_id == user.id, PaymentReceipt.status == "pending",
            PaymentReceipt.kind == kind,
        )
    )).scalar() or 0
    if kind != "client_order" and pending_count >= MAX_PENDING_RECEIPTS_PER_USER:
        raise HTTPException(status_code=400, detail="Ko'rib chiqilmagan cheklaringiz juda ko'p — avval ularni tasdiqlashni kuting")
    data, mime = await process_uploaded_image(file)
    receipt = PaymentReceipt(
        kind=kind, user_id=user.id, partner_id=partner_id, order_id=order_id,
        amount=amount, image_data=data, image_mime=mime, comment=(comment or "")[:300],
        status="pending",
    )
    db.add(receipt)
    await db.commit()
    await db.refresh(receipt)

    # Egalarga (OWNER) Telegram orqali xabar — best-effort
    try:
        owners = (await db.execute(
            select(User).where(User.role == UserRole.OWNER, User.telegram_id.is_not(None))
        )).scalars().all()
        for o in owners:
            await send_telegram_message(
                o.telegram_id,
                f"🧾 Yangi chek #{receipt.id}: {RECEIPT_KIND_LABELS.get(kind, kind)}\n"
                f"{user.full_name or user.phone_number} — {amount:,.0f} so'm\nAdmin panel → Moliya → Cheklar",
            )
    except Exception as e:
        print(f"Chek bildirishnomasi xatoligi: {e}")
    return receipt


async def _mark_order_receipts(db: AsyncSession, order_id: int, approved: bool, reviewer_id: int, reason: str = "") -> None:
    """Buyurtmaga bog'langan kutilayotgan cheklarni (admin buyurtmani botdan
    yoki paneldan tasdiqlaganda) ham yopadi — ikki joyda holat farq qilmasin."""
    res = await db.execute(select(PaymentReceipt).where(
        PaymentReceipt.order_id == order_id, PaymentReceipt.kind == "client_order", PaymentReceipt.status == "pending"
    ))
    for r in res.scalars().all():
        r.status = "approved" if approved else "rejected"
        r.reviewed_by_id = reviewer_id
        r.reviewed_at = datetime.utcnow()
        if not approved:
            r.reject_reason = reason or None
    await db.commit()


@finance_router.get("/receipts")
async def list_receipts(
    status_filter: str = "pending",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("moliya")),
):
    stmt = select(PaymentReceipt).options(
        selectinload(PaymentReceipt.user), selectinload(PaymentReceipt.partner)
    ).order_by(PaymentReceipt.created_at.desc()).limit(100)
    if status_filter in ("pending", "approved", "rejected"):
        stmt = stmt.where(PaymentReceipt.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    out = []
    for r in rows:
        d = _receipt_to_dict(r)
        d["created_at"] = r.created_at.isoformat() if r.created_at else None
        d["reviewed_at"] = r.reviewed_at.isoformat() if r.reviewed_at else None
        out.append(d)
    return {"receipts": out}


@finance_router.get("/receipts/{receipt_id}/image")
async def receipt_image(
    receipt_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("moliya")),
):
    r = (await db.execute(select(PaymentReceipt).where(PaymentReceipt.id == receipt_id))).scalars().first()
    if not r or not r.image_data:
        raise HTTPException(status_code=404, detail="Chek rasmi topilmadi")
    return Response(
        content=r.image_data, media_type=r.image_mime or "image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@finance_router.post("/receipts/{receipt_id}/approve")
async def approve_receipt(
    receipt_id: int,
    amount: Optional[float] = Form(None, allow_inf_nan=False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("moliya")),
):
    """Chekni tasdiqlaydi. Kuryer/hamkor chekida balans (admin haqiqatda
    tushgan summani `amount` bilan tuzatishi mumkin) to'ldiriladi; mijoz
    chekida esa P2P buyurtma to'lovi tasdiqlanadi."""
    r = (await db.execute(
        select(PaymentReceipt).where(PaymentReceipt.id == receipt_id).with_for_update()
    )).scalars().first()
    if not r:
        raise HTTPException(status_code=404, detail="Chek topilmadi")
    if r.status != "pending":
        raise HTTPException(status_code=400, detail="Bu chek allaqachon ko'rib chiqilgan")

    final_amount = amount if (amount is not None and amount > 0) else (r.amount or 0)
    notify_user_id = r.user_id
    msg = ""

    if r.kind == "client_order":
        order = (await db.execute(select(Order).where(Order.id == r.order_id))).scalars().first()
        if not order:
            raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
        r.status = "approved"
        r.reviewed_by_id = current_user.id
        r.reviewed_at = datetime.utcnow()
        await db.commit()
        if not order.payment_verified and order.status != OrderStatus.CANCELLED:
            await _verify_p2p_order(db, order, True, current_user.id)
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

    if r.kind == "courier_topup":
        prof = (await db.execute(
            select(CourierProfile).where(CourierProfile.user_id == r.user_id).with_for_update()
        )).scalars().first()
        if not prof:
            raise HTTPException(status_code=404, detail="Kuryer profili topilmadi")
        prof.balance = (prof.balance or 0) + final_amount
        db.add(Transaction(
            user_id=r.user_id, type=TransactionType.DEPOSIT, amount=final_amount,
            note=f"Balans to'ldirildi — chek #{r.id} tasdiqlandi", created_by_id=current_user.id,
        ))
        msg = f"✅ Chekingiz tasdiqlandi — balansingizga {final_amount:,.0f} so'm qo'shildi."
    elif r.kind == "partner_topup":
        partner = (await db.execute(
            select(PartnerProfile).where(PartnerProfile.id == r.partner_id).with_for_update()
        )).scalars().first()
        if not partner:
            raise HTTPException(status_code=404, detail="Hamkor topilmadi")
        partner.balance = (partner.balance or 0) + final_amount
        db.add(Transaction(
            partner_id=partner.id, type=TransactionType.DEPOSIT, amount=final_amount,
            note=f"Hisob to'ldirildi — chek #{r.id} tasdiqlandi", created_by_id=current_user.id,
        ))
        msg = f"✅ Chekingiz tasdiqlandi — hisobingizga {final_amount:,.0f} so'm qo'shildi."
    else:
        raise HTTPException(status_code=400, detail="Noma'lum chek turi")

    r.amount = final_amount
    r.status = "approved"
    r.reviewed_by_id = current_user.id
    r.reviewed_at = datetime.utcnow()
    await db.commit()

    try:
        u = (await db.execute(select(User).where(User.id == notify_user_id))).scalars().first()
        if u and u.telegram_id and msg:
            await send_telegram_message(u.telegram_id, msg)
    except Exception as e:
        print(f"Chek tasdiq bildirishnomasi xatoligi: {e}")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@finance_router.post("/receipts/{receipt_id}/reject")
async def reject_receipt(
    receipt_id: int,
    reason: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("moliya")),
):
    r = (await db.execute(
        select(PaymentReceipt).where(PaymentReceipt.id == receipt_id).with_for_update()
    )).scalars().first()
    if not r:
        raise HTTPException(status_code=404, detail="Chek topilmadi")
    if r.status != "pending":
        raise HTTPException(status_code=400, detail="Bu chek allaqachon ko'rib chiqilgan")

    r.status = "rejected"
    r.reject_reason = (reason or "").strip()[:300] or None
    r.reviewed_by_id = current_user.id
    r.reviewed_at = datetime.utcnow()
    await db.commit()

    if r.kind == "client_order" and r.order_id:
        order = (await db.execute(select(Order).where(Order.id == r.order_id))).scalars().first()
        if order and not order.payment_verified and order.status != OrderStatus.CANCELLED:
            await _verify_p2p_order(db, order, False, current_user.id, reason)
    else:
        try:
            u = (await db.execute(select(User).where(User.id == r.user_id))).scalars().first()
            if u and u.telegram_id:
                extra = f"\nSabab: {r.reject_reason}" if r.reject_reason else ""
                await send_telegram_message(u.telegram_id, f"❌ Chek #{r.id} ({r.amount:,.0f} so'm) tasdiqlanmadi.{extra}")
        except Exception as e:
            print(f"Chek rad bildirishnomasi xatoligi: {e}")
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
    if os.getenv("ALLOW_SYSTEM_RESET", "").lower() not in ("1", "true", "yes"):
        raise HTTPException(
            status_code=403,
            detail="Tizimni tozalash o'chirilgan. Real ma'lumot himoyasi uchun faqat ALLOW_SYSTEM_RESET=1 bo'lganda ishlaydi.",
        )
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
    await db.execute(text("DELETE FROM users WHERE role IN ('CLIENT', 'COURIER')"))
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Tozalab bo'lmadi (bog'liq yozuvlar bor): {str(e)[:200]}")

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


_WH_SECRET_ON = False   # webhook secret bilan ro'yxatdan o'tgandan KEYIN tekshiriladi (eski webhook ishdan chiqmasligi uchun)


def _telegram_webhook_secret() -> str:
    from telegram_bot import webhook_secret
    return webhook_secret() if (_WH_SECRET_ON and os.getenv("TELEGRAM_BOT_TOKEN")) else ""


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Telegram har bir yangi xabar/harakat haqida shu manzilga POST
    so'rov yuboradi. Bu yerda: /start, telefon ulashish (contact), tugma
    bosish (callback_query) va PIN kiritish (oddiy matn) qayta ishlanadi."""
    _wh_secret = _telegram_webhook_secret()
    if _wh_secret and request.headers.get("x-telegram-bot-api-secret-token") != _wh_secret:
        raise HTTPException(status_code=403, detail="Forbidden")
    update = await request.json()

    # DIQQAT (VAQTINCHALIK DEBUG): Telegram'dan har bir kelgan update'ni
    # to'liq ko'rish uchun. Muammo tuzatilgach, buni olib tashlash mumkin —
    # hozircha "tugma bosilganda hech narsa bo'lmayapti" degan xatoni
    # aniqlashtirish uchun eng ishonchli yo'l shu.
    if os.getenv("DEBUG_TELEGRAM", "").lower() in ("1", "true"):
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

            if data == "set_app_pin":
                bot_conversation_state[chat_id] = {"awaiting_pin_for": "client"}
                await send_telegram_message(
                    chat_id,
                    "🔑 Ilovaga kirish uchun 4 xonali PIN kod o'ylab toping va shu yerga yozing (masalan: 4271).\n"
                    "Keyin ilovada telefon raqamingiz va shu PIN bilan kirasiz.",
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
            "Assalomu alaykum! 👋 <b>ZooMo</b> botiga xush kelibsiz.\n\n"
            "Tizimga ulanish uchun quyidagi tugma orqali telefon raqamingizni yuboring:",
            reply_markup=contact_request_keyboard(),
        )
        return {"ok": True}

    if text.strip().lower() in ("/pin", "/kod"):
        u = (await db.execute(select(User).where(User.telegram_id == str(chat_id)))).scalars().first()
        if not u:
            await send_telegram_message(chat_id, "Avval /start bosib, telefon raqamingizni ulashing.")
        else:
            bot_conversation_state[chat_id] = {"awaiting_pin_for": "client"}
            await send_telegram_message(chat_id, "🔑 Ilova uchun 4 xonali PIN kod yozing (masalan: 4271):")
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

        if role_key == "client":
            await db.commit()
            bot_conversation_state.pop(chat_id, None)
            await send_telegram_message(
                chat_id,
                "✅ PIN o'rnatildi!\n\nEndi ZooMo ilovasiga (telefon/kompyuterga o'rnatilgan) telefon raqamingiz va shu PIN bilan kirishingiz mumkin.\n"
                "Xavfsizlik uchun yuborgan PIN xabaringizni o'chirib qo'ying.",
            )
            return {"ok": True}

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
        app_url = get_public_base_url(request) + "/app"
        role_choice_keyboard = {
            "inline_keyboard": [
                [{"text": "🏠 ZooMo ilovasini ochish", "web_app": {"url": app_url}}],
                [{"text": "🛍 Menyuni ochish (mijoz)", "web_app": {"url": shop_url}}],
                [{"text": "🔑 Ilova uchun PIN o'rnatish", "callback_data": "set_app_pin"}],
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
    if isinstance(result, dict) and result.get("ok"):
        globals()["_WH_SECRET_ON"] = True
    return {"webhook_url": webhook_url, "telegram_response": result}


@app.get("/telegram/webhook-info")
async def telegram_webhook_info(owner: User = Depends(require_owner)):
    """Diagnostika uchun: hozirgi webhook sozlamasi qanday ekanini
    ko'rsatadi — xususan 'allowed_updates' ichida 'callback_query'
    borligini shu yerdan TEKSHIRISH mumkin (taxmin qilmasdan)."""
    return await get_telegram_webhook_info()


# ==================== ILOVA: RO'YXATDAN O'TISH (mijoz / kuryer / hamkor) ====================
app_api_router = APIRouter(prefix="/api/app", tags=["Ilova (kirish/ro'yxat)"])

applications_router = APIRouter(tags=["Arizalar"])

COURIER_TRANSPORTS = {"walking", "bike", "scooter", "car"}


class AppMeBody(BaseModel):
    init_data: Optional[str] = None


class AppRegisterBody(BaseModel):
    role: str                       # "client" | "courier" | "partner"
    full_name: str
    phone_number: str
    password: str                   # 4 xonali PIN
    accepted_terms: bool = False
    init_data: Optional[str] = None  # Telegram ichida ochilgan bo'lsa
    city_id: Optional[int] = None
    latitude: Optional[float] = None   # mijoz joylashuvi
    longitude: Optional[float] = None
    transport_type: Optional[str] = None  # kuryer
    # hamkor
    brand_name: Optional[str] = None
    category: Optional[str] = None
    address: Optional[str] = None
    legal_name: Optional[str] = None
    stir: Optional[str] = None
    bank_account: Optional[str] = None
    mfo: Optional[str] = None
    bank_name: Optional[str] = None
    director_name: Optional[str] = None
    director_phone: Optional[str] = None
    legal_type: Optional[str] = None
    pinfl: Optional[str] = None


@app_api_router.get("/config")
async def app_config(db: AsyncSession = Depends(get_db)):
    """Ro'yxatdan o'tish formasi uchun: shaharlar va rol shartlari."""
    cities = (await db.execute(select(City).where(City.is_active == True).order_by(City.name))).scalars().all()
    setting = await _get_or_create_setting(db)
    await db.commit()
    return {
        "cities": [{"id": c.id, "name": c.name} for c in cities],
        "terms": {
            "client": setting.client_terms or "",
            "courier": setting.courier_terms or "",
            "partner": setting.partner_terms or "",
        },
    }


@app_api_router.post("/me")
async def app_me(request: Request, body: AppMeBody, db: AsyncSession = Depends(get_db)):
    """Hozirgi foydalanuvchi holati: ro'yxatdan o'tganmi, qaysi rollari bor,
    arizalari qaysi holatda. Telegram initData yoki sessiya bo'yicha."""
    user = None
    tg_user = validate_telegram_init_data(body.init_data) if body.init_data else None
    opts = (selectinload(User.courier_profile), selectinload(User.partner_profile))
    if tg_user:
        user = (await db.execute(
            select(User).where(User.telegram_id == str(tg_user["id"])).options(*opts)
        )).scalars().first()
    if not user and request.session.get("user_id"):
        user = (await db.execute(select(User).where(User.id == request.session["user_id"]).options(*opts))).scalars().first()
    if not user:
        return {"registered": False, "in_telegram": bool(tg_user)}

    cp, pp = user.courier_profile, user.partner_profile
    return {
        "registered": True,
        "in_telegram": bool(tg_user),
        "name": user.full_name,
        "phone": user.phone_number,
        "is_admin": user.role in (UserRole.OWNER, UserRole.ADMIN),
        "client": True,
        "courier": ({"status": cp.application_status or "approved"} if cp else None),
        "partner": ({"status": pp.application_status or "approved", "brand": pp.brand_name} if pp else None),
    }


@app_api_router.post("/register")
async def app_register(body: AppRegisterBody, request: Request, db: AsyncSession = Depends(get_db)):
    role = (body.role or "").strip().lower()
    if role not in ("client", "courier", "partner"):
        raise HTTPException(status_code=400, detail="Rolni tanlang: mijoz, kuryer yoki hamkor")

    full_name = (body.full_name or "").strip()
    if len(full_name) < 2:
        raise HTTPException(status_code=400, detail="Ismingizni kiriting")
    phone = normalize_phone(body.phone_number)
    if not re.fullmatch(r"\+998\d{9}", phone or ""):
        raise HTTPException(status_code=400, detail="Telefon raqami noto'g'ri (masalan: +998901234567)")
    validate_pin(body.password or "")
    if not body.accepted_terms:
        raise HTTPException(status_code=400, detail="Davom etish uchun shartlarga rozilik bering")

    tg_user = validate_telegram_init_data(body.init_data) if body.init_data else None
    tg_id = str(tg_user["id"]) if tg_user else None

    opts = (selectinload(User.courier_profile), selectinload(User.partner_profile))
    by_phone = await find_user_by_phone(db, phone, options=opts)
    by_tg = None
    if tg_id:
        by_tg = (await db.execute(select(User).where(User.telegram_id == tg_id).options(*opts))).scalars().first()
    if by_phone and by_tg and by_phone.id != by_tg.id:
        raise HTTPException(status_code=409, detail="Bu telefon raqami boshqa hisobga biriktirilgan. Administratsiya bilan bog'laning.")

    user = by_tg or by_phone

    # Rol bo'yicha oldindan tekshiruvlar (foydalanuvchi yaratishdan OLDIN)
    if role == "client" and not user and not tg_id:
        raise HTTPException(
            status_code=400,
            detail="Mijoz sifatida ro'yxatdan o'tish uchun ilovani Telegram ichida (bot orqali) oching.",
        )
    if role == "courier":
        if body.city_id is None:
            raise HTTPException(status_code=400, detail="Ishlaydigan shaharni tanlang")
        transport = body.transport_type if body.transport_type in COURIER_TRANSPORTS else "walking"
    legal = None
    if role == "partner":
        if body.city_id is None:
            raise HTTPException(status_code=400, detail="Do'kon joylashgan shaharni tanlang")
        if not (body.brand_name or "").strip() or not (body.category or "").strip() or not (body.address or "").strip():
            raise HTTPException(status_code=400, detail="Do'kon nomi, kategoriyasi va manzilini kiriting")
        legal = _clean_legal_details(
            body.legal_name, body.stir, body.bank_account, body.mfo,
            body.bank_name, body.director_name, body.director_phone,
            legal_type=body.legal_type if body.legal_type == "self_employed" else "legal", pinfl=body.pinfl,
        )
    if body.city_id is not None:
        city = (await db.execute(select(City).where(City.id == body.city_id))).scalars().first()
        if not city:
            raise HTTPException(status_code=400, detail="Shahar topilmadi")

    if user:
        authorized = bool(tg_id and user.telegram_id == tg_id) or bool(
            user.password_hash and verify_password(body.password, user.password_hash)
        )
        if not authorized:
            raise HTTPException(
                status_code=409,
                detail="Bu telefon raqami allaqachon ro'yxatdan o'tgan. \"Kirish\" bo'limidan PIN bilan kiring.",
            )
        if role == "courier" and user.courier_profile:
            raise HTTPException(status_code=400, detail="Siz allaqachon kuryer sifatida ro'yxatdan o'tgansiz")
        if role == "partner" and user.partner_profile:
            raise HTTPException(status_code=400, detail="Siz allaqachon hamkor sifatida ro'yxatdan o'tgansiz")
    else:
        user = User(full_name=full_name, phone_number=phone, role=UserRole.CLIENT, telegram_id=tg_id)
        db.add(user)
        await db.flush()

    if not user.password_hash:
        user.password_hash = hash_password(body.password)
    if tg_id and not user.telegram_id:
        user.telegram_id = tg_id
    if body.latitude is not None and body.longitude is not None:
        user.home_latitude = body.latitude
        user.home_longitude = body.longitude
    if body.city_id is not None and user.city_id is None and role != "partner":
        user.city_id = body.city_id

    notify_text = None
    reply = {"ok": True, "role": role}
    now = datetime.utcnow()

    if role == "client":
        reply["redirect"] = "/shop"
        reply["message"] = "Ro'yxatdan o'tdingiz! Endi buyurtma berishingiz mumkin."
    elif role == "courier":
        db.add(CourierProfile(
            user_id=user.id, transport_type=transport, is_approved=False,
            application_status="pending", terms_accepted_at=now,
        ))
        reply["message"] = "Arizangiz qabul qilindi! Administratsiya tez orada siz bilan bog'lanadi."
        notify_text = (
            f"🛵 Yangi KURYER arizasi!\n\n<b>Ism:</b> {user.full_name}\n<b>Telefon:</b> {user.phone_number}\n"
            f"<b>Transport:</b> {transport}\n\nAdmin panel → Arizalar bo'limidan ko'rib chiqing va bog'laning."
        )
    else:
        db.add(PartnerProfile(
            user_id=user.id, city_id=body.city_id, brand_name=body.brand_name.strip()[:150],
            category=_norm_partner_category(body.category), address=body.address.strip()[:300],
            is_open=False, application_status="pending", terms_accepted_at=now, **legal,
        ))
        reply["message"] = "Arizangiz qabul qilindi! Administratsiya tez orada siz bilan bog'lanadi."
        notify_text = (
            f"🏪 Yangi HAMKOR arizasi!\n\n<b>Do'kon:</b> {body.brand_name.strip()}\n"
            f"<b>Turi:</b> {'O‘z-o‘zini band qilgan shaxs' if legal.get('legal_type') == 'self_employed' else 'Yuridik shaxs / YTT'}\n"
            f"<b>Nomi:</b> {html.escape(str(legal['legal_name']))}\n"
            + (f"<b>PINFL:</b> {legal.get('pinfl')}\n" if legal.get('legal_type') == 'self_employed' else f"<b>STIR:</b> {legal['stir']}\n<b>Direktor:</b> {html.escape(str(legal['director_name']))}\n")
            + f"<b>Telefon:</b> {legal['director_phone']}\n"
            f"<b>Aloqa:</b> {user.full_name}, {user.phone_number}\n\nAdmin panel → Arizalar bo'limidan ko'rib chiqing va bog'laning."
        )

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        print(f"[REGISTER XATOSI] {e!r}")
        raise HTTPException(status_code=400, detail="Ro'yxatdan o'tkazib bo'lmadi — ma'lumotlar takrorlangan bo'lishi mumkin")

    if notify_text:
        try:
            owners = (await db.execute(
                select(User).where(User.role == UserRole.OWNER, User.telegram_id.is_not(None))
            )).scalars().all()
            for o in owners:
                await send_telegram_message(o.telegram_id, notify_text)
        except Exception as e:
            print(f"Ariza bildirishnomasi xatoligi: {e}")

    if role == "client":
        request.session["user_id"] = user.id
    return reply


# ---- Brute-force'dan himoya: 4 xonali PIN uchun urinishlarni cheklash ----
_login_attempts: dict = {}


def _rate_limited(key: str, limit: int = 6, window: int = 900) -> bool:
    import time as _t
    now = _t.time()
    arr = [t for t in _login_attempts.get(key, []) if now - t < window]
    _login_attempts[key] = arr
    return len(arr) >= limit


def _rate_hit(key: str) -> None:
    import time as _t
    _login_attempts.setdefault(key, []).append(_t.time())


class ClientLoginBody(BaseModel):
    phone_number: str
    password: str


@app_api_router.post("/client-login")
async def app_client_login(body: ClientLoginBody, request: Request, db: AsyncSession = Depends(get_db)):
    """Mustaqil mijoz ilovasi (PWA / .exe) uchun: telefon + PIN → token.
    PIN botda o'rnatiladi (🔑 «Ilova uchun PIN») — shu bilan telefon raqami Telegram orqali tasdiqlangan bo'ladi."""
    phone = normalize_phone(body.phone_number)
    ip = request.client.host if request.client else "?"
    keys = [f"p:{phone_tail(phone)}", f"i:{ip}"]
    if any(_rate_limited(k) for k in keys):
        raise HTTPException(status_code=429, detail="Juda ko'p urinish. 15 daqiqadan keyin qayta urinib ko'ring.")
    user = await find_user_by_phone(db, phone)
    if not user or not user.is_active or not user.password_hash or not verify_password(body.password or "", user.password_hash):
        for k in keys:
            _rate_hit(k)
        raise HTTPException(status_code=401, detail="Telefon raqami yoki PIN noto'g'ri. PIN yo'q bo'lsa, botda «🔑 Ilova uchun PIN» tugmasini bosing.")
    for k in keys:
        _login_attempts.pop(k, None)
    return {"ok": True, "token": make_client_token(user.id), "name": user.full_name}


@applications_router.post("/admin/applications/partner/{partner_id}/approve")
async def approve_partner_application(
    partner_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)
):
    partner = (await db.execute(
        select(PartnerProfile).options(selectinload(PartnerProfile.user)).where(PartnerProfile.id == partner_id)
    )).scalars().first()
    if not partner or partner.application_status != "pending":
        raise HTTPException(status_code=404, detail="Ariza topilmadi yoki allaqachon ko'rib chiqilgan")
    partner.application_status = "approved"
    partner.is_open = True
    await db.commit()
    try:
        if partner.user and partner.user.telegram_id:
            await send_telegram_message(
                partner.user.telegram_id,
                "✅ Hamkorlik arizangiz tasdiqlandi! Endi telefon raqamingiz va PIN kodingiz bilan hamkor kabinetiga kirishingiz mumkin.",
            )
    except Exception as e:
        print(f"Ariza bildirishnomasi xatoligi: {e}")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@applications_router.post("/admin/applications/partner/{partner_id}/reject")
async def reject_partner_application(
    partner_id: int, reason: str = Form(""), db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    partner = (await db.execute(
        select(PartnerProfile).options(selectinload(PartnerProfile.user)).where(PartnerProfile.id == partner_id)
    )).scalars().first()
    if not partner or partner.application_status != "pending":
        raise HTTPException(status_code=404, detail="Ariza topilmadi yoki allaqachon ko'rib chiqilgan")
    tg = partner.user.telegram_id if partner.user else None
    await db.delete(partner)
    await db.commit()
    try:
        if tg:
            extra = f"\nSabab: {reason.strip()}" if reason.strip() else ""
            await send_telegram_message(tg, f"❌ Hamkorlik arizangiz qabul qilinmadi.{extra}")
    except Exception as e:
        print(f"Ariza bildirishnomasi xatoligi: {e}")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@applications_router.post("/admin/applications/courier/{user_id}/approve")
async def approve_courier_application(
    user_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)
):
    user = (await db.execute(
        select(User).options(selectinload(User.courier_profile)).where(User.id == user_id)
    )).scalars().first()
    if not user or not user.courier_profile or user.courier_profile.application_status != "pending":
        raise HTTPException(status_code=404, detail="Ariza topilmadi yoki allaqachon ko'rib chiqilgan")
    user.courier_profile.application_status = "approved"
    user.courier_profile.is_approved = True
    await db.commit()
    try:
        if user.telegram_id:
            await send_telegram_message(
                user.telegram_id,
                "✅ Kuryerlik arizangiz tasdiqlandi! Telefon raqamingiz va PIN kodingiz bilan kuryer kabinetiga kiring. "
                "Naqd buyurtma olish uchun balansingizni to'ldirishni unutmang.",
            )
    except Exception as e:
        print(f"Ariza bildirishnomasi xatoligi: {e}")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@applications_router.post("/admin/applications/courier/{user_id}/reject")
async def reject_courier_application(
    user_id: int, reason: str = Form(""), db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    user = (await db.execute(
        select(User).options(selectinload(User.courier_profile)).where(User.id == user_id)
    )).scalars().first()
    if not user or not user.courier_profile or user.courier_profile.application_status != "pending":
        raise HTTPException(status_code=404, detail="Ariza topilmadi yoki allaqachon ko'rib chiqilgan")
    tg = user.telegram_id
    await db.delete(user.courier_profile)
    await db.commit()
    try:
        if tg:
            extra = f"\nSabab: {reason.strip()}" if reason.strip() else ""
            await send_telegram_message(tg, f"❌ Kuryerlik arizangiz qabul qilinmadi.{extra}")
    except Exception as e:
        print(f"Ariza bildirishnomasi xatoligi: {e}")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# ==================== 11. MIJOZ MINI APP (Telegram WebApp) ====================
# DIQQAT: bu yerdagi barcha endpointlar login/parolsiz — chunki mijoz admin
# emas. Buning o'rniga, HAR BIR so'rovda Telegram'ning initData'si
# tekshiriladi (validate_telegram_init_data) — bu, aslida, "parol" vazifasini
# bajaradi, chunki uni faqat Telegram'ning o'zi to'g'ri yarata oladi.

shop_router = APIRouter(prefix="/api/shop", tags=["Mijoz Mini App"])


class PromoCheckBody(BaseModel):
    init_data: str
    code: str


@shop_router.post("/promo/check")
async def shop_promo_check(body: PromoCheckBody, db: AsyncSession = Depends(get_db)):
    tg_user = _get_telegram_user_or_403(body.init_data)
    client = (await db.execute(
        select(User).where(_ident_cond(tg_user), User.role == UserRole.CLIENT)
    )).scalars().first()
    if not client:
        raise HTTPException(status_code=404, detail="Mijoz topilmadi")
    promo, err = await validate_promo_for_client(db, body.code, client)
    if not promo:
        raise HTTPException(status_code=400, detail=err)
    return {"code": promo.code, "percent": promo.discount_percent or 0, "amount": promo.discount_amount or 0}


@shop_router.get("/promotions")
async def shop_promotions(city_id: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    """Mini App tepasidagi banner(lar) va referal/cashback ma'lumotlarini
    qaytaradi — bularning barchasi OWNER tomonidan boshqariladi.
    Frontend (shop.html) shu manzilni chaqirib, mos joylarni chizadi."""
    setting = await _get_or_create_setting(db)
    await db.commit()  # _get_or_create_setting yangi qator yaratgan bo'lishi mumkin

    _now = datetime.utcnow()
    _bq = select(Banner).where(
        Banner.is_active == True,
        or_(Banner.starts_at.is_(None), Banner.starts_at <= _now),
        or_(Banner.ends_at.is_(None), Banner.ends_at >= _now),
    )
    # Shahar: umumiy (NULL) bannerlar hammaga; shahar bannerlari — faqat shu shahar mijoziga
    _bq = _bq.where(or_(Banner.city_id.is_(None), Banner.city_id == city_id)) if city_id is not None else _bq.where(Banner.city_id.is_(None))
    banners = (await db.execute(_bq.order_by(Banner.display_order, Banner.id))).scalars().all()

    # Dasturlar HOZIR ishlayaptimi? (owner yoqgan + shahar/muddat qoidasi yoki asosiy sozlama bo'yicha qiymat > 0)
    cb_val = await effective_bonus_value(db, "cashback", city_id, setting.cashback_earn_percent or 0.0)
    rf_val = await effective_bonus_value(db, "referral", city_id, setting.referral_bonus_amount or 0.0)
    bd_val = await effective_bonus_value(db, "birthday", city_id, setting.birthday_bonus_amount or 0.0)

    def _n(v):
        return f"{v:,.0f}".replace(",", " ") if float(v) == int(v) else f"{v:g}"
    cb_on = bool(setting.cashback_visible) and cb_val > 0
    rf_on = bool(setting.referral_visible) and rf_val > 0
    bd_on = bd_val > 0

    # Faol promo-kodlar: mijoz shahriga mos, muddati o'tmagan, limiti tugamagan — bannerda ko'rinadi
    _pq = select(PromoCode).where(
        PromoCode.is_active == True,
        or_(PromoCode.starts_at.is_(None), PromoCode.starts_at <= _now),
        or_(PromoCode.expires_at.is_(None), PromoCode.expires_at >= _now),
        or_(PromoCode.city_id.is_(None), PromoCode.city_id == city_id) if city_id is not None else PromoCode.city_id.is_(None),
    ).order_by(PromoCode.id.desc()).limit(10)
    promos = []
    for pc in (await db.execute(_pq)).scalars().all():
        if pc.max_uses and (pc.used_count or 0) >= pc.max_uses:
            continue
        if pc.discount_percent:
            disc = f"{_n(pc.discount_percent)}% chegirma"
        elif pc.discount_amount:
            disc = f"{_n(pc.discount_amount)} so'm chegirma"
        else:
            continue
        left = (pc.max_uses - (pc.used_count or 0)) if pc.max_uses else None
        promos.append({"id": pc.id, "code": pc.code, "text": disc, "percent": pc.discount_percent or 0,
                       "amount": pc.discount_amount or 0, "left": left,
                       "until": (pc.expires_at + timedelta(hours=5)).strftime("%d.%m.%Y") if pc.expires_at else None})

    return {
        "promos": promos,
        "birthday": {"active": bd_on, "amount": bd_val,
                     "text": f"Tug'ilgan kuningizda {_n(bd_val)} so'm bonus sovg'a qilamiz!" if bd_on else ""},
        "banners": [
            {
                "id": b.id,
                "title": b.title,
                "text": b.text_content,
                "image_url": f"/media/banner/{b.id}" if b.image_data else None,
                "link_url": b.link_url,
                "bg_color": b.bg_color,
                "slide_seconds": b.slide_seconds,
            }
            for b in banners
        ],
        "referral": {
            "visible": rf_on,
            "text": (setting.referral_program_text or "").strip() or (f"Do'stingizni taklif qiling — ikkalangiz {_n(rf_val)} so'm keshbek olasiz!" if rf_on else ""),
        },
        "cashback": {
            "visible": cb_on,
            "text": (setting.bonus_cashback_text or "").strip() or (f"Har bir buyurtmadan {_n(cb_val)}% keshbek qaytadi" if cb_on else ""),
        },
    }


# ---- Mustaqil ilova (PWA / .exe) uchun kirish tokeni ----
# Telegram'siz ochilgan mijoz ilovasi telefon + PIN bilan kiradi va imzolangan
# token oladi. Token shop endpointlarida `init_data` o'rnida yuboriladi ("zm1." bilan boshlanadi).
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

_CLIENT_TOKEN_MAX_AGE = 60 * 60 * 24 * 90  # 90 kun
_token_serializer = URLSafeTimedSerializer(SESSION_SECRET_KEY, salt="zoomo-client-token")


def make_client_token(user_id: int) -> str:
    return "zm1." + _token_serializer.dumps({"u": user_id})


def verify_client_token(token: str) -> Optional[int]:
    if not token or not token.startswith("zm1."):
        return None
    try:
        data = _token_serializer.loads(token[4:], max_age=_CLIENT_TOKEN_MAX_AGE)
        return int(data["u"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None


def _get_telegram_user_or_403(init_data: str) -> dict:
    """Mijozni aniqlaydi: Telegram initData YOKI mustaqil ilova tokeni ("zm1.").
    Qaytgan dict'da yo "id" (telegram id), yo "uid" (User.id) bo'ladi — _ident_cond() ishlatiladi."""
    if init_data and init_data.startswith("zm1."):
        uid = verify_client_token(init_data)
        if not uid:
            raise HTTPException(status_code=403, detail="Sessiya tugagan. Iltimos, qaytadan kiring.")
        return {"uid": uid}
    tg_user = validate_telegram_init_data(init_data)
    if not tg_user:
        raise HTTPException(status_code=403, detail="Telegram tasdiqlanmadi. Iltimos, ilovani Telegram ichidan oching.")
    return tg_user


def _ident_cond(ident: dict):
    """SQLAlchemy sharti: foydalanuvchini telegram id yoki User.id bo'yicha topish."""
    if "uid" in ident:
        return User.id == ident["uid"]
    return User.telegram_id == str(ident["id"])


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
    via_ai: Optional[bool] = None   # ZooMo AI orqali yig'ilgan buyurtma — oldindan to'lov shart


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

    result = await db.execute(
        select(User).where(_ident_cond(tg_user), User.role == UserRole.CLIENT)
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
    client.last_active_at = datetime.utcnow()
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

    result = await db.execute(
        select(User).where(_ident_cond(tg_user), User.role == UserRole.CLIENT)
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

    result = await db.execute(
        select(User).where(_ident_cond(tg_user), User.role == UserRole.CLIENT)
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

    result = await db.execute(select(User).where(_ident_cond(tg_user)))
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

    client_result = await db.execute(
        select(User).where(_ident_cond(tg_user), User.role == UserRole.CLIENT)
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
    receipt_pending_ids = set()
    if orders:
        rp = await db.execute(select(PaymentReceipt.order_id).where(
            PaymentReceipt.kind == "client_order", PaymentReceipt.status == "pending",
            PaymentReceipt.order_id.in_([o.id for o in orders]),
        ))
        receipt_pending_ids = {row[0] for row in rp.all()}

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
            "payment_method": o.payment_method,
            "payment_verified": bool(o.payment_verified),
            "needs_receipt": bool(
                o.payment_method == "p2p" and not o.payment_verified
                and o.status != OrderStatus.CANCELLED and o.id not in receipt_pending_ids
            ),
            "receipt_pending": o.id in receipt_pending_ids,
            "can_cancel": bool(
                o.payment_method == "p2p" and o.status in _CANCEL_OPEN
                and (o.payment_verified or (o.id not in receipt_pending_ids and not o.payment_receipt_file_id))
            ),
            "refund_amount": o.refund_amount or 0,
            "refund_status": o.refund_status,
            "cancel_stage": o.cancel_stage,
        }
        for o in orders
    ]


@shop_router.post("/orders/{order_id}/receipt")
async def shop_upload_receipt(
    order_id: int,
    init_data: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Mijoz P2P orqali pul o'tkazgach, chek rasmini platforma ichida yuboradi."""
    tg_user = _get_telegram_user_or_403(init_data)
    client = (await db.execute(
        select(User).where(_ident_cond(tg_user), User.role == UserRole.CLIENT)
    )).scalars().first()
    if not client:
        raise HTTPException(status_code=404, detail="Mijoz topilmadi")
    order = (await db.execute(
        select(Order).where(Order.id == order_id, Order.client_id == client.id)
    )).scalars().first()
    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    if order.payment_method != "p2p" or order.payment_verified or order.status == OrderStatus.CANCELLED:
        raise HTTPException(status_code=400, detail="Bu buyurtma uchun chek yuklash shart emas")
    total = (order.total_price or 0) + (order.delivery_fee or 0) - (order.discount_amount or 0)
    receipt = await _create_receipt(
        db, kind="client_order", user=client, amount=max(total, MIN_TOPUP_AMOUNT), file=file, order_id=order.id,
    )
    return {"ok": True, "receipt_id": receipt.id}


@shop_router.get("/orders/{order_id}/track")
async def shop_order_track(order_id: int, init_data: str, db: AsyncSession = Depends(get_db)):
    """Mijoz 'Yo'lda' bo'lgan buyurtmasini JONLI kuzatishi uchun — kuryerning
    hozirgi GPS koordinatasini qaytaradi. Frontend (shop.html) bu manzilga
    har 5-8 sekundda so'rov yuborib, xaritadagi kuryer belgisini yangilab
    turadi. Buyurtma boshqa mijozniki bo'lsa ko'rsatilmaydi."""
    tg_user = _get_telegram_user_or_403(init_data)

    client_result = await db.execute(select(User).where(_ident_cond(tg_user)))
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
        "delivery_location": (
            {"lat": order.delivery_latitude, "lng": order.delivery_longitude}
            if order.delivery_latitude is not None and order.delivery_longitude is not None else None
        ),
    }



# ==================== REYTING BO'YICHA TARTIB ====================
# Hamkor: bahosi yuqori do'kon mijoz ro'yxatida yuqorida chiqadi (admin sozlamalardan o'chirib-yoqadi).
# Kuryer: buyurtma taklif qilish tartibi reytingga BOG'LIQ EMAS; yuqori reytingli kuryerga admin shaharlar kesimida BONUS belgilaydi.
RP_PRIOR_STARS, RP_PRIOR_W = 4.0, 5


def _bayes(avg, n, prior=RP_PRIOR_STARS, w=RP_PRIOR_W) -> float:
    n = n or 0
    return ((float(avg or 0) * n) + prior * w) / (n + w)




@shop_router.get("/partners")
async def shop_partners(city_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PartnerProfile).where(PartnerProfile.city_id == city_id, PartnerProfile.is_open == True)
    )
    partners = result.scalars().all()
    rows = (await db.execute(select(OrderRating.partner_id, func.avg(OrderRating.partner_stars), func.count(OrderRating.partner_stars))
                             .where(OrderRating.partner_stars.isnot(None), OrderRating.partner_id.in_([p.id for p in partners] or [0]))
                             .group_by(OrderRating.partner_id))).all()
    rt = {pid: (float(a), n) for pid, a, n in rows}
    setting = await _get_or_create_setting(db)
    out = [{"id": p.id, "name": p.brand_name, "category": _norm_partner_category(p.category), "address": p.address,
            "rating": round(rt[p.id][0], 1) if p.id in rt else None, "rating_count": rt[p.id][1] if p.id in rt else 0}
           for p in partners]
    if bool(getattr(setting, "rating_priority_enabled", True)):
        out.sort(key=lambda d: (-_bayes(rt[d["id"]][0], rt[d["id"]][1], w=3) if d["id"] in rt else -RP_PRIOR_STARS, d["name"].lower()))
    return out


class DeliveryQuoteBody(BaseModel):
    init_data: str
    partner_id: int
    lat: Optional[float] = None
    lng: Optional[float] = None


@shop_router.post("/delivery-quote")
async def shop_delivery_quote(body: DeliveryQuoteBody, db: AsyncSession = Depends(get_db)):
    """Savatda mijozga yetkazish narxini (masofa bo'yicha) ko'rsatish uchun.
    Buyurtma yaratilganda narx baribir serverda QAYTA hisoblanadi."""
    _get_telegram_user_or_403(body.init_data)
    setting = await _get_or_create_setting(db)
    partner = (await db.execute(select(PartnerProfile).where(PartnerProfile.id == body.partner_id))).scalars().first()
    if not partner:
        raise HTTPException(status_code=404, detail="Do'kon topilmadi")
    km = _road_km(setting, partner.latitude, partner.longitude, body.lat, body.lng)
    fee = compute_delivery_fee(setting, km)
    return {
        "delivery_fee": fee,
        "distance_km": km,
        "by_distance": bool(getattr(setting, "km_pricing_enabled", False) and km is not None),
        "km_pricing_enabled": bool(getattr(setting, "km_pricing_enabled", False)),
        "base_radius": setting.km_base_radius,
        "base_price": setting.km_base_price,
        "extra_price": setting.km_extra_price,
    }


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

    client_result = await db.execute(
        select(User).where(_ident_cond(tg_user), User.role == UserRole.CLIENT)
    )
    client = client_result.scalars().first()
    if not client:
        raise HTTPException(status_code=404, detail="Mijoz topilmadi — avval botga /start bosing")

    if not body.items:
        raise HTTPException(status_code=400, detail="Savat bo'sh")

    partner_result = await db.execute(select(PartnerProfile).where(PartnerProfile.id == body.partner_id))
    partner = partner_result.scalars().first()
    if not partner or partner.application_status == "pending":
        raise HTTPException(status_code=404, detail="Do'kon topilmadi")
    if partner.is_open is False:
        raise HTTPException(status_code=400, detail="Do'kon hozir yopiq — keyinroq urinib ko'ring")
    if not (body.delivery_address or "").strip():
        raise HTTPException(status_code=400, detail="Yetkazish manzilini kiriting")

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
        if not product.is_available:
            raise HTTPException(status_code=400, detail=f"«{product.name}» hozir mavjud emas")
        if item.quantity > 99:
            raise HTTPException(status_code=400, detail="Bitta mahsulotdan 99 tadan ko'p buyurtma qilib bo'lmaydi")
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
    if (body.promo_code or "").strip():
        promo_code_obj, _perr = await validate_promo_for_client(db, body.promo_code, client, lock=True)
        if not promo_code_obj:
            raise HTTPException(status_code=400, detail=_perr)
        if promo_code_obj.discount_percent:
            discount_amount = total_price * (promo_code_obj.discount_percent / 100)
        elif promo_code_obj.discount_amount:
            discount_amount = min(promo_code_obj.discount_amount, total_price)

    # ---- KESHBEKNI ISHLATISH (mavjud balansdan ko'p ishlatib bo'lmaydi) ----
    cashback_used = 0.0
    if body.use_cashback and (client.cashback_balance or 0) > 0:
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

    # ---- MASOFA (KM) BO'YICHA NARX: hamkor -> mijoz ----
    distance_km = None
    courier_km_bonus = 0.0
    if (body.order_type or "delivery") == "delivery":
        distance_km = _road_km(setting, partner.latitude, partner.longitude, delivery_lat, delivery_lng)
        if distance_km is not None:
            delivery_fee = compute_delivery_fee(setting, distance_km)
            courier_km_bonus = compute_courier_km_bonus(setting, distance_km)

    payment_method = body.payment_method or "cash"
    if body.via_ai:
        # AI buyurtma: to'lov (karta orqali) tasdiqlangandan KEYIN hamkorga boradi — qoida server tomonida majburiy
        payment_method = "p2p"

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
        distance_km=distance_km,
        courier_km_bonus=courier_km_bonus,
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

    telegram_id = client.telegram_id  # mustaqil ilova mijozida Telegram bo'lmasligi mumkin
    try:
        if not telegram_id:
            pass  # bildirishnoma yuboriladigan joy yo'q — ilovaning o'zida ko'rsatiladi
        elif payment_method == "p2p" and p2p_card:
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


def _touch_active(user, db_session=None) -> bool:
    """Oxirgi faollikni belgilaydi (har 10 daqiqada bir marta — jonli yangilanishda bazani ortiqcha yozmaslik uchun)."""
    now = datetime.utcnow()
    la = getattr(user, "last_active_at", None)
    if la is None or (now - la).total_seconds() > 600:
        user.last_active_at = now
        return True
    return False


@partner_router.get("", response_class=HTMLResponse)
async def partner_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    partner = await _get_own_partner(db, current_user)
    if _touch_active(current_user):
        await db.commit()

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
            "ai_on": _ai_flags(await _get_or_create_setting(db))["partner"],
            "ai_help_on": _ai_flags(await _get_or_create_setting(db))["help"],
            "ai_led_on": bool(_ai_flags(await _get_or_create_setting(db))["led"] and partner.led_allowed),
            "banner_ads_on": bool(partner.banner_allowed),
            "category_suggestions": list(dict.fromkeys(PARTNER_CATEGORIES + [c for c in (await db.execute(select(PartnerProfile.category).where(PartnerProfile.category.isnot(None)).distinct().limit(60))).scalars().all() if c])),
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


@partner_router.get("/cards", dependencies=[Depends(secure_action)])
async def partner_list_cards(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    result = await db.execute(
        select(Card).where(Card.user_id == current_user.id, Card.is_active == True).order_by(Card.created_at.desc())
    )
    return {"cards": [_card_to_dict(c) for c in result.scalars().all()]}


@partner_router.post("/cards", dependencies=[Depends(secure_action)])
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


@partner_router.post("/cards/{card_id}/delete", dependencies=[Depends(secure_action)])
async def partner_delete_card(
    card_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    await _delete_card_for_user(db, current_user.id, card_id)
    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


@partner_router.post("/withdraw", dependencies=[Depends(secure_action)])
async def partner_request_withdrawal(
    amount: float = Form(..., allow_inf_nan=False),
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

    available = (partner.balance or 0) - (partner.frozen_balance or 0)
    if amount > available:
        raise HTTPException(
            status_code=400,
            detail=f"Yechib olish uchun mavjud mablag' yetarli emas (mavjud: {available:,.0f} so'm)",
        )

    partner.frozen_balance = (partner.frozen_balance or 0) + amount
    db.add(WithdrawalRequest(partner_id=partner.id, card_id=card_id, amount=amount, status=WithdrawalStatus.PENDING))
    await db.commit()
    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


@partner_router.post("/products/create")
async def partner_create_product(
    name: str = Form(...),
    price: float = Form(..., allow_inf_nan=False),
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
    price: float = Form(..., allow_inf_nan=False),
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


@partner_router.post("/settings/update", dependencies=[Depends(secure_action)])
async def partner_update_settings(
    brand_name: str = Form(...),
    address: str = Form(...),
    category: Optional[str] = Form(None),
    latitude: Optional[float] = Form(None, allow_inf_nan=False),
    longitude: Optional[float] = Form(None, allow_inf_nan=False),
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
    if category is not None and category.strip():
        partner.category = _norm_partner_category(category)
    if latitude is not None and longitude is not None:
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise HTTPException(status_code=400, detail="Joylashuv koordinatasi noto'g'ri")
        partner.latitude = latitude
        partner.longitude = longitude
    if notification_sound in ("chime1", "chime2", "chime3"):
        partner.notification_sound = notification_sound
    await db.commit()
    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


def _clean_digits(value: Optional[str], length: int, label: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) != length:
        raise HTTPException(status_code=400, detail=f"{label} aynan {length} xonali raqam bo'lishi kerak")
    return digits


def _clean_legal_details(
    legal_name: Optional[str], stir: Optional[str], bank_account: Optional[str], mfo: Optional[str],
    bank_name: Optional[str], director_name: Optional[str], director_phone: Optional[str],
    legal_type: Optional[str] = "legal", pinfl: Optional[str] = None,
) -> dict:
    """Hamkor yuridik rekvizitlarini tekshiradi va tozalab qaytaradi.
    legal_type='self_employed' — o'z-o'zini band qilgan shaxs: F.I.Sh. + JSHSHIR (PINFL) + hisob raqami."""
    legal_name = (legal_name or "").strip()
    bank_name = (bank_name or "").strip()
    director_name = (director_name or "").strip()
    if legal_type == "self_employed":
        if not legal_name:
            raise HTTPException(status_code=400, detail="F.I.Sh. ni kiriting")
        if not bank_name:
            raise HTTPException(status_code=400, detail="Bank nomini kiriting")
        phone = normalize_phone(director_phone or "")
        if not re.fullmatch(r"\+998\d{9}", phone or ""):
            raise HTTPException(status_code=400, detail="Telefon raqami noto'g'ri (masalan: +998901234567)")
        mfo_d = re.sub(r"\D", "", mfo or "")
        return {
            "legal_type": "self_employed",
            "legal_name": legal_name[:200],
            "pinfl": _clean_digits(pinfl, 14, "JSHSHIR (PINFL)"),
            "stir": None,
            "bank_account": _clean_digits(bank_account, 20, "Hisob-kitob raqami"),
            "mfo": _clean_digits(mfo_d, 5, "MFO") if mfo_d else None,
            "bank_name": bank_name[:120],
            "director_name": legal_name[:120],
            "director_phone": phone,
        }
    if not legal_name:
        raise HTTPException(status_code=400, detail="Yuridik nomini kiriting (masalan: 'Gourmet Express' MChJ yoki 'Aliyev B.' YTT)")
    if not bank_name:
        raise HTTPException(status_code=400, detail="Bank nomini kiriting")
    if not director_name:
        raise HTTPException(status_code=400, detail="Direktor/rahbar ismini kiriting")
    phone = normalize_phone(director_phone or "")
    if not re.fullmatch(r"\+998\d{9}", phone or ""):
        raise HTTPException(status_code=400, detail="Direktor telefon raqami noto'g'ri (masalan: +998901234567)")
    return {
        "legal_type": "legal",
        "pinfl": None,
        "legal_name": legal_name[:200],
        "stir": _clean_digits(stir, 9, "STIR (INN)"),
        "bank_account": _clean_digits(bank_account, 20, "Hisob-kitob raqami"),
        "mfo": _clean_digits(mfo, 5, "MFO"),
        "bank_name": bank_name[:120],
        "director_name": director_name[:120],
        "director_phone": phone,
    }


@partner_router.post("/legal", dependencies=[Depends(secure_action)])
async def partner_update_legal(
    legal_name: str = Form(""),
    stir: str = Form(""),
    bank_account: str = Form(""),
    mfo: str = Form(""),
    bank_name: str = Form(""),
    director_name: str = Form(""),
    director_phone: str = Form(""),
    legal_type: str = Form("legal"),
    pinfl: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    """Hamkor yuridik rekvizitlari (STIR, hisob raqami, MFO, bank, direktor) yoki o'z-o'zini band qilgan shaxs (PINFL)."""
    partner = await _get_own_partner(db, current_user)
    data = _clean_legal_details(legal_name, stir, bank_account, mfo, bank_name, director_name, director_phone,
                                legal_type="self_employed" if legal_type == "self_employed" else "legal", pinfl=pinfl)
    for k, v in data.items():
        setattr(partner, k, v)
    await db.commit()
    return RedirectResponse(url="/partner", status_code=status.HTTP_303_SEE_OTHER)


@partner_router.get("/legal/data", dependencies=[Depends(secure_action)])
async def partner_legal_data(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_partner_user)):
    """To'liq rekvizitlar — FAQAT maxfiy javob tasdiqlangandan keyin (sahifada ular yashirin turadi)."""
    p = await _get_own_partner(db, current_user)
    return {"legal_type": p.legal_type or "legal", "legal_name": p.legal_name or "", "stir": p.stir or "", "pinfl": p.pinfl or "",
            "bank_account": p.bank_account or "", "mfo": p.mfo or "", "bank_name": p.bank_name or "",
            "director_name": p.director_name or "", "director_phone": p.director_phone or ""}


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
    if old_status in (OrderStatus.CANCELLED, OrderStatus.DELIVERED):
        raise HTTPException(status_code=400, detail="Bu buyurtma yakunlangan (bekor qilingan yoki yetkazilgan) — holatini o'zgartirib bo'lmaydi")
    try:
        order.status = OrderStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Noto'g'ri holat")
    _staff_cancelled = False
    if order.status == OrderStatus.CANCELLED:
        _staff_cancelled = await _apply_staff_cancel(db, order, "by_partner")
    await db.commit()
    if _staff_cancelled:
        await _notify_staff_cancel(db, order)

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
    if _touch_active(current_user):
        await db.commit()
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
        .options(selectinload(Order.items), selectinload(Order.partner), selectinload(Order.client))
        .where(Order.courier_id == current_user.id, Order.status == OrderStatus.ON_THE_WAY)
        .order_by(Order.created_at)
    )
    my_active_orders = (await db.execute(my_active_stmt)).scalars().all()
    # Navigator uchun: do'kon egasining telefoni (kuryer do'konga qo'ng'iroq qilishi uchun)
    _p_uids = [o.partner.user_id for o in my_active_orders if o.partner and o.partner.user_id]
    partner_phones = {}
    if _p_uids:
        for u in (await db.execute(select(User).where(User.id.in_(_p_uids)))).scalars().all():
            partner_phones[u.id] = u.phone_number

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

    # ---- PREPAID BALANS: har buyurtma uchun kerakli summa va yetish-yetmasligi ----
    setting = await _get_or_create_setting(db)
    courier_pct = (setting.courier_share_percent or 0) / 100
    reserved = await _courier_reserved_hold(db, current_user.id, courier_pct)
    free_balance = ((courier_profile.balance if courier_profile else 0) or 0) - reserved
    order_holds = {o.id: _courier_cash_hold(o, courier_pct) for o in available_orders}
    # ---- Har buyurtma uchun kuryer daromadi: ulush + masofa (km) uchun qo'shimcha haqlar ----
    order_calc = {}
    _rb_now = await courier_rating_bonus_for(db, current_user.id)
    for o in list(my_active_orders) + list(available_orders):
        _rb = o.courier_rating_bonus if o.courier_rating_bonus is not None else _rb_now
        base = (o.delivery_fee or 0.0) * courier_pct
        dist = o.distance_km
        if dist is None and o.partner:
            dist = _road_km(setting, o.partner.latitude, o.partner.longitude, o.delivery_latitude, o.delivery_longitude)
        d_bonus = o.courier_km_bonus or 0.0
        if not d_bonus and dist is not None and o.status == OrderStatus.LOOKING_FOR_COURIER:
            d_bonus = compute_courier_km_bonus(setting, dist)
        pk = o.pickup_distance_km
        p_bonus = o.courier_pickup_bonus or 0.0
        estimated = False
        if pk is None and o.partner and courier_profile:
            pk = _road_km(setting, courier_profile.latitude, courier_profile.longitude, o.partner.latitude, o.partner.longitude)
            if pk is not None:
                p_bonus = compute_courier_pickup_bonus(setting, pk)
                estimated = True
        order_calc[o.id] = {
            "base": round(base), "dist_km": dist, "delivery_bonus": round(d_bonus),
            "pickup_km": pk, "pickup_bonus": round(p_bonus), "pickup_estimated": estimated,
            "rating_bonus": round(_rb or 0),
            "total": round(base + d_bonus + p_bonus + (_rb or 0)),
        }
    topup_card = await _get_topup_card_info(db)
    my_receipts = (await db.execute(
        select(PaymentReceipt).options(selectinload(PaymentReceipt.user), selectinload(PaymentReceipt.partner))
        .where(PaymentReceipt.user_id == current_user.id, PaymentReceipt.kind == "courier_topup")
        .order_by(PaymentReceipt.created_at.desc()).limit(10)
    )).scalars().all()
    my_receipts = [_receipt_to_dict(r) for r in my_receipts]

    return templates.TemplateResponse(
        request=request,
        name="courier.html",
        context={
            "current_user": current_user,
            "courier_profile": courier_profile,
            "available_orders": available_orders,
            "order_holds": order_holds,
            "order_calc": order_calc,
            "free_balance": free_balance,
            "reserved_hold": reserved,
            "topup_card": topup_card,
            "my_receipts": my_receipts,
            "MIN_TOPUP_AMOUNT": MIN_TOPUP_AMOUNT,
            "my_active_orders": my_active_orders,
            "partner_phones": partner_phones,
            "courier_pct": courier_pct,
            "ai_on": _ai_flags(setting)["courier"],
            "ai_help_on": _ai_flags(setting)["help"],
            "ai_led_on": _ai_flags(setting)["led"] and not (courier_profile is not None and courier_profile.led_enabled is False),
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
    lat: float = Form(..., allow_inf_nan=False),
    lng: float = Form(..., allow_inf_nan=False),
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

    # Balansi yetmaydigan naqd buyurtmalar uchun signal chalinmasin.
    prof_res = await db.execute(select(CourierProfile).where(CourierProfile.user_id == current_user.id))
    prof = prof_res.scalars().first()
    if ids:
        st = await _get_or_create_setting(db)
        pct = (st.courier_share_percent or 0) / 100
        reserved = await _courier_reserved_hold(db, current_user.id, pct)
        free = ((prof.balance if prof else 0) or 0) - reserved
        ords = (await db.execute(select(Order).where(Order.id.in_(ids)))).scalars().all()
        ids = [o.id for o in ords if _courier_cash_hold(o, pct) <= free]

    my_active_stmt = select(Order.id).where(
        Order.courier_id == current_user.id, Order.status == OrderStatus.ON_THE_WAY
    )
    my_ids = [row[0] for row in (await db.execute(my_active_stmt)).all()]

    return JSONResponse({"available_ids": ids, "my_active_ids": my_ids})


@courier_router_app.get("/legal/data", dependencies=[Depends(secure_action)])
async def courier_legal_data(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_courier_user)):
    cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == current_user.id))).scalars().first()
    return {"se_name": (cp.se_name if cp else "") or "", "pinfl": (cp.pinfl if cp else "") or "",
            "se_bank_name": (cp.se_bank_name if cp else "") or "", "se_account": (cp.se_account if cp else "") or ""}


@courier_router_app.post("/legal", dependencies=[Depends(secure_action)])
async def courier_update_legal(
    se_name: str = Form(""),
    pinfl: str = Form(""),
    se_bank_name: str = Form(""),
    se_account: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    """Kuryer — o'z-o'zini band qilgan shaxs rekvizitlari (to'lovlar uchun)."""
    cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == current_user.id))).scalars().first()
    if not cp:
        raise HTTPException(status_code=404, detail="Kuryer profili topilmadi")
    name = (se_name or "").strip()
    bank = (se_bank_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="F.I.Sh. ni kiriting")
    if not bank:
        raise HTTPException(status_code=400, detail="Bank nomini kiriting")
    cp.se_name = name[:150]
    cp.pinfl = _clean_digits(pinfl, 14, "JSHSHIR (PINFL)")
    cp.se_bank_name = bank[:120]
    cp.se_account = _clean_digits(se_account, 20, "Hisob-kitob raqami")
    await db.commit()
    return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)


@courier_router_app.get("/cards", dependencies=[Depends(secure_action)])
async def courier_list_cards(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    result = await db.execute(
        select(Card).where(Card.user_id == current_user.id, Card.is_active == True).order_by(Card.created_at.desc())
    )
    return {"cards": [_card_to_dict(c) for c in result.scalars().all()]}


@courier_router_app.post("/cards", dependencies=[Depends(secure_action)])
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


@courier_router_app.post("/cards/{card_id}/delete", dependencies=[Depends(secure_action)])
async def courier_delete_card(
    card_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    await _delete_card_for_user(db, current_user.id, card_id)
    return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)


@courier_router_app.post("/withdraw", dependencies=[Depends(secure_action)])
async def courier_request_withdrawal(
    amount: float = Form(..., allow_inf_nan=False),
    card_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    """Kuryerning 'balance' maydoni — uning platformadagi hisobi (musbat =
    mablag' bor). Yechish mumkin: balance - frozen_balance. So'ralgan summa
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

    available = (courier_profile.balance or 0) - (courier_profile.frozen_balance or 0)
    if available <= 0:
        raise HTTPException(status_code=400, detail="Hozircha yechib olish uchun mavjud mablag' yo'q")
    if amount > available:
        raise HTTPException(
            status_code=400,
            detail=f"Yechib olish uchun mavjud mablag' yetarli emas (mavjud: {available:,.0f} so'm)",
        )

    courier_profile.frozen_balance = (courier_profile.frozen_balance or 0) + amount
    db.add(WithdrawalRequest(user_id=current_user.id, card_id=card_id, amount=amount, status=WithdrawalStatus.PENDING))
    await db.commit()
    return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)


@courier_router_app.post("/topup")
async def courier_topup_receipt(
    amount: float = Form(..., allow_inf_nan=False),
    comment: str = Form(""),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    """Kuryer admin kartasiga pul o'tkazgach, chek rasmini shu yerga yuklaydi —
    admin/operator ko'rib tasdiqlagach, balansi to'ldiriladi."""
    await _create_receipt(db, kind="courier_topup", user=current_user, amount=amount, file=file, comment=comment)
    return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)


@courier_router_app.post("/orders/{order_id}/accept")
async def courier_accept_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    courier_profile_result = await db.execute(
        select(CourierProfile).where(CourierProfile.user_id == current_user.id).with_for_update()
    )
    courier_profile = courier_profile_result.scalars().first()
    if courier_profile and courier_profile.is_blocked:
        raise HTTPException(
            status_code=403,
            detail="Siz administrator tomonidan vaqtincha bloklangansiz — yangi buyurtma qabul qila olmaysiz. Administratorga murojaat qiling.",
        )

    order_result = await db.execute(
        select(Order).where(Order.id == order_id, Order.status == OrderStatus.LOOKING_FOR_COURIER).with_for_update()
    )
    order = order_result.scalars().first()
    if not order:
        raise HTTPException(status_code=404, detail="Bu buyurtma allaqachon boshqa kuryer tomonidan olingan")

    if order.courier_id is not None:
        raise HTTPException(status_code=400, detail="Bu buyurtmani allaqachon boshqa kuryer oldi")

    # NAQD buyurtma: balans yetishi shart (yo'ldagi boshqa naqd buyurtmalar band qilgan summa ayiriladi).
    setting = await _get_or_create_setting(db)
    courier_pct = (setting.courier_share_percent or 0) / 100
    await _apply_pickup_bonus(db, order, current_user.id, setting)
    need = _courier_cash_hold(order, courier_pct)
    if need > 0:
        reserved = await _courier_reserved_hold(db, current_user.id, courier_pct)
        free = ((courier_profile.balance if courier_profile else 0) or 0) - reserved
        if free < need:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Balansingiz yetarli emas: bu naqd buyurtma uchun kamida {need:,.0f} so'm kerak, "
                    f"sizda {max(free, 0):,.0f} so'm bor. Avval balansingizni to'ldiring (admin kartasiga o'tkazib, chekni yuklang)."
                ),
            )

    order.courier_id = current_user.id
    order.status = OrderStatus.ON_THE_WAY
    order.on_the_way_at = datetime.utcnow()
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
        .with_for_update(of=Order)
    )
    order = order_result.scalars().first()
    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")

    if order.status == OrderStatus.DELIVERED:
        return RedirectResponse(url="/courier", status_code=status.HTTP_303_SEE_OTHER)
    if order.status != OrderStatus.ON_THE_WAY:
        raise HTTPException(status_code=400, detail="Faqat 'Yo'lda' holatidagi buyurtmani yetkazildi deb belgilash mumkin")
    _assert_can_deliver(order)

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

# ==================== ZOOMO AI (aqlli yordamchi) ====================
# Tashqi AI xizmatisiz, qoida va tarixiy ma'lumotlar asosida ishlaydi (kalit/pul kerak emas).
# Admin umumiy yoki rollar bo'yicha (mijoz / kuryer / hamkor) yoqadi va o'chiradi.
# O'chiq bo'lsa API 403 qaytaradi, panellarda bo'lim ko'rinmaydi.

def _ai_flags(setting) -> dict:
    master = bool(getattr(setting, "ai_enabled", True)) if setting is not None else True

    def f(name):
        v = getattr(setting, name, True) if setting is not None else True
        return master and (True if v is None else bool(v))
    return {"client": f("ai_client_enabled"), "courier": f("ai_courier_enabled"), "partner": f("ai_partner_enabled"),
            "dispatcher": f("ai_dispatcher_enabled"), "led": f("ai_led_enabled"),
            "help": f("ai_help_enabled"), "campaigns": f("ai_campaigns_enabled")}


async def _ai_require(db: AsyncSession, role: str) -> SystemSetting:
    setting = await _get_or_create_setting(db)
    if not _ai_flags(setting).get(role):
        raise HTTPException(status_code=403, detail="ZooMo AI hozircha o'chirilgan")
    return setting


def _ai_norm(s: str) -> str:
    s = (s or "").lower()
    for ch in ("’", "‘", "ʻ", "ʼ", "`", "´"):
        s = s.replace(ch, "'")
    return s


_AI_QTY_WORDS = {
    "bitta": 1, "bir": 1, "ikkita": 2, "ikki": 2, "uchta": 3, "uch": 3, "to'rtta": 4, "tortta": 4,
    "beshta": 5, "besh": 5, "oltita": 6, "yettita": 7, "sakkizta": 8, "to'qqizta": 9, "o'nta": 10,
    "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
}
_AI_STOP = {
    "aka", "bilan", "bitta", "ikkita", "uchta", "menga", "kerak", "olib", "ber", "bering", "iltimos",
    "yegim", "kelyapti", "nimadir", "judayam", "bugun", "yana", "qo'shib", "ham", "uchun", "va",
    "xohlayman", "istayman", "buyurtma", "bergin", "beray", "shu", "shunday", "juda", "kecha",
    "хочу", "мне", "дайте", "пожалуйста", "please", "want", "need",
}
_AI_ALIASES = {"kola": ["kola", "cola", "кола"], "pitsa": ["pitsa", "pizza", "пицца"], "lavash": ["lavash", "лаваш"],
               "burger": ["burger", "бургер"], "choy": ["choy", "chay", "чай", "tea"], "kofe": ["kofe", "coffee", "кофе"],
               "sho'rva": ["sho'rva", "shorva", "sup", "суп"], "non": ["non", "хлеб"]}
_AI_DRINK = ["kola", "cola", "sok", "limonad", "suv", "fanta", "pepsi", "choy", "ichimlik", "напит", "сок", "вода", "drink"]
_AI_INTENTS = [
    ("health", "Shamollash va og'riq uchun",
     ["tomog", "shamoll", "yo'tal", "yotal", "isitma", "kasal", "bosh og'ri", "bosh o'g'ri", "gripp", "горл", "простуд", "sore throat", "cold"],
     ["sho'rva", "shorva", "limon", "choy", "asal", "mastava", "sup", "bulyon", "imbir", "суп", "чай", "tea", "soup"]),
    ("mood", "Kayfiyatni ko'taradigan shirinliklar",
     ["charcha", "horg'in", "holsiz", "kayfiyat", "xafa", "stress", "shirin", "tatli", "устал", "сладк", "sweet", "tired"],
     ["shirin", "tort", "desert", "pirojn", "shokolad", "muzqaymoq", "kofe", "donut", "baklava", "cake", "торт", "кофе", "coffee", "dessert"]),
    ("light", "Yengil va sog'lom variantlar",
     ["yengil", "salat", "parhez", "diet", "sog'lom", "легк", "light"],
     ["salat", "sho'rva", "yogurt", "meva", "grill", "sabzavot", "салат", "salad"]),
    ("cold", "Chanqoqni bosadigan ichimliklar",
     ["chanq", "sovuq", "muzdek", "issiq kun", "жажд", "thirst"], _AI_DRINK),
    ("hearty", "To'yimli taomlar",
     ["och ", "ochiqdim", "to'yim", "qorin", "голод", "hungry"],
     ["osh", "lag'mon", "kebab", "shashlik", "burger", "lavash", "pitsa", "pizza", "somsa", "манты", "manti"]),
    ("spicy", "Achchiq taomlar", ["achchiq", "spicy", "остр"], ["achchiq", "spicy", "острый", "lag'mon", "chili"]),
]


class AIAssistBody(BaseModel):
    init_data: str
    query: str
    alts: Optional[List[str]] = None      # ovozdan aniqlangan muqobil variantlar
    lat: Optional[float] = None
    lng: Optional[float] = None


# ---- AI qidiruv: ovoz xatolariga chidamli "kalit" shakli ----
_AI_TR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i", "й": "y",
          "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
          "х": "h", "ц": "s", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
          "ў": "o", "қ": "k", "ғ": "g", "ҳ": "h"}


def _ai_key(s: Optional[str]) -> str:
    """Matnni solishtirish uchun bir xil shaklga keltiradi: kirill→lotin, apostroflar olib tashlanadi
    (sho'rva=shorva, o'g'ri=ogri), q=k, x=h, w=v. Shunda ovozdan kirillcha chiqsa ham, imlo biroz boshqacha bo'lsa ham mos keladi."""
    s = (s or "").lower()
    for ch in ("’", "‘", "ʻ", "ʼ", "`", "´", "'"):
        s = s.replace(ch, "")
    s = "".join(_AI_TR.get(c, c) for c in s)
    return s.translate({ord("q"): "k", ord("x"): "h", ord("w"): "v"})


def _ai_words(s: str) -> list:
    return re.findall(r"[a-z0-9]+", _ai_key(s))


def _ai_stem(w: str) -> str:
    return w[:max(4, len(w) - 2)] if len(w) > 5 else w


_AI_STOP_K = {_ai_key(x) for x in _AI_STOP} | {"kerak", "kerakli", "mollari", "molla", "narsa", "narsalar", "istardim", "ayting",
                                              "topib", "bering", "ber", "menga", "bor", "mi", "emas"}
_AI_QTY_K = {_ai_key(k): v for k, v in _AI_QTY_WORDS.items()}
_AI_ALIASES_K = {_ai_key(k): [_ai_key(x) for x in v] for k, v in _AI_ALIASES.items()}
# Yaqin ma'noli so'zlar guruhlari: mijoz bittasini aytsa, hammasi qidiriladi (yuz/malham → krem, losyon...)
_AI_SYN = [
    ["yuz", "teri", "malham", "krem", "losyon", "maska", "serum", "kosmetika", "parvarish", "surt", "surish", "nam", "gel", "balzam", "maz"],
    ["soch", "shampun", "konditsioner", "balzam", "maska", "boyok"],
    ["dori", "dorixona", "apteka", "tabletka", "kapsula", "vitamin", "sirop", "tomchi", "malham", "plastir", "bint", "termometr", "ogrik", "ogri", "kasal"],
    ["kurilish", "tamir", "remont", "sement", "kraska", "boyok", "mih", "plitka", "laminat", "parket", "santehnika", "asbob", "instrument", "kabel", "rozetka", "lampa", "truba", "kley", "shpatlevka", "elektr"],
    ["tozala", "yuvish", "kir", "poroshok", "sovun", "shvabra", "detergent", "salfetka", "gel"],
    ["bola", "bolalar", "chakalok", "pampers", "taglik", "smes", "pyure"],
    ["ozik", "ovkat", "produkt", "bakkal", "supermarket", "market", "ichimlik", "meva", "sabzavot", "go'sht", "gosht", "sut", "non"],
    ["kafe", "restoran", "yemakhona", "taom", "ovkat", "fast", "osh", "lavash", "pitsa", "burger"],
]
_AI_SYN = [[_ai_key(w) for w in g] for g in _AI_SYN]
_AI_INTENTS_EXTRA = [
    ("skin", "Yuz va teri parvarishi uchun",
     ["yuz", "teri", "malham", "krem", "kosmetik", "losyon", "maska", "serum", "soch", "shampun", "parfyum", "atir", "surt"],
     ["krem", "malham", "losyon", "maska", "serum", "gel", "kosmetik", "teri", "yuz", "shampun", "balzam", "parfyum", "soch"]),
    ("pharma", "Dorixonadan kerakli vositalar",
     ["dori", "dorixona", "apteka", "tabletka", "vitamin", "bint", "plastir", "termometr", "antibiotik", "sirop", "burun", "allergiya", "bosim", "kasal", "ogri", "ogrik"],
     ["dori", "tabletka", "vitamin", "sirop", "dorixona", "apteka", "plastir", "bint", "termometr", "tomchi", "kapsula"]),
    ("build", "Qurilish va ta'mirlash uchun",
     ["kurilish", "tamir", "remont", "sement", "kraska", "boyok", "mih", "plitka", "laminat", "santehnika", "asbob", "instrument", "kabel", "rozetka", "lampa", "truba", "kley"],
     ["kurilish", "sement", "kraska", "boyok", "mih", "plitka", "laminat", "santehnika", "asbob", "kabel", "rozetka", "lampa", "truba", "kley", "shpatlevka", "parket", "elektr", "instrument"]),
    ("clean", "Uy tozaligi uchun",
     ["tozala", "yuvish", "kir", "poroshok", "sovun", "chang", "supur"],
     ["poroshok", "sovun", "gel", "shvabra", "tozala", "detergent", "salfetka"]),
    ("baby", "Bolalar uchun",
     ["bola", "bolalar", "chakalok", "pampers", "taglik"],
     ["pampers", "taglik", "bolalar", "smes", "chakalok", "pyure"]),
]


@shop_router.get("/ai/status")
async def shop_ai_status(db: AsyncSession = Depends(get_db)):
    setting = await _get_or_create_setting(db)
    await db.commit()
    fl = _ai_flags(setting)
    return {"enabled": fl["client"], "help": fl["help"]}


def _ai_match_vocab(qw: str, vocab: list, vset: set) -> dict:
    """So'zga mos lug'at so'zlari: aniq/prefiks (1.0), keyin o'xshash imlo (0.8)."""
    import difflib
    out = {}
    st = _ai_stem(qw)
    for v in vocab:
        if v == qw:
            out[v] = 1.0
        elif len(st) >= 4 and (v.startswith(st) or (len(v) >= 4 and st.startswith(_ai_stem(v)))):
            out[v] = 0.95
    if not out and len(qw) >= 5:
        for v in difflib.get_close_matches(qw, vocab, n=4, cutoff=0.78):
            out[v] = 0.8
    return out


async def _ai_assist_core(db: AsyncSession, tg_user, body: "AIAssistBody", query: str) -> dict:
    q = _ai_key(query).strip()[:300]
    client = (await db.execute(select(User).where(_ident_cond(tg_user)))).scalars().first()
    stmt = (
        select(Product, PartnerProfile)
        .join(PartnerProfile, Product.partner_id == PartnerProfile.id)
        .where(Product.is_available == True, PartnerProfile.is_open == True)
        .order_by(Product.id.desc())   # yangi qo'shilgan mahsulotlar birinchi o'qiladi
    )
    if client and client.city_id:
        stmt = stmt.where(PartnerProfile.city_id == client.city_id)
    rows = (await db.execute(stmt.limit(6000))).all()

    # Mijoz joylashuvi: avval hozirgi GPS, bo'lmasa saqlangan uy manzili
    lat, lng = body.lat, body.lng
    if (lat is None or lng is None) and client is not None:
        lat, lng = getattr(client, "home_latitude", None), getattr(client, "home_longitude", None)
    have_loc = lat is not None and lng is not None

    def pdist(pr):
        if not have_loc or pr.latitude is None or pr.longitude is None:
            return None
        return _haversine_km(lat, lng, pr.latitude, pr.longitude)

    dist_cache = {}
    for _, pr in rows:
        if pr.id not in dist_cache:
            dist_cache[pr.id] = pdist(pr)

    def dkey(pid):  # yaqin birinchi; noma'lum masofa oxirida
        d = dist_cache.get(pid)
        return (d is None, d if d is not None else 0)

    # Mijoz do'kon nomini aytdimi? («Evos dan 3 ta lavash»)
    named = {}
    for _, pr in rows:
        bn = _ai_key(pr.brand_name or "").strip()
        if len(bn) >= 3 and bn in q:
            named[pr.id] = pr.brand_name
    if named:
        rows = [(p, pr) for p, pr in rows if pr.id in named]

    def hay(p, pr):
        return _ai_key(f"{p.name} {p.description or ''} {p.category or ''} {pr.category or ''}")

    def mk(p, pr, qty, kind, reason):
        d = dist_cache.get(pr.id)
        return {"product_id": p.id, "name": p.name, "price": p.price, "partner_id": pr.id,
                "partner_name": pr.brand_name, "qty": qty, "kind": kind, "reason": reason,
                "image_url": p.image_url, "distance_km": round(d, 1) if d is not None else None}

    tokens = [t for t in re.findall(r"[a-z0-9]+", q) if t]
    # 1) Aniq nom bo'yicha (miqdor bilan): "uchta lavash va bitta kola"
    wanted = []   # [(stems, qty)]
    for i, tok in enumerate(tokens):
        if len(tok) < 3 or tok in _AI_STOP_K or tok in _AI_QTY_K or tok.isdigit():
            continue
        if any(tok in _ai_key(n) for n in named.values()):
            continue  # do'kon nomining o'zi mahsulot emas
        stems = _AI_ALIASES_K.get(tok, [tok])
        stems = [_ai_stem(x) for x in stems]
        qty = 1
        for j in (i - 1, i - 2, i + 1):
            if 0 <= j < len(tokens):
                t = tokens[j]
                if t in _AI_QTY_K:
                    qty = _AI_QTY_K[t]
                    break
                if t.isdigit() and 0 < int(t) <= 30:
                    qty = int(t)
                    break
        wanted.append((stems, min(qty, 20)))
    cand = {}  # partner_id -> {wanted_index: (product, partner, qty)}
    matched_any = set()   # menyuda umuman topilgan so'zlar (tushunilmagan so'zlar "yo'q narsa" hisoblanmaydi)
    for wi, (stems, qty) in enumerate(wanted):
        for p, pr in rows:
            if any(sm in _ai_key(p.name) for sm in stems):
                matched_any.add(wi)
                cur = cand.setdefault(pr.id, {})
                if wi not in cur or p.price < cur[wi][0].price:
                    cur[wi] = (p, pr, qty)
    items = []
    summary = ""
    if cand:
        # Eng ko'p so'rovni qoplaydigan do'kon; teng bo'lsa — mijozga eng yaqini
        best_pid = sorted(cand.keys(), key=lambda pid: (-len(cand[pid]), dkey(pid)))[0]
        for wi in sorted(cand[best_pid]):
            p, pr, qty = cand[best_pid][wi]
            items.append(mk(p, pr, qty, "match", "So'rovingizga mos"))
        pr0 = cand[best_pid][sorted(cand[best_pid])[0]][1]
        # Aqlli qo'shimcha: ichimlik
        _drink_k = [_ai_key(d) for d in _AI_DRINK]
        if not any(any(d in _ai_key(i["name"]) for d in _drink_k) for i in items):
            drinks = sorted([(p, pr) for p, pr in rows if pr.id == best_pid and any(d in hay(p, pr) for d in _drink_k)],
                            key=lambda x: x[0].price)
            if drinks:
                items.append(mk(drinks[0][0], drinks[0][1], 1, "addon", "Ichimlik ham qo'shilsinmi?"))
        d0 = dist_cache.get(best_pid)
        where = f"«{pr0.brand_name}»" + (f" ({d0:.1f} km)" if d0 is not None else "")
        missing = len(matched_any) - len(cand[best_pid])
        summary = (f"{where} do'konidan topdim" if named else f"Sizga eng yaqin: {where}") + \
                  (f". {missing} ta narsa bu do'konda yo'q." if missing > 0 else ".") + " Tasdiqlang."
    else:
        # 2) Niyat bo'yicha (kayfiyat, salomatlik, teri, qurilish...) — yaqin do'konlar birinchi
        def trig_hit(trig: str) -> bool:
            tw = _ai_key(trig).split()
            for w in tw:
                if len(w) <= 3:
                    if w not in tokens:
                        return False
                else:
                    stw = _ai_stem(w)
                    if not any(t.startswith(stw) for t in tokens):
                        return False
            return bool(tw)
        hit_words, hit_label = [], None
        for key, label, triggers, w in list(_AI_INTENTS) + _AI_INTENTS_EXTRA:
            if any(trig_hit(t) for t in triggers):
                hit_label = hit_label or label
                hit_words += [_ai_key(x) for x in w]
        if hit_label:
            hw = list(dict.fromkeys(hit_words))
            scored = []
            for p, pr in rows:
                hws = set(_ai_words(hay(p, pr)))
                sc = sum(1 for x in hw if x in hws or (len(x) >= 5 and any(w.startswith(x) for w in hws)))
                if sc:
                    scored.append((p, pr, sc))
            scored.sort(key=lambda x: (dkey(x[1].id), -x[2], x[0].price))
            seen = {}
            for p, pr, sc in scored:
                if seen.get(pr.id, 0) >= 2:
                    continue
                seen[pr.id] = seen.get(pr.id, 0) + 1
                items.append(mk(p, pr, 1, "idea", hit_label))
                if len(items) >= 8:
                    break
            if items:
                summary = hit_label + (" — eng yaqin joylardan" if have_loc else "")
        if not items:
            # 3) Umumiy qidiruv: nom, tavsif, mahsulot va do'kon kategoriyasi, do'kon nomi bo'yicha;
            #    yaqin ma'noli so'zlar va imlo xatolari hisobga olinadi.
            words = [t for t in tokens if len(t) >= 3 and t not in _AI_STOP_K and t not in _AI_QTY_K and not t.isdigit()]
            fields = []
            vocab_set = set()
            for p, pr in rows:
                f = (set(_ai_words(p.name)), set(_ai_words(f"{p.description or ''} {p.category or ''}")),
                     set(_ai_words(pr.category or "")), set(_ai_words(pr.brand_name or "")))
                fields.append(f)
                for part in f:
                    vocab_set |= part
            vocab = sorted(vocab_set)
            fw = (3.0, 2.0, 2.5, 1.5)
            wmap = []   # har bir so'z uchun {lug'at so'zi: og'irlik}
            for w in words:
                m = _ai_match_vocab(w, vocab, vocab_set)
                for g in _AI_SYN:
                    if any(w == x or (len(w) >= 3 and (w.startswith(_ai_stem(x)) or _ai_stem(w) == x)) for x in g):
                        for x in g:
                            for v, wt in _ai_match_vocab(x, vocab, vocab_set).items():
                                m[v] = max(m.get(v, 0), 0.6 * wt)
                wmap.append(m)
            scored = []
            for (p, pr), f in zip(rows, fields):
                sc = 0.0
                for m in wmap:
                    best = 0.0
                    for part, wt in zip(f, fw):
                        for v in part:
                            if v in m:
                                best = max(best, wt * m[v])
                    sc += best
                if sc > 0:
                    scored.append((p, pr, sc))
            scored.sort(key=lambda x: (-round(x[2], 1), dkey(x[1].id), x[0].price))
            seen = {}
            for p, pr, sc in scored:
                if seen.get(pr.id, 0) >= 3:
                    continue
                seen[pr.id] = seen.get(pr.id, 0) + 1
                items.append(mk(p, pr, 1, "idea", "Qidiruv bo'yicha"))
                if len(items) >= 8:
                    break
            if not items and named:
                summary = "«" + ", ".join(named.values()) + "» do'konida bunday mahsulot topilmadi. Do'kon nomisiz yozib ko'ring — yaqin joylardan qidiraman."
            else:
                if items:
                    _pids = {i["partner_id"] for i in items}
                    cats = list(dict.fromkeys(_norm_partner_category(r[1].category) for r in rows if r[1].id in _pids))
                    summary = "Topilgan natijalar" + (f" ({', '.join(cats[:3])})" if cats else "")
                else:
                    summary = "Mos mahsulot topilmadi. Boshqacha yozib ko'ring (masalan: «uchta lavash va bitta kola», «tomog'im og'riyapti» yoki «qurilish mollari»)."
    return {"enabled": True, "summary": summary, "items": items, "have_location": have_loc,
            "named_partner": bool(named), "needs_payment": True}


@shop_router.post("/ai/assist")
async def shop_ai_assist(body: AIAssistBody, db: AsyncSession = Depends(get_db)):
    """Mijoz matnini (yoki ovozdan matnga aylangan so'zini) tushunib, MIJOZGA ENG YAQIN do'kondan mos mahsulotlarni yig'adi.
    Ma'lumot har so'rovda bazadan o'qiladi — yangi hamkor va yangi mahsulotlar darhol hisobga olinadi.
    Ovozdan bir nechta variant kelsa, birinchisi topmasa keyingilari sinab ko'riladi.
    Buyurtma faqat mijoz tasdig'i va to'lovdan keyin yuboriladi."""
    tg_user = _get_telegram_user_or_403(body.init_data)
    await _ai_require(db, "client")
    first = (body.query or "").strip()[:300]
    if len(first) < 2:
        raise HTTPException(status_code=400, detail="Nima xohlayotganingizni yozing")
    cands = [first] + [a.strip()[:300] for a in (body.alts or [])[:3] if a and a.strip() and a.strip().lower() != first.lower()]
    res = None
    for c in cands:
        res = await _ai_assist_core(db, tg_user, body, c)
        if res["items"]:
            break
    if res is not None and not res["items"] and len(cands) > 1:
        res["summary"] = "Mos mahsulot topilmadi. Aniqroq ayting yoki yozib ko'ring."
    return res


# ---------- Kuryer: aqlli maslahatchi ----------
@courier_router_app.get("/ai/insights")
async def courier_ai_insights(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_courier_user),
):
    setting = await _ai_require(db, "courier")
    cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == current_user.id))).scalars().first()
    pct = (setting.courier_share_percent or 0) / 100
    stmt = (
        select(Order).options(selectinload(Order.partner))
        .where(or_(
            and_(Order.status == OrderStatus.LOOKING_FOR_COURIER, Order.courier_id.is_(None)),
            and_(Order.courier_id == current_user.id, Order.status == OrderStatus.ON_THE_WAY),
        ))
    )
    if current_user.city_id is not None:
        stmt = stmt.outerjoin(PartnerProfile, Order.partner_id == PartnerProfile.id).where(
            or_(PartnerProfile.city_id == current_user.city_id, Order.courier_id == current_user.id))
    orders = (await db.execute(stmt.limit(60))).scalars().all()
    avail = [o for o in orders if o.courier_id is None]
    mine = [o for o in orders if o.courier_id == current_user.id]

    def plat(o): return o.partner.latitude if o.partner else None
    def plng(o): return o.partner.longitude if o.partner else None

    ranked = []
    for o in avail:
        pk = _road_km(setting, cp.latitude, cp.longitude, plat(o), plng(o)) if cp else None
        dk = o.distance_km
        if dk is None:
            dk = _road_km(setting, plat(o), plng(o), o.delivery_latitude, o.delivery_longitude)
        earn = (o.delivery_fee or 0) * pct + compute_courier_km_bonus(setting, dk) + compute_courier_pickup_bonus(setting, pk)
        total_km = (pk or 0) + (dk or 0)
        ranked.append({"id": o.id, "earn": round(earn), "pickup_km": pk, "dist_km": dk,
                       "per_km": round(earn / total_km) if total_km > 0.2 else None,
                       "brand": o.partner.brand_name if o.partner else ""})
    tips = []
    with_km = [r for r in ranked if r["per_km"] is not None]
    if with_km:
        b = max(with_km, key=lambda r: r["per_km"])
        tips.append({"type": "best", "icon": "🏆", "order_ids": [b["id"]],
                     "text": f"Eng foydali buyurtma: #{b['id']} ({b['brand']}) — taxminan {b['earn']:,} so'm, har km ≈ {b['per_km']:,} so'm.".replace(",", " ")})
        near = min((r for r in ranked if r["pickup_km"] is not None), key=lambda r: r["pickup_km"], default=None)
        if near and near["id"] != b["id"]:
            tips.append({"type": "near", "icon": "📍", "order_ids": [near["id"]],
                         "text": f"Eng yaqin do'kon: #{near['id']} — {near['pickup_km']:.1f} km."})
    # Batching: bir yo'nalishdagi buyurtmalarni birlashtirish
    pool = [o for o in avail + mine if o.partner and o.partner.latitude is not None and o.delivery_latitude is not None]
    used, groups = set(), []
    for o in pool:
        if o.id in used:
            continue
        g = [o]
        for o2 in pool:
            if o2.id == o.id or o2.id in used:
                continue
            if (_haversine_km(plat(o), plng(o), plat(o2), plng(o2)) <= 1.2 and
                    _haversine_km(o.delivery_latitude, o.delivery_longitude, o2.delivery_latitude, o2.delivery_longitude) <= 2.5):
                g.append(o2)
        if len(g) >= 2:
            groups.append(g[:3])
            used.update(x.id for x in g[:3])
    for g in groups:
        ids = [x.id for x in g]
        tips.append({"type": "batch", "icon": "🔗", "order_ids": ids,
                     "text": "Birga olib ketish mumkin: " + ", ".join(f"#{i}" for i in ids) + " — do'konlar va manzillar bir yo'nalishda."})
    w = getattr(setting.weather_condition, "value", "clear")
    if w in ("rain", "snow"):
        tips.append({"type": "weather", "icon": "🌧️", "order_ids": [], "text": "Ob-havo yomon: yo'lda ehtiyot bo'ling, tezlikni pasaytiring."})
    elif w == "hot":
        tips.append({"type": "weather", "icon": "☀️", "order_ids": [], "text": "Issiq kun: suv olib yuring, buyurtmani soyada kuting."})
    hour = (datetime.utcnow() + UZB_TZ_OFFSET).hour
    if hour in (12, 13, 18, 19, 20):
        tips.append({"type": "peak", "icon": "⏰", "order_ids": [], "text": "Hozir yuqori talab vaqti — buyurtmalar ko'p bo'ladi, onlayn turing."})
    if not avail and not mine:
        tips.append({"type": "idle", "icon": "💤", "order_ids": [], "text": "Hozir bo'sh buyurtma yo'q. Do'konlar atrofida turish tezroq buyurtma olishga yordam beradi."})
    return {"enabled": True, "tips": tips, "ranked": ranked[:10]}


# ---------- Hamkor: bashorat va ovozli/matnli buyruq ----------
@partner_router.get("/ai/insights")
async def partner_ai_insights(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    setting = await _ai_require(db, "partner")
    partner = await _get_own_partner(db, current_user)
    now = datetime.utcnow() + UZB_TZ_OFFSET
    since = datetime.utcnow() - timedelta(days=56)
    orders = (await db.execute(
        select(Order).where(Order.partner_id == partner.id, Order.created_at >= since, Order.status != OrderStatus.CANCELLED)
    )).scalars().all()
    wd = now.weekday()
    names = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
    by_hour = [0] * 24
    days_seen = set()
    all_days = set()
    for o in orders:
        t = o.created_at + UZB_TZ_OFFSET
        all_days.add(t.date())
        if t.weekday() == wd:
            by_hour[t.hour] += 1
            days_seen.add(t.date())
    n_days = max(len(days_seen), 1)
    avg_hour = [round(c / n_days, 1) for c in by_hour]
    expected_today = round(sum(avg_hour[now.hour:]), 1)
    peak = sorted(range(24), key=lambda h: -avg_hour[h])[:2]
    peak = [h for h in peak if avg_hour[h] > 0]
    top = {}
    if orders:
        ids = [o.id for o in orders]
        items = (await db.execute(select(OrderItem.product_name, func.sum(OrderItem.quantity))
                                  .where(OrderItem.order_id.in_(ids)).group_by(OrderItem.product_name))).all()
        top = sorted(items, key=lambda r: -(r[1] or 0))[:5]
    insights = []
    if not orders:
        insights.append({"icon": "📊", "text": "Hali tarix yetarli emas. Buyurtmalar to'plangach, bashorat aniqroq bo'ladi."})
    else:
        if peak:
            insights.append({"icon": "⏰", "text": f"{names[wd]} kunlari odatda eng ko'p buyurtma soat " + ", ".join(f"{h}:00" for h in sorted(peak)) + " atrofida keladi."})
        insights.append({"icon": "📈", "text": f"Bugun yana taxminan {expected_today:g} ta buyurtma kutilmoqda (so'nggi {n_days} ta {names[wd].lower()} tahlili)."})
    w = getattr(setting.weather_condition, "value", "clear")
    wmsg = {"rain": "Yomg'ir kutilmoqda: yetkazish buyurtmalari odatda ortadi. Issiq taomlar va xamir/qiyma zaxirasini ~20% ko'paytiring.",
            "snow": "Qor: yetkazish talabi ortadi. Issiq taomlar zaxirasini ko'paytiring.",
            "cold": "Sovuq havo: issiq sho'rva va ichimliklar talabi ortadi.",
            "hot": "Issiq havo: sovuq ichimlik, salat va muzqaymoq talabi ortadi.",
            "windy": "Shamol: kuryer kechikishi mumkin, taomlarni oldindan tayyorlab qo'ying."}.get(w)
    if wmsg:
        insights.append({"icon": "🌦️", "text": wmsg})
    if wd >= 4:
        insights.append({"icon": "🎉", "text": "Hafta oxiri yaqin: kechki soatlarda talab odatda yuqori bo'ladi."})
    if top:
        insights.append({"icon": "🔥", "text": "Eng ko'p sotilganlar: " + ", ".join(f"{n} ({int(c or 0)})" for n, c in top)})
    return {"enabled": True, "insights": insights, "hours": avg_hour, "weekday": names[wd], "now_hour": now.hour}


class PartnerAICommand(BaseModel):
    text: str


@partner_router.post("/ai/command")
async def partner_ai_command(
    body: PartnerAICommand,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_partner_user),
):
    """«Tovuq tugadi» → tovuqli taomlar menyudan yashiriladi; «tovuq keldi» → qayta ko'rsatiladi."""
    await _ai_require(db, "partner")
    partner = await _get_own_partner(db, current_user)
    t = _ai_norm(body.text).strip()[:200]
    off_w = ("tugadi", "tugab", "qolmadi", "yo'q", "yoq", "tugagan", "закончил", "нет", "out of")
    on_w = ("keldi", "bor", "qaytdi", "mavjud", "paydo", "есть", "back", "available")
    if any(w in t for w in off_w):
        make = False
    elif any(w in t for w in on_w):
        make = True
    else:
        raise HTTPException(status_code=400, detail="Masalan: «Tovuq tugadi» yoki «Tovuq keldi» deb yozing")
    stop = set(off_w) | set(on_w) | {"va", "bilan", "hamma", "barcha", "ham", "endi", "bugun"}
    words = [w for w in re.findall(r"[\w']+", t) if len(w) >= 3 and w not in stop]
    if not words:
        raise HTTPException(status_code=400, detail="Qaysi mahsulot ekanini yozing (masalan: «Tovuq tugadi»)")
    prods = (await db.execute(select(Product).where(Product.partner_id == partner.id))).scalars().all()
    stems = [w[:max(4, len(w) - 2)] if len(w) > 5 else w for w in words]
    hit = [p for p in prods if any(s in _ai_norm(f"{p.name} {p.description or ''}") for s in stems)]
    if not hit:
        raise HTTPException(status_code=404, detail="Menyuda bunday mahsulot topilmadi")
    changed = []
    for p in hit:
        if bool(p.is_available) != make:
            p.is_available = make
            changed.append(p.name)
    await db.commit()
    alts = []
    if not make:
        cats = {p.category for p in hit}
        alts = [p.name for p in prods if p.is_available and p.category in cats][:4]
    return {"ok": True, "available": make, "changed": changed, "matched": [p.name for p in hit], "alternatives": alts}


# ---------- AI-dispetcher: kechikishni avtomatik hal qilish ----------
async def ai_dispatcher_tick() -> int:
    """Kuryer kechiksa — operator aralashuvisiz mijozga uzr xabari va promo-kod yuboradi.
    Kutilgan vaqt = 12 daqiqa + masofa(km) * 4 daqiqa; unga 5 daqiqa qo'shilsa kechikkan hisoblanadi."""
    import secrets as _sec
    sent = 0
    async with AsyncSessionLocal() as db:
        setting = await _get_or_create_setting(db)
        if not _ai_flags(setting)["dispatcher"]:
            return 0
        now = datetime.utcnow()
        orders = (await db.execute(
            select(Order).options(selectinload(Order.client))
            .where(Order.status == OrderStatus.ON_THE_WAY, Order.ai_delay_notified != True,
                   Order.on_the_way_at.is_not(None))
            .limit(100)
        )).scalars().all()
        for o in orders:
            expected = 12 + (o.distance_km or 3.0) * 4 + 5
            late_min = (now - o.on_the_way_at).total_seconds() / 60 - expected
            if late_min < 0:
                continue
            o.ai_delay_notified = True
            promo_txt = ""
            amount = float(setting.ai_delay_promo_amount or 0)
            if bool(getattr(setting, "ai_delay_promo_enabled", False)) and amount > 0:
                code = "ZM" + _sec.token_hex(3).upper()
                db.add(PromoCode(code=code, discount_amount=amount, max_uses=1, is_active=True,
                                 expires_at=now + timedelta(days=14)))
                promo_txt = f"\nUzr sifatida keyingi buyurtmangizga {int(amount):,} so'm chegirma: <b>{code}</b>".replace(",", " ")
            client = o.client
            if client and client.telegram_id:
                try:
                    await send_telegram_message(
                        client.telegram_id,
                        f"🛵 Buyurtma #{o.id}: kuryerimiz yo'ldagi sabablar bilan biroz kechikmoqda. Kechirasiz, tez orada yetib boradi.{promo_txt}",
                    )
                    sent += 1
                except Exception as e:
                    print(f"[AI-DISPETCHER] xabar yuborilmadi: {e!r}")
        await db.commit()
    return sent


async def _ai_dispatcher_loop():
    import asyncio
    while True:
        try:
            await asyncio.sleep(60)
            await ai_dispatcher_tick()
            await ai_campaign_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[AI-DISPETCHER XATO] {e!r}")


# ---------- LED reklama (kuryer sumkasining orqa-pastki ekrani) ----------
# Hammasini FAQAT admin boshqaradi: hamkor reklama SO'RAYDI (pending), adminga to'lov qiladi,
# admin tasdiqlagach (approved) AI navbatiga qo'shiladi. Kuryer faqat ko'radi.
class LedAdBody(BaseModel):
    title: str
    text: Optional[str] = ""
    bg_color: Optional[str] = "#f59e0b"
    radius_km: Optional[float] = 3.0
    seconds: Optional[int] = None   # faqat admin (admin_led_create)
    days: Optional[int] = None      # faqat admin: necha kun efirda


class LedApproveBody(BaseModel):
    days: int = 7
    paid_amount: float = 0.0
    seconds: Optional[int] = None


class LedUpdateBody(BaseModel):
    seconds: Optional[int] = None
    title: Optional[str] = None
    text: Optional[str] = None
    radius_km: Optional[float] = None
    bg_color: Optional[str] = None


def _led_live(a: LedAd, now=None) -> bool:
    now = now or datetime.utcnow()
    return bool(a.is_active) and a.status == "approved" and (a.expires_at is None or a.expires_at > now)


def _led_dict(a: LedAd) -> dict:
    expired = a.status == "approved" and a.expires_at is not None and a.expires_at <= datetime.utcnow()
    return {"id": a.id, "title": a.title, "text": a.text or "", "bg_color": a.bg_color or "#f59e0b",
            "radius_km": a.radius_km or 3.0, "is_active": bool(a.is_active), "status": ("expired" if expired else (a.status or "pending")),
            "paid_amount": a.paid_amount or 0.0, "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            "admin_note": a.admin_note or "", "partner_id": a.partner_id, "seconds": a.seconds,
            "brand": a.partner.brand_name if a.partner else "ZooMo"}


def _led_clean(body: LedAdBody):
    title = (body.title or "").strip()[:60]
    if not title:
        raise HTTPException(status_code=400, detail="Reklama sarlavhasini yozing")
    color = body.bg_color if re.fullmatch(r"#[0-9a-fA-F]{6}", body.bg_color or "") else "#f59e0b"
    radius = min(max(body.radius_km or 3.0, 0.5), 30.0)
    return title, (body.text or "").strip()[:120], color, radius


@partner_router.get("/ai/led")
async def partner_led_list(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_partner_user)):
    await _ai_require(db, "led")
    partner = await _get_own_partner(db, current_user)
    if not partner.led_allowed:
        raise HTTPException(status_code=403, detail="LED reklama sizga yoqilmagan — administrator bilan bog'laning")
    ads = (await db.execute(select(LedAd).options(selectinload(LedAd.partner)).where(LedAd.partner_id == partner.id)
                            .order_by(LedAd.id.desc()))).scalars().all()
    return {"ads": [_led_dict(a) for a in ads], "pay_card": await _get_topup_card_info(db)}


@partner_router.post("/ai/led")
async def partner_led_create(body: LedAdBody, db: AsyncSession = Depends(get_db),
                             current_user: User = Depends(get_current_partner_user)):
    """Hamkor reklama SO'ROVI yuboradi. U ekranga chiqmaydi — admin to'lovni tasdiqlab yoqmaguncha."""
    await _ai_require(db, "led")
    partner = await _get_own_partner(db, current_user)
    if not partner.led_allowed:
        raise HTTPException(status_code=403, detail="LED reklama sizga yoqilmagan — administrator bilan bog'laning")
    title, text, color, radius = _led_clean(body)
    cnt = (await db.execute(select(func.count(LedAd.id)).where(LedAd.partner_id == partner.id, LedAd.status == "pending"))).scalar() or 0
    if cnt >= 5:
        raise HTTPException(status_code=400, detail="Tasdiq kutayotgan 5 tadan ortiq so'rov bo'lishi mumkin emas")
    db.add(LedAd(partner_id=partner.id, title=title, text=text, bg_color=color, radius_km=radius, status="pending", is_active=False))
    await db.commit()
    return {"ok": True}


@partner_router.post("/ai/led/{ad_id}/delete")
async def partner_led_delete(ad_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_partner_user)):
    """Hamkor faqat hali tasdiqlanmagan (yoki rad etilgan) so'rovini bekor qila oladi."""
    await _ai_require(db, "led")
    partner = await _get_own_partner(db, current_user)
    if not partner.led_allowed:
        raise HTTPException(status_code=403, detail="LED reklama sizga yoqilmagan — administrator bilan bog'laning")
    ad = (await db.execute(select(LedAd).where(LedAd.id == ad_id, LedAd.partner_id == partner.id))).scalars().first()
    if not ad:
        raise HTTPException(status_code=404, detail="Reklama topilmadi")
    if ad.status == "approved":
        raise HTTPException(status_code=403, detail="Tasdiqlangan reklamani faqat admin o'zgartiradi")
    await db.delete(ad)
    await db.commit()
    return {"ok": True}


@courier_router_app.get("/led", response_class=HTMLResponse)
async def courier_led_page(request: Request, db: AsyncSession = Depends(get_db),
                           current_user: User = Depends(get_current_courier_user)):
    await _ai_require(db, "led")
    _cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == current_user.id))).scalars().first()
    if _cp is not None and _cp.led_enabled is False:
        raise HTTPException(status_code=403, detail="LED ekran sizga o'chirilgan")
    return templates.TemplateResponse(request=request, name="led.html", context={"current_user": current_user})


def _led_playlist(ads: list, now=None) -> list:
    """AI efir navbati (TV kabi): har reklamaga og'irlik beriladi — kuryerga yaqinroq do'kon va ko'proq
    to'lagan reklama ko'proq efir oladi. "Silliq og'irlikli aylanma" (smooth weighted round-robin)
    bir reklamani ketma-ket takrorlamaydi, hammasi adolatli tarqaladi."""
    if not ads:
        return []
    for a in ads:
        w = 1.0
        if a["distance_km"] is not None and a["radius_km"]:
            w += 2.0 * max(0.0, 1.0 - a["distance_km"] / a["radius_km"])
        w += min((a["paid_amount"] or 0) / 100000.0, 3.0)
        a["_w"] = w
        a["_cur"] = 0.0
    total = sum(a["_w"] for a in ads)
    slots = max(len(ads), min(14, int(round(total))))
    out = []
    last = None
    for _ in range(slots):
        for a in ads:
            a["_cur"] += a["_w"]
        pick = max((a for a in ads if a["id"] != last) or ads, key=lambda a: a["_cur"])
        pick["_cur"] -= total
        last = pick["id"]
        item = {k: v for k, v in pick.items() if not k.startswith("_")}
        item["seconds"] = int(pick["seconds"]) if pick.get("seconds") else int(7 + min(5, round(pick["_w"])))
        out.append(item)
    return out


# ==================== HAMKOR BANNER REKLAMASI (mijoz ilovasida) ====================
# Egasi hamkorga ruxsat beradi (PartnerProfile.banner_allowed) va kunlik narxni belgilaydi.
# Hamkor so'rov yuboradi (rasm/matn/sarlavha/rang) -> egasi to'lovni tasdiqlab ruxsat beradi -> Banner
# hamkorning O'Z SHAHRI mijozlariga `days` kun ko'rsatiladi.
banner_req_admin_router = APIRouter(tags=["Hamkor banner so'rovlari"])
BANNER_REQ_MAX_PENDING = 5


def _bq_dict(r: "BannerRequest") -> dict:
    return {"id": r.id, "title": r.title or "", "text": r.text_content or "", "bg_color": r.bg_color or "#f59e0b",
            "has_image": bool(r.image_data), "days": r.days, "price": r.price or 0.0, "status": r.status or "pending",
            "admin_note": r.admin_note or "", "at": r.created_at.isoformat() if r.created_at else None,
            "partner": r.partner.brand_name if r.partner else "?", "partner_id": r.partner_id, "city_id": r.city_id}


async def _partner_banner_guard(db: AsyncSession, current_user: User) -> PartnerProfile:
    partner = await _get_own_partner(db, current_user)
    if not partner.banner_allowed:
        raise HTTPException(status_code=403, detail="Banner reklama sizga yoqilmagan — administrator bilan bog'laning")
    return partner


@partner_router.get("/banner-ads")
async def partner_banner_ads(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_partner_user)):
    partner = await _partner_banner_guard(db, current_user)
    setting = await _get_or_create_setting(db)
    rows = (await db.execute(select(BannerRequest).options(selectinload(BannerRequest.partner))
                             .where(BannerRequest.partner_id == partner.id).order_by(BannerRequest.id.desc()).limit(50))).scalars().all()
    city = (await db.execute(select(City.name).where(City.id == partner.city_id))).scalars().first() if partner.city_id else None
    return {"price_per_day": setting.partner_banner_price_per_day or 0.0, "city": city,
            "requests": [_bq_dict(r) for r in rows], "pay_card": await _get_topup_card_info(db)}


@partner_router.post("/banner-ads")
async def partner_banner_ads_create(
    title: str = Form(""), text_content: str = Form(""), bg_color: str = Form("#f59e0b"),
    days: int = Form(7), image: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_partner_user),
):
    partner = await _partner_banner_guard(db, current_user)
    if partner.city_id is None:
        raise HTTPException(status_code=400, detail="Do'koningiz shahri belgilanmagan — administrator bilan bog'laning")
    title = (title or "").strip()[:60]
    text_content = (text_content or "").strip()[:300]
    has_img = bool(image and image.filename)
    if not has_img and not (title or text_content):
        raise HTTPException(status_code=400, detail="Rasm yoki sarlavha/matn kiriting")
    if not (1 <= days <= 60):
        raise HTTPException(status_code=400, detail="Muddat 1 dan 60 kungacha bo'lsin")
    color = bg_color if re.fullmatch(r"#[0-9a-fA-F]{6}", bg_color or "") else "#f59e0b"
    pending = (await db.execute(select(func.count(BannerRequest.id)).where(
        BannerRequest.partner_id == partner.id, BannerRequest.status == "pending"))).scalar() or 0
    if pending >= BANNER_REQ_MAX_PENDING:
        raise HTTPException(status_code=400, detail=f"Tasdiq kutayotgan {BANNER_REQ_MAX_PENDING} tadan ortiq so'rov bo'lishi mumkin emas")
    img, mime = (None, None)
    if has_img:
        img, mime = await process_uploaded_image(image)
        # Hamkor yuklagan rasm BARCHA mijozlarga ko'rsatiladi — kengaytma emas, haqiqiy fayl sarlavhasini ham tekshiramiz
        if not (img[:8] == b"\x89PNG\r\n\x1a\n" or img[:3] == b"\xff\xd8\xff" or (img[:4] == b"RIFF" and img[8:12] == b"WEBP")):
            raise HTTPException(status_code=400, detail="Bu haqiqiy rasm fayli emas (JPG, PNG yoki WEBP yuklang)")
    setting = await _get_or_create_setting(db)
    price = float(setting.partner_banner_price_per_day or 0.0) * days
    db.add(BannerRequest(partner_id=partner.id, city_id=partner.city_id, title=title, text_content=text_content, bg_color=color,
                         image_data=img, image_mime=mime, days=days, price=price, status="pending"))
    await db.commit()
    return {"ok": True, "price": price}


@partner_router.post("/banner-ads/{req_id}/delete")
async def partner_banner_ads_delete(req_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_partner_user)):
    partner = await _partner_banner_guard(db, current_user)
    r = (await db.execute(select(BannerRequest).where(BannerRequest.id == req_id, BannerRequest.partner_id == partner.id))).scalars().first()
    if not r:
        raise HTTPException(status_code=404, detail="So'rov topilmadi")
    if r.status == "approved":
        raise HTTPException(status_code=403, detail="Tasdiqlangan bannerni faqat administrator o'zgartiradi")
    await db.delete(r)
    await db.commit()
    return {"ok": True}


@partner_router.get("/banner-ads/{req_id}/image")
async def partner_banner_ads_image(req_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_partner_user)):
    partner = await _get_own_partner(db, current_user)
    r = (await db.execute(select(BannerRequest).where(BannerRequest.id == req_id, BannerRequest.partner_id == partner.id))).scalars().first()
    if not r or not r.image_data:
        raise HTTPException(status_code=404, detail="Rasm topilmadi")
    return Response(content=r.image_data, media_type=r.image_mime or "image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


async def _banner_req_viewer(db: AsyncSession, current_user: User) -> Optional[int]:
    """Egasi — hammasi (None). Operator — faqat egasi ruxsat bergan bo'lsa va faqat o'z shahri (city_id qaytaradi)."""
    if current_user.role == UserRole.OWNER:
        return None
    if not await operator_can_see(db, current_user, "banner_sorovlari") or current_user.city_id is None:
        raise HTTPException(status_code=403, detail="Bu bo'limni ko'rish huquqingiz yo'q — administrator bilan bog'laning.")
    return current_user.city_id


@banner_req_admin_router.get("/admin/banner-requests")
async def admin_banner_requests(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    scope_city = await _banner_req_viewer(db, current_user)
    q = select(BannerRequest).options(selectinload(BannerRequest.partner)).order_by(BannerRequest.id.desc()).limit(100)
    if scope_city is not None:
        q = q.where(BannerRequest.city_id == scope_city)
    rows = (await db.execute(q)).scalars().all()
    cn = {c.id: c.name for c in (await db.execute(select(City))).scalars().all()}
    out = []
    for r in rows:
        d = _bq_dict(r)
        d["city"] = cn.get(r.city_id, "—")
        out.append(d)
    setting = await _get_or_create_setting(db)
    return {"items": out, "can_decide": True, "price_per_day": setting.partner_banner_price_per_day or 0.0}


@banner_req_admin_router.get("/admin/banner-requests/{req_id}/image")
async def admin_banner_request_image(req_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    scope_city = await _banner_req_viewer(db, current_user)
    r = (await db.execute(select(BannerRequest).where(BannerRequest.id == req_id))).scalars().first()
    if not r or not r.image_data or (scope_city is not None and r.city_id != scope_city):
        raise HTTPException(status_code=404, detail="Rasm topilmadi")
    return Response(content=r.image_data, media_type=r.image_mime or "image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


class BannerDecideBody(BaseModel):
    note: Optional[str] = ""


@banner_req_admin_router.post("/admin/banner-requests/{req_id}/approve")
async def admin_banner_request_approve(req_id: int, body: BannerDecideBody, db: AsyncSession = Depends(get_db),
                                       owner: User = Depends(get_current_admin_user)):
    """To'lovni qabul qilgach tasdiqlash (egasi yoki — ruxsat berilgan — o'z shahri operatori):
    Banner hamkor shahri mijozlariga `days` kun chiqadi."""
    scope_city = await _banner_req_viewer(db, owner)
    r = (await db.execute(select(BannerRequest).where(BannerRequest.id == req_id).with_for_update())).scalars().first()
    if not r or (scope_city is not None and r.city_id != scope_city):
        raise HTTPException(status_code=404, detail="So'rov topilmadi")
    if r.status != "pending":
        raise HTTPException(status_code=400, detail="Bu so'rov allaqachon ko'rib chiqilgan")
    now = datetime.utcnow()
    b = Banner(title=r.title, text_content=r.text_content, image_data=r.image_data, image_mime=r.image_mime,
               bg_color=r.bg_color, is_active=True, display_order=0, city_id=r.city_id,
               starts_at=now, ends_at=now + timedelta(days=r.days or 7))
    db.add(b)
    await db.flush()
    r.status = "approved"
    r.banner_id = b.id
    r.decided_at = now
    r.admin_note = (body.note or "").strip()[:200] or None
    await db.commit()
    return {"ok": True, "banner_id": b.id}


@banner_req_admin_router.post("/admin/banner-requests/{req_id}/reject")
async def admin_banner_request_reject(req_id: int, body: BannerDecideBody, db: AsyncSession = Depends(get_db),
                                      owner: User = Depends(get_current_admin_user)):
    scope_city = await _banner_req_viewer(db, owner)
    r = (await db.execute(select(BannerRequest).where(BannerRequest.id == req_id))).scalars().first()
    if not r or (scope_city is not None and r.city_id != scope_city):
        raise HTTPException(status_code=404, detail="So'rov topilmadi")
    if r.status != "pending":
        raise HTTPException(status_code=400, detail="Bu so'rov allaqachon ko'rib chiqilgan")
    r.status = "rejected"
    r.decided_at = datetime.utcnow()
    r.admin_note = (body.note or "").strip()[:200] or None
    await db.commit()
    return {"ok": True}


@banner_req_admin_router.post("/admin/settings/banner-price")
async def admin_set_banner_price(price_per_day: float = Form(..., allow_inf_nan=False), db: AsyncSession = Depends(get_db),
                                 owner: User = Depends(require_owner)):
    if price_per_day < 0 or price_per_day > 100_000_000:
        raise HTTPException(status_code=400, detail="Narx noto'g'ri")
    setting = await _get_or_create_setting(db)
    setting.partner_banner_price_per_day = price_per_day
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@banner_req_admin_router.post("/admin/partners/{partner_id}/ads-access")
async def admin_partner_ads_access(partner_id: int, led_allowed: Optional[str] = Form(None), banner_allowed: Optional[str] = Form(None),
                                   db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    p = (await db.execute(select(PartnerProfile).where(PartnerProfile.id == partner_id))).scalars().first()
    if not p:
        raise HTTPException(status_code=404, detail="Do'kon topilmadi")
    p.led_allowed = bool(led_allowed)
    p.banner_allowed = bool(banner_allowed)
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)



@courier_router_app.get("/led/feed")
async def courier_led_feed(lat: Optional[float] = None, lng: Optional[float] = None,
                           db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_courier_user)):
    """Kuryer joylashuviga qarab AI tuzgan efir navbati. Faqat admin tasdiqlagan, to'langan, muddati tugamagan reklamalar."""
    await _ai_require(db, "led")
    cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == current_user.id))).scalars().first()
    if cp is not None and cp.led_enabled is False:
        return {"ads": [], "disabled": True}
    ads = (await db.execute(select(LedAd).options(selectinload(LedAd.partner))
                            .where(LedAd.is_active == True, LedAd.status == "approved").limit(300))).scalars().all()
    now = datetime.utcnow()
    cand = []
    for a in ads:
        if not _led_live(a, now):
            continue
        p = a.partner
        if p is not None and p.is_open is False:
            continue
        dist = None
        if lat is not None and lng is not None and p is not None and p.latitude is not None and p.longitude is not None:
            dist = _haversine_km(lat, lng, p.latitude, p.longitude)
            if dist > (a.radius_km or 3.0):
                continue
        d = _led_dict(a)
        d["distance_km"] = round(dist, 2) if dist is not None else None
        cand.append(d)
    return {"ads": _led_playlist(cand), "disabled": False}


ai_admin_router = APIRouter(tags=["ZooMo AI Admin"])


async def _led_get(db, ad_id):
    ad = (await db.execute(select(LedAd).where(LedAd.id == ad_id))).scalars().first()
    if not ad:
        raise HTTPException(status_code=404, detail="Reklama topilmadi")
    return ad


@ai_admin_router.get("/admin/ai/led")
async def admin_led_list(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    ads = (await db.execute(select(LedAd).options(selectinload(LedAd.partner)).order_by(LedAd.id.desc()).limit(300))).scalars().all()
    couriers = (await db.execute(select(CourierProfile, User).join(User, CourierProfile.user_id == User.id).limit(300))).all()
    return {"ads": [_led_dict(a) for a in ads],
            "couriers": [{"id": c.id, "name": u.full_name, "led_enabled": c.led_enabled is not False} for c, u in couriers]}


@ai_admin_router.post("/admin/ai/led")
async def admin_led_create(body: LedAdBody, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    title, text, color, radius = _led_clean(body)
    secs = min(max(int(body.seconds), 3), 120) if body.seconds else None
    exp = datetime.utcnow() + timedelta(days=min(max(int(body.days), 1), 365)) if body.days else None
    db.add(LedAd(partner_id=None, title=title, text=text, bg_color=color, radius_km=30.0 if not body.radius_km else radius,
                 seconds=secs, expires_at=exp, status="approved", is_active=True))
    await db.commit()
    return {"ok": True}


@ai_admin_router.post("/admin/ai/led/{ad_id}/approve")
async def admin_led_approve(ad_id: int, body: LedApproveBody, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    """To'lov qabul qilingach tasdiqlash: necha kunga va qancha to'langani (efir ulushiga ta'sir qiladi)."""
    ad = await _led_get(db, ad_id)
    days = min(max(int(body.days or 0), 1), 365)
    ad.status = "approved"
    ad.is_active = True
    ad.paid_amount = max(float(body.paid_amount or 0), 0.0)
    ad.expires_at = datetime.utcnow() + timedelta(days=days)
    if body.seconds:
        ad.seconds = min(max(int(body.seconds), 3), 120)
    await db.commit()
    return {"ok": True}


@ai_admin_router.post("/admin/ai/led/{ad_id}/reject")
async def admin_led_reject(ad_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    ad = await _led_get(db, ad_id)
    ad.status = "rejected"
    ad.is_active = False
    await db.commit()
    return {"ok": True}


@ai_admin_router.post("/admin/ai/led/{ad_id}/toggle")
async def admin_led_toggle(ad_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    ad = await _led_get(db, ad_id)
    if ad.status != "approved":
        raise HTTPException(status_code=400, detail="Avval to'lovni tasdiqlang")
    ad.is_active = not ad.is_active
    await db.commit()
    return {"ok": True, "is_active": ad.is_active}


@ai_admin_router.post("/admin/ai/led/{ad_id}/update")
async def admin_led_update(ad_id: int, body: LedUpdateBody, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    ad = await _led_get(db, ad_id)
    if body.title is not None and body.title.strip():
        ad.title = body.title.strip()[:60]
    if body.text is not None:
        ad.text = body.text.strip()[:120]
    if body.radius_km is not None:
        ad.radius_km = min(max(body.radius_km, 0.5), 30.0)
    if body.bg_color and re.fullmatch(r"#[0-9a-fA-F]{6}", body.bg_color):
        ad.bg_color = body.bg_color
    if body.seconds is not None:
        ad.seconds = min(max(int(body.seconds), 3), 120) if body.seconds > 0 else None
    await db.commit()
    return {"ok": True}


@ai_admin_router.post("/admin/ai/led/{ad_id}/delete")
async def admin_led_delete(ad_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    ad = (await db.execute(select(LedAd).where(LedAd.id == ad_id))).scalars().first()
    if ad:
        await db.delete(ad)
        await db.commit()
    return {"ok": True}


@ai_admin_router.post("/admin/ai/led/courier/{cp_id}/toggle")
async def admin_led_courier_toggle(cp_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    cp = (await db.execute(select(CourierProfile).where(CourierProfile.id == cp_id))).scalars().first()
    if not cp:
        raise HTTPException(status_code=404, detail="Kuryer topilmadi")
    cp.led_enabled = not (cp.led_enabled is not False)
    await db.commit()
    return {"ok": True, "led_enabled": cp.led_enabled}


# ---------- AI yordamchi (rolga mos qo'llanma) ----------
import ai_help_kb


class AIHelpBody(BaseModel):
    q: str = ""


class AIHelpShopBody(BaseModel):
    init_data: str
    q: str = ""


@shop_router.post("/ai/help")
async def shop_ai_help(body: AIHelpShopBody, db: AsyncSession = Depends(get_db)):
    _get_telegram_user_or_403(body.init_data)
    await _ai_require(db, "help")
    return ai_help_kb.answer("client", body.q[:300])


@courier_router_app.post("/ai/help")
async def courier_ai_help(body: AIHelpBody, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_courier_user)):
    await _ai_require(db, "help")
    return ai_help_kb.answer("courier", body.q[:300])


@partner_router.post("/ai/help")
async def partner_ai_help(body: AIHelpBody, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_partner_user)):
    await _ai_require(db, "help")
    return ai_help_kb.answer("partner", body.q[:300])


# ---------- Admin AI xabarlari (bildirishnoma kampaniyalari) ----------
class AIMessageBody(BaseModel):
    title: Optional[str] = ""
    text: str
    audience: str = "client"
    inactive_hours: Optional[int] = None
    repeat_hours: Optional[int] = 168


def _aim_dict(m: AIMessage) -> dict:
    return {"id": m.id, "title": m.title, "text": m.text, "audience": m.audience, "inactive_hours": m.inactive_hours,
            "repeat_hours": m.repeat_hours, "is_active": bool(m.is_active), "sent_count": m.sent_count or 0,
            "last_run_at": m.last_run_at.isoformat() if m.last_run_at else None}


def _aim_text(m: AIMessage) -> str:
    return "📣 " + html.escape(m.text)


async def _aim_send(db: AsyncSession, m: AIMessage, users: list) -> int:
    import asyncio as _a
    sent = 0
    for u in users:
        if not u.telegram_id:
            continue
        try:
            ok = await send_telegram_message(u.telegram_id, _aim_text(m))
        except Exception as e:
            print(f"[AI-XABAR] yuborilmadi: {e!r}")
            ok = False
        if ok is not False:
            db.add(AIMessageLog(message_id=m.id, user_id=u.id))
            sent += 1
        await _a.sleep(0.05)
    m.sent_count = (m.sent_count or 0) + sent
    m.last_run_at = datetime.utcnow()
    await db.commit()
    return sent


def _ai_audience_cond(audience: str):
    """Auditoriya sharti. DIQQAT: multi-role tizimida kuryer/hamkorning User.role maydoni CLIENT bo'lib qoladi —
    shuning uchun ular rol bo'yicha emas, PROFIL mavjudligi (tasdiqlangan) bo'yicha topiladi."""
    if audience == "courier":
        return User.id.in_(select(CourierProfile.user_id).where(CourierProfile.application_status != "pending"))
    if audience == "partner":
        return User.id.in_(select(PartnerProfile.user_id).where(PartnerProfile.application_status != "pending"))
    return User.role == UserRole.CLIENT


async def ai_campaign_tick() -> int:
    """«N soat kirmaganlarga» xabarlar: har daqiqada tekshiriladi, bir mijozga takror oralig'ida qayta yuborilmaydi."""
    total = 0
    async with AsyncSessionLocal() as db:
        setting = await _get_or_create_setting(db)
        if not _ai_flags(setting)["campaigns"]:
            return 0
        now = datetime.utcnow()
        msgs = (await db.execute(select(AIMessage).where(AIMessage.is_active == True, AIMessage.inactive_hours.is_not(None)))).scalars().all()
        for m in msgs:
            if m.audience not in ("client", "courier", "partner"):
                continue
            cutoff = now - timedelta(hours=max(int(m.inactive_hours or 1), 1))
            cands = (await db.execute(
                select(User).where(_ai_audience_cond(m.audience), User.is_active == True, User.telegram_id.is_not(None),
                                   func.coalesce(User.last_active_at, User.created_at) < cutoff).limit(300)
            )).scalars().all()
            if not cands:
                continue
            gap = now - timedelta(hours=max(int(m.repeat_hours or 24), 1))
            recent = set(r[0] for r in (await db.execute(
                select(AIMessageLog.user_id).where(AIMessageLog.message_id == m.id, AIMessageLog.sent_at > gap))).all())
            todo = [u for u in cands if u.id not in recent][:100]
            if todo:
                total += await _aim_send(db, m, todo)
    return total


@ai_admin_router.post("/admin/ai/help")
async def admin_ai_help(body: AIHelpBody, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    await _ai_require(db, "help")
    return ai_help_kb.answer("admin", body.q[:300])


@ai_admin_router.get("/admin/ai/messages")
async def admin_aim_list(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    ms = (await db.execute(select(AIMessage).order_by(AIMessage.id.desc()).limit(100))).scalars().all()
    return {"messages": [_aim_dict(m) for m in ms]}


@ai_admin_router.post("/admin/ai/messages")
async def admin_aim_create(body: AIMessageBody, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    text = (body.text or "").strip()[:1000]
    if not text:
        raise HTTPException(status_code=400, detail="Xabar matnini yozing")
    if body.audience not in ("client", "courier", "partner"):
        raise HTTPException(status_code=400, detail="Auditoriya noto'g'ri")
    ih = body.inactive_hours if body.inactive_hours and body.inactive_hours > 0 else None
    if ih is not None:
        ih = min(ih, 24 * 365)
    rh = min(max(body.repeat_hours or 168, 1), 24 * 365)
    m = AIMessage(title=(body.title or text)[:60], text=text, audience=body.audience, inactive_hours=ih, repeat_hours=rh,
                  is_active=True)
    db.add(m)
    await db.commit()
    return {"ok": True}


async def _aim_get(db, mid):
    m = (await db.execute(select(AIMessage).where(AIMessage.id == mid))).scalars().first()
    if not m:
        raise HTTPException(status_code=404, detail="Xabar topilmadi")
    return m


@ai_admin_router.post("/admin/ai/messages/{mid}/toggle")
async def admin_aim_toggle(mid: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    m = await _aim_get(db, mid)
    m.is_active = not m.is_active
    await db.commit()
    return {"ok": True, "is_active": m.is_active}


@ai_admin_router.post("/admin/ai/messages/{mid}/delete")
async def admin_aim_delete(mid: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    m = await _aim_get(db, mid)
    await db.delete(m)
    await db.commit()
    return {"ok": True}


@ai_admin_router.post("/admin/ai/messages/{mid}/send")
async def admin_aim_send_now(mid: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    """Hozir yuborish: butun auditoriyaga (Telegram ulangan foydalanuvchilar) bir martalik. Orqa fonda yuboriladi."""
    setting = await _get_or_create_setting(db)
    if not _ai_flags(setting)["campaigns"]:
        raise HTTPException(status_code=403, detail="AI xabarlari o'chirilgan")
    m = await _aim_get(db, mid)
    aud_cond = _ai_audience_cond(m.audience)
    users = (await db.execute(select(User).where(aud_cond, User.is_active == True, User.telegram_id.is_not(None)).limit(2000))).scalars().all()
    n = len(users)
    mid_ = m.id

    async def _run():
        async with AsyncSessionLocal() as db2:
            mm = (await db2.execute(select(AIMessage).where(AIMessage.id == mid_))).scalars().first()
            us = (await db2.execute(select(User).where(aud_cond, User.is_active == True, User.telegram_id.is_not(None)).limit(2000))).scalars().all()
            if mm:
                await _aim_send(db2, mm, us)
    import asyncio as _a
    _a.create_task(_run())
    return {"ok": True, "recipients": n}


# ==================== CHAT / FIKR-MULOHAZA VA MAJBURIY E'LONLAR ====================
# Hamkor, kuryer va mijoz administratsiyaga real muammo/fikr yozadi (chat). Admin va operator
# 3 bo'limda (hamkor / kuryer / mijoz) ko'radi, har bir foydalanuvchiga shaxsiy javob beradi (Telegramdagidek),
# yoki butun bo'limga e'lon yuboradi: Telegram + ilova ichida ovozli oyna («Tushundim» bosilguncha yopilmaydi).
SUP_SECTIONS = ("client", "courier", "partner")
SUP_LABELS = {"client": "Mijozlar", "courier": "Kuryerlar", "partner": "Hamkorlar"}
SUP_MAX_LEN = 1500
SUP_RATE_PER_HOUR = 30
ANN_VISIBLE_DAYS = 7
support_admin_router = APIRouter(tags=["Chat va e'lonlar"])


def _sup_msg(m: SupportMessage) -> dict:
    return {"id": m.id, "sender": m.sender, "text": m.text, "at": m.created_at.isoformat() if m.created_at else None}


async def sup_thread(db: AsyncSession, user: User, section: str) -> dict:
    msgs = (await db.execute(select(SupportMessage).where(SupportMessage.user_id == user.id, SupportMessage.section == section)
                             .order_by(SupportMessage.id.desc()).limit(100))).scalars().all()
    msgs.reverse()
    changed = False
    for m in msgs:
        if m.sender == "staff" and not m.read_by_user:
            m.read_by_user = True
            changed = True
    if changed:
        await db.commit()
    return {"messages": [_sup_msg(m) for m in msgs]}


async def sup_send(db: AsyncSession, user: User, section: str, text: str) -> dict:
    text = (text or "").strip()[:SUP_MAX_LEN]
    if not text:
        raise HTTPException(status_code=400, detail="Xabar matnini yozing")
    since = datetime.utcnow() - timedelta(hours=1)
    cnt = (await db.execute(select(func.count(SupportMessage.id)).where(
        SupportMessage.user_id == user.id, SupportMessage.sender == "user", SupportMessage.created_at > since))).scalar() or 0
    if cnt >= SUP_RATE_PER_HOUR:
        raise HTTPException(status_code=429, detail="Juda ko'p xabar yubordingiz. Birozdan keyin yozing.")
    db.add(SupportMessage(user_id=user.id, section=section, sender="user", text=text, read_by_user=True, read_by_staff=False))
    await db.commit()
    return {"ok": True}


async def sup_poll(db: AsyncSession, user: User, section: str) -> dict:
    unread = (await db.execute(select(func.count(SupportMessage.id)).where(
        SupportMessage.user_id == user.id, SupportMessage.section == section,
        SupportMessage.sender == "staff", SupportMessage.read_by_user == False))).scalar() or 0  # noqa: E712
    since = datetime.utcnow() - timedelta(days=ANN_VISIBLE_DAYS)
    acked = select(AnnouncementAck.announcement_id).where(AnnouncementAck.user_id == user.id)
    anns = (await db.execute(select(Announcement).where(
        Announcement.audience == section, Announcement.created_at > since, Announcement.id.notin_(acked))
        .order_by(Announcement.id))).scalars().all()
    my_city = user.city_id
    if section == "partner":
        my_city = (await db.execute(select(PartnerProfile.city_id).where(PartnerProfile.user_id == user.id))).scalars().first()
    anns = [a for a in anns if a.city_id is None or a.city_id == my_city]
    return {"unread": unread, "pending": [{"id": a.id, "text": a.text, "at": a.created_at.isoformat()} for a in anns]}


async def sup_ack(db: AsyncSession, user: User, section: str, aid: int) -> dict:
    a = (await db.execute(select(Announcement).where(Announcement.id == aid, Announcement.audience == section))).scalars().first()
    if a:
        exists = (await db.execute(select(AnnouncementAck.id).where(AnnouncementAck.announcement_id == aid, AnnouncementAck.user_id == user.id))).first()
        if not exists:
            db.add(AnnouncementAck(announcement_id=aid, user_id=user.id))
            await db.commit()
    return {"ok": True}


class SupTextBody(BaseModel):
    text: str


# ---- Hamkor (sessiya) ----
@partner_router.get("/support")
async def partner_sup_thread(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_partner_user)):
    return await sup_thread(db, current_user, "partner")


@partner_router.post("/support/send")
async def partner_sup_send(body: SupTextBody, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_partner_user)):
    return await sup_send(db, current_user, "partner", body.text)


@partner_router.get("/support/poll")
async def partner_sup_poll(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_partner_user)):
    return await sup_poll(db, current_user, "partner")


@partner_router.post("/support/ack/{aid}")
async def partner_sup_ack(aid: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_partner_user)):
    return await sup_ack(db, current_user, "partner", aid)


# ---- Kuryer (sessiya) ----
@courier_router_app.get("/support")
async def courier_sup_thread(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_courier_user)):
    return await sup_thread(db, current_user, "courier")


@courier_router_app.post("/support/send")
async def courier_sup_send(body: SupTextBody, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_courier_user)):
    return await sup_send(db, current_user, "courier", body.text)


@courier_router_app.get("/support/poll")
async def courier_sup_poll(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_courier_user)):
    return await sup_poll(db, current_user, "courier")


@courier_router_app.post("/support/ack/{aid}")
async def courier_sup_ack(aid: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_courier_user)):
    return await sup_ack(db, current_user, "courier", aid)


# ---- Mijoz (Telegram initData yoki ilova tokeni) ----
class ShopSupBody(BaseModel):
    init_data: str
    text: Optional[str] = ""
    id: Optional[int] = None


async def _shop_sup_user(db: AsyncSession, init_data: str) -> User:
    tg_user = _get_telegram_user_or_403(init_data)
    u = (await db.execute(select(User).where(_ident_cond(tg_user), User.role == UserRole.CLIENT))).scalars().first()
    if not u or not u.is_active:
        raise HTTPException(status_code=404, detail="Foydalanuvchi topilmadi")
    return u


@shop_router.post("/support")
async def shop_sup_thread(body: ShopSupBody, db: AsyncSession = Depends(get_db)):
    return await sup_thread(db, await _shop_sup_user(db, body.init_data), "client")


@shop_router.post("/support/send")
async def shop_sup_send(body: ShopSupBody, db: AsyncSession = Depends(get_db)):
    return await sup_send(db, await _shop_sup_user(db, body.init_data), "client", body.text or "")


@shop_router.post("/support/poll")
async def shop_sup_poll(body: ShopSupBody, db: AsyncSession = Depends(get_db)):
    return await sup_poll(db, await _shop_sup_user(db, body.init_data), "client")


@shop_router.post("/support/ack")
async def shop_sup_ack(body: ShopSupBody, db: AsyncSession = Depends(get_db)):
    return await sup_ack(db, await _shop_sup_user(db, body.init_data), "client", int(body.id or 0))


# ---- Admin / operator ----
def _sup_scope_cond(me: User, section: str):
    """Operator o'z shahri bilan cheklanadi (egasi va shaharsiz operator — cheklovsiz → None)."""
    if not (me.role == UserRole.ADMIN and me.city_id is not None):
        return None
    if section == "partner":
        return User.id.in_(select(PartnerProfile.user_id).where(PartnerProfile.city_id == me.city_id))
    return User.city_id == me.city_id


async def _sup_check_scope(db: AsyncSession, me: User, user_id: int, section: str):
    cond = _sup_scope_cond(me, section)
    if cond is None:
        return
    ok = (await db.execute(select(User.id).where(User.id == user_id, cond))).first()
    if not ok:
        raise HTTPException(status_code=403, detail="Bu foydalanuvchi sizning shahringizga tegishli emas")


def _sup_section(section: str) -> str:
    if section not in SUP_SECTIONS:
        raise HTTPException(status_code=400, detail="Bo'lim noto'g'ri")
    return section


@support_admin_router.get("/admin/support/unread")
async def admin_sup_unread(db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("xabarlar"))):
    out = {}
    for sec in SUP_SECTIONS:
        q = select(func.count(func.distinct(SupportMessage.user_id))).where(
            SupportMessage.section == sec, SupportMessage.sender == "user", SupportMessage.read_by_staff == False)  # noqa: E712
        cond = _sup_scope_cond(current_user, sec)
        if cond is not None:
            q = q.where(SupportMessage.user_id.in_(select(User.id).where(cond)))
        out[sec] = (await db.execute(q)).scalar() or 0
    out["total"] = sum(out.values())
    return out


@support_admin_router.get("/admin/support/threads")
async def admin_sup_threads(section: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("xabarlar"))):
    section = _sup_section(section)
    lq = select(func.max(SupportMessage.id)).where(SupportMessage.section == section)
    cond = _sup_scope_cond(current_user, section)
    if cond is not None:
        lq = lq.where(SupportMessage.user_id.in_(select(User.id).where(cond)))
    last_ids = (await db.execute(lq.group_by(SupportMessage.user_id))).scalars().all()
    if not last_ids:
        return {"threads": []}
    last = (await db.execute(select(SupportMessage).where(SupportMessage.id.in_(last_ids)).order_by(SupportMessage.id.desc()))).scalars().all()
    unread = dict((await db.execute(select(SupportMessage.user_id, func.count(SupportMessage.id)).where(
        SupportMessage.section == section, SupportMessage.sender == "user", SupportMessage.read_by_staff == False)  # noqa: E712
        .group_by(SupportMessage.user_id))).all())
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_([m.user_id for m in last])))).scalars().all()}
    out = []
    for m in last:
        u = users.get(m.user_id)
        out.append({"user_id": m.user_id, "name": u.full_name if u else "?", "phone": u.phone_number if u else "",
                    "last": m.text[:80], "last_sender": m.sender, "at": m.created_at.isoformat(), "unread": unread.get(m.user_id, 0)})
    out.sort(key=lambda t: t["at"], reverse=True)
    out.sort(key=lambda t: 0 if t["unread"] else 1)
    return {"threads": out}


@support_admin_router.get("/admin/support/thread/{user_id}")
async def admin_sup_thread(user_id: int, section: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("xabarlar"))):
    section = _sup_section(section)
    await _sup_check_scope(db, current_user, user_id, section)
    u = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    if not u:
        raise HTTPException(status_code=404, detail="Foydalanuvchi topilmadi")
    msgs = (await db.execute(select(SupportMessage).where(SupportMessage.user_id == user_id, SupportMessage.section == section)
                             .order_by(SupportMessage.id.desc()).limit(200))).scalars().all()
    msgs.reverse()
    for m in msgs:
        if m.sender == "user" and not m.read_by_staff:
            m.read_by_staff = True
    await db.commit()
    return {"user": {"id": u.id, "name": u.full_name, "phone": u.phone_number, "telegram": bool(u.telegram_id)},
            "messages": [_sup_msg(m) for m in msgs]}


@support_admin_router.post("/admin/support/thread/{user_id}/reply")
async def admin_sup_reply(user_id: int, section: str, body: SupTextBody, db: AsyncSession = Depends(get_db),
                          current_user: User = Depends(require_section("xabarlar"))):
    section = _sup_section(section)
    text = (body.text or "").strip()[:SUP_MAX_LEN]
    if not text:
        raise HTTPException(status_code=400, detail="Javob matnini yozing")
    await _sup_check_scope(db, current_user, user_id, section)
    u = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    if not u:
        raise HTTPException(status_code=404, detail="Foydalanuvchi topilmadi")
    db.add(SupportMessage(user_id=user_id, section=section, sender="staff", staff_id=current_user.id, text=text,
                          read_by_staff=True, read_by_user=False))
    await db.commit()
    tg = False
    if u.telegram_id:
        try:
            tg = (await send_telegram_message(u.telegram_id, "💬 <b>ZooMo administratsiyasi javobi</b>\n\n" + html.escape(text)
                                              + "\n\n<i>Javob berish uchun ilovadagi chat bo'limiga yozing.</i>")) is not False
        except Exception as e:
            print(f"[SUPPORT] telegram yuborilmadi: {e!r}")
    return {"ok": True, "telegram": tg}


class AnnounceBody(BaseModel):
    audience: str
    text: str
    city_id: Optional[int] = None   # None = barcha shaharlar (faqat egasi tanlaydi)


async def _ann_recipients(db: AsyncSession, audience: str, city_id: Optional[int] = None) -> list:
    if audience == "partner" and city_id is not None:
        cond = User.id.in_(select(PartnerProfile.user_id).where(PartnerProfile.application_status != "pending", PartnerProfile.city_id == city_id))
    elif audience == "courier" and city_id is not None:
        cond = and_(User.city_id == city_id, User.id.in_(select(CourierProfile.user_id).where(CourierProfile.application_status != "pending")))
    elif audience == "client" and city_id is not None:
        cond = and_(User.role == UserRole.CLIENT, User.city_id == city_id)
    elif audience == "courier":
        cond = User.id.in_(select(CourierProfile.user_id).where(CourierProfile.application_status != "pending"))
    elif audience == "partner":
        cond = User.id.in_(select(PartnerProfile.user_id).where(PartnerProfile.application_status != "pending"))
    else:
        cond = User.role == UserRole.CLIENT
    return (await db.execute(select(User).where(cond, User.is_active == True))).scalars().all()  # noqa: E712


@support_admin_router.post("/admin/support/announce")
async def admin_sup_announce(body: AnnounceBody, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_owner)):
    """Faqat EGASI: bo'limga (hamkor / kuryer / mijoz) — barcha shaharlarga yoki tanlangan shaharga — majburiy e'lon:
    Telegram + ilova ichida «Tushundim» bosilguncha chiqadi. Operator e'lon bera olmaydi (faqat javob yozadi)."""
    audience = _sup_section(body.audience)
    text = (body.text or "").strip()[:SUP_MAX_LEN]
    if len(text) < 2:
        raise HTTPException(status_code=400, detail="E'lon matnini yozing")
    scope_city = body.city_id
    if scope_city is not None and not (await db.execute(select(City.id).where(City.id == scope_city))).first():
        raise HTTPException(status_code=404, detail="Shahar topilmadi")
    ann = Announcement(audience=audience, text=text, created_by_id=current_user.id, city_id=scope_city)
    db.add(ann)
    await db.commit()
    users = await _ann_recipients(db, audience, scope_city)
    ann_id = ann.id
    msg = "📢 <b>ZooMo — muhim e'lon</b>\n\n" + html.escape(text)
    targets = [u.telegram_id for u in users if u.telegram_id]

    async def _run():
        import asyncio as _a
        sent = 0
        for tid in targets:
            try:
                if (await send_telegram_message(tid, msg)) is not False:
                    sent += 1
            except Exception as e:
                print(f"[E'LON] yuborilmadi: {e!r}")
            await _a.sleep(0.05)
        async with AsyncSessionLocal() as db2:
            a2 = (await db2.execute(select(Announcement).where(Announcement.id == ann_id))).scalars().first()
            if a2:
                a2.sent_count = sent
                await db2.commit()

    import asyncio as _asyncio
    _asyncio.create_task(_run())
    return {"ok": True, "recipients": len(users), "telegram": len(targets)}


@support_admin_router.get("/admin/support/announcements")
async def admin_sup_announcements(audience: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("xabarlar"))):
    audience = _sup_section(audience)
    aq = select(Announcement).where(Announcement.audience == audience)
    if current_user.role == UserRole.ADMIN and current_user.city_id is not None:
        aq = aq.where(or_(Announcement.city_id == current_user.city_id, Announcement.city_id.is_(None)))
    rows = (await db.execute(aq.order_by(Announcement.id.desc()).limit(15))).scalars().all()
    _cn = {c.id: c.name for c in (await db.execute(select(City))).scalars().all()}
    acks = dict((await db.execute(select(AnnouncementAck.announcement_id, func.count(AnnouncementAck.id)).where(
        AnnouncementAck.announcement_id.in_([a.id for a in rows] or [0])).group_by(AnnouncementAck.announcement_id))).all())
    return {"items": [{"id": a.id, "text": a.text, "at": a.created_at.isoformat(), "sent": a.sent_count or 0, "seen": acks.get(a.id, 0),
                       "city": _cn.get(a.city_id) if a.city_id else "Barcha shaharlar"} for a in rows]}


# ==================== BAHOLASH (platforma, kuryer, hamkor) ====================
RATING_WINDOW_DAYS = 3
LOW_STARS = 3


class RatingBody(BaseModel):
    init_data: str
    order_id: Optional[int] = None
    platform: Optional[int] = None
    courier: Optional[int] = None
    partner: Optional[int] = None
    comment: Optional[str] = ""


def _star(v):
    if v is None:
        return None
    try:
        v = int(v)
    except Exception:
        raise HTTPException(status_code=400, detail="Baho 1 dan 5 gacha bo'lsin")
    if not 1 <= v <= 5:
        raise HTTPException(status_code=400, detail="Baho 1 dan 5 gacha bo'lsin")
    return v


@shop_router.post("/rating/pending")
async def shop_rating_pending(body: RatingBody, db: AsyncSession = Depends(get_db)):
    """Oxirgi yetkazilgan, hali baholanmagan buyurtma (so'nggi 3 kun) — baholash oynasi uchun."""
    client = await _shop_sup_user(db, body.init_data)
    since = datetime.utcnow() - timedelta(days=RATING_WINDOW_DAYS)
    rated = select(OrderRating.order_id).where(OrderRating.user_id == client.id)
    o = (await db.execute(select(Order).where(Order.client_id == client.id, Order.status == OrderStatus.DELIVERED,
                                              Order.created_at > since, Order.id.notin_(rated))
                          .order_by(Order.id.desc()).limit(1))).scalars().first()
    if not o:
        return {"order": None}
    partner = (await db.execute(select(PartnerProfile).where(PartnerProfile.id == o.partner_id))).scalars().first() if o.partner_id else None
    courier = (await db.execute(select(User).where(User.id == o.courier_id))).scalars().first() if o.courier_id else None
    return {"order": {"id": o.id, "partner": partner.brand_name if partner else "", "courier": courier.full_name if courier else "",
                      "has_courier": bool(courier), "has_partner": bool(partner)}}


@shop_router.post("/rating/submit")
async def shop_rating_submit(body: RatingBody, db: AsyncSession = Depends(get_db)):
    client = await _shop_sup_user(db, body.init_data)
    o = (await db.execute(select(Order).where(Order.id == int(body.order_id or 0), Order.client_id == client.id))).scalars().first()
    if not o or o.status != OrderStatus.DELIVERED:
        raise HTTPException(status_code=400, detail="Faqat yetkazilgan buyurtmani baholash mumkin")
    if (await db.execute(select(OrderRating.id).where(OrderRating.order_id == o.id))).first():
        raise HTTPException(status_code=400, detail="Bu buyurtma allaqachon baholangan")
    pl, co, pa = _star(body.platform), _star(body.courier), _star(body.partner)
    if not o.courier_id:
        co = None
    if not o.partner_id:
        pa = None
    if pl is None and co is None and pa is None:
        raise HTTPException(status_code=400, detail="Kamida bittasini baholang")
    comment = (body.comment or "").strip()[:500]
    db.add(OrderRating(order_id=o.id, user_id=client.id, courier_user_id=o.courier_id, partner_id=o.partner_id,
                       platform_stars=pl, courier_stars=co, partner_stars=pa, comment=comment or None))
    low = any(v is not None and v <= LOW_STARS for v in (pl, co, pa))
    if low:
        # Past baho — administratsiyaga chat xabari bo'lib tushadi (o'qilmagan sifatida ko'rinadi)
        parts = [f"{n} {v}" for n, v in (("platforma", pl), ("kuryer", co), ("hamkor", pa)) if v is not None]
        txt = f"⭐ Past baho — buyurtma #{o.id}: " + ", ".join(parts) + (f". Izoh: {comment}" if comment else "")
        db.add(SupportMessage(user_id=client.id, section="client", sender="user", text=txt[:SUP_MAX_LEN], read_by_user=True, read_by_staff=False))
    await db.commit()
    return {"ok": True, "low": low}


@partner_router.get("/rating")
async def partner_rating(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_partner_user)):
    pid = current_user.partner_profile.id
    avg, n = (await db.execute(select(func.avg(OrderRating.partner_stars), func.count(OrderRating.partner_stars))
                               .where(OrderRating.partner_id == pid, OrderRating.partner_stars.isnot(None)))).first()
    return {"avg": round(float(avg), 2) if avg else None, "count": n or 0}


@courier_router_app.get("/rating")
async def courier_rating(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_courier_user)):
    avg, n = (await db.execute(select(func.avg(OrderRating.courier_stars), func.count(OrderRating.courier_stars))
                               .where(OrderRating.courier_user_id == current_user.id, OrderRating.courier_stars.isnot(None)))).first()
    return {"avg": round(float(avg), 2) if avg else None, "count": n or 0}


@support_admin_router.get("/admin/ratings/summary")
async def admin_ratings_summary(city_id: Optional[int] = None, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("xabarlar"))):
    scope = []
    if current_user.role == UserRole.ADMIN and current_user.city_id is not None:
        city_id = current_user.city_id          # operator — faqat o'z shahri
    if city_id is not None:
        scope.append(OrderRating.user_id.in_(select(User.id).where(User.city_id == city_id)))
    pl = (await db.execute(select(func.avg(OrderRating.platform_stars), func.count(OrderRating.platform_stars))
                           .where(OrderRating.platform_stars.isnot(None), *scope))).first()

    async def worst(col, idcol, model, namecol):
        rows = (await db.execute(select(idcol, func.avg(col), func.count(col)).where(col.isnot(None), idcol.isnot(None), *scope)
                                 .group_by(idcol).order_by(func.avg(col)).limit(5))).all()
        out = []
        for rid, avg, n in rows:
            nm = (await db.execute(select(namecol).where(model.id == rid))).scalars().first()
            out.append({"name": nm or "?", "avg": round(float(avg), 2), "count": n})
        return out
    last = (await db.execute(select(OrderRating).where(*scope).order_by(OrderRating.id.desc()).limit(15))).scalars().all()
    names = {u.id: u.full_name for u in (await db.execute(select(User).where(User.id.in_([r.user_id for r in last] or [0])))).scalars().all()}
    return {"platform": {"avg": round(float(pl[0]), 2) if pl[0] else None, "count": pl[1] or 0},
            "worst_couriers": await worst(OrderRating.courier_stars, OrderRating.courier_user_id, User, User.full_name),
            "worst_partners": await worst(OrderRating.partner_stars, OrderRating.partner_id, PartnerProfile, PartnerProfile.brand_name),
            "last": [{"client": names.get(r.user_id, "?"), "order": r.order_id, "p": r.platform_stars, "c": r.courier_stars, "h": r.partner_stars,
                      "comment": r.comment or "", "at": r.created_at.isoformat()} for r in last]}


# ---- Reyting bonusi: shaharlar kesimida (faqat egasi boshqaradi) ----
class RatingBonusBody(BaseModel):
    enabled: bool = False
    min_rating: float = 4.7
    min_count: int = 5
    bonus_amount: float = 0.0


@support_admin_router.get("/admin/rating-bonus")
async def admin_rating_bonus_list(db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    cities = (await db.execute(select(City).order_by(City.name))).scalars().all()
    cfgs = {c.city_id: c for c in (await db.execute(select(CityRatingBonus))).scalars().all()}
    couriers = (await db.execute(select(User).where(User.id.in_(select(CourierProfile.user_id)), User.is_active == True))).scalars().all()  # noqa: E712
    rows = (await db.execute(select(OrderRating.courier_user_id, func.avg(OrderRating.courier_stars), func.count(OrderRating.courier_stars))
                             .where(OrderRating.courier_stars.isnot(None)).group_by(OrderRating.courier_user_id))).all()
    rt = {uid: (float(a), n) for uid, a, n in rows}
    out = []
    for c in cities:
        cfg = cfgs.get(c.id)
        mine = [u for u in couriers if u.city_id == c.id]
        item = {"city_id": c.id, "city": c.name, "enabled": bool(cfg and cfg.enabled), "min_rating": cfg.min_rating if cfg else 4.7,
                "min_count": cfg.min_count if cfg else 5, "bonus_amount": cfg.bonus_amount if cfg else 0.0, "couriers": []}
        for u in mine:
            a, n = rt.get(u.id, (None, 0))
            ok = bool(cfg and cfg.enabled and (cfg.bonus_amount or 0) > 0 and n >= (cfg.min_count or 0) and a is not None and a >= (cfg.min_rating or 0))
            item["couriers"].append({"name": u.full_name, "avg": round(a, 2) if a else None, "count": n, "gets_bonus": ok})
        item["couriers"].sort(key=lambda x: (-(x["avg"] or 0), x["name"]))
        out.append(item)
    return {"cities": out}


@support_admin_router.post("/admin/rating-bonus/{city_id}")
async def admin_rating_bonus_save(city_id: int, body: RatingBonusBody, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    if not (await db.execute(select(City.id).where(City.id == city_id))).first():
        raise HTTPException(status_code=404, detail="Shahar topilmadi")
    if not (1.0 <= body.min_rating <= 5.0):
        raise HTTPException(status_code=400, detail="Minimal baho 1 dan 5 gacha bo'lsin")
    if body.min_count < 0 or body.bonus_amount < 0 or body.bonus_amount > 10_000_000:
        raise HTTPException(status_code=400, detail="Qiymat noto'g'ri")
    cfg = (await db.execute(select(CityRatingBonus).where(CityRatingBonus.city_id == city_id))).scalars().first()
    if not cfg:
        cfg = CityRatingBonus(city_id=city_id)
        db.add(cfg)
    cfg.enabled = bool(body.enabled)
    cfg.min_rating = body.min_rating
    cfg.min_count = body.min_count
    cfg.bonus_amount = body.bonus_amount
    await db.commit()
    return {"ok": True}


# Routerlarni ulash
app.include_router(sec_router)
app.include_router(courier_router_app)

# ==================== MIJOZ BUYURTMANI RAD ETISHI (faqat onlayn/P2P to'lovda) ====================
# Naqd to'lovli buyurtmani rad etib bo'lmaydi. Onlayn (P2P, tasdiqlangan) buyurtmada pul holatga qarab taqsimlanadi:
#   created      — do'kon hali qabul qilmagan: pul to'liq qaytadi
#   preparing    — do'kon qabul qilib tayyorlashni boshlagan: mahsulot narxining X% (standart 60%) ushlab qolinadi,
#                  qolgani va yetkazish haqi qaytadi; ushlangan summa hamkorga (komissiya ayirilib) o'tadi
#   ready        — tayyor, kuryer izlanmoqda: mahsulotning to'liq puli ushlanadi (hamkorga), yetkazish haqi qaytadi
#   on_the_way   — kuryer yo'lda: pul QAYTARILMAYDI. Mahsulot kuryerda qoladi; kuryer balansidan mahsulot puli
#                  (hamkor ulushi + komissiya) yechiladi, hamkorga komissiya ayirilib o'tkaziladi, mijozning to'lovi
#                  kuryerga o'tadi; yetkazish haqi to'liq (komissiyasiz) kuryerda qoladi.
cancel_router = APIRouter(tags=["Buyurtmani rad etish"])

async def _apply_staff_cancel(db: AsyncSession, order: Order, by: str) -> bool:
    """Buyurtmani hamkor/admin/operator (yoki to'lov rad etilgani uchun) bekor qilganda MIJOZGA pulini qaytarish:
    ishlatilgan keshbek balansga qaytadi; onlayn (tasdiqlangan P2P) to'langan bo'lsa — naqd qismi to'liq
    "qaytariladigan pullar" ro'yxatiga tushadi. Bir buyurtma uchun faqat BIR MARTA ishlaydi.
    by: by_partner | by_admin | payment_rejected. True — shu safar hisoblangan bo'lsa."""
    if order.cancel_stage or order.financials_applied:
        return False
    S = float(order.total_price or 0)
    D = float(order.delivery_fee or 0)
    cb = float(order.cashback_used or 0)
    pd = max(0.0, float(order.discount_amount or 0) - cb)
    paid_online = order.payment_method == "p2p" and bool(order.payment_verified)
    cb_back, r_cash = cb, 0.0
    if paid_online:
        V = max(0.0, S + D - pd)
        cb_back = min(cb, V)
        r_cash = V - cb_back
    if cb_back > 0:
        client = (await db.execute(select(User).where(User.id == order.client_id).with_for_update())).scalars().first()
        if client:
            client.cashback_balance = (client.cashback_balance or 0) + cb_back
    order.cancel_stage = by
    order.cancelled_at = datetime.utcnow()
    order.cancel_forfeit = 0.0
    order.financials_applied = True
    if r_cash > 0.5:
        order.refund_amount = r_cash
        order.refund_status = "pending"
    order._staff_cancel_info = (cb_back, r_cash)
    return True


async def _notify_staff_cancel(db: AsyncSession, order: Order) -> None:
    info = getattr(order, "_staff_cancel_info", None)
    if not info:
        return
    cb_back, r_cash = info
    if cb_back <= 0 and r_cash <= 0.5:
        return
    try:
        cl = (await db.execute(select(User).where(User.id == order.client_id))).scalars().first()
        if cl and cl.telegram_id:
            parts = []
            if r_cash > 0.5:
                parts.append(f"{_fm(r_cash)} so'm tez orada kartangizga qaytariladi")
            if cb_back > 0:
                parts.append(f"{_fm(cb_back)} so'm keshbek balansingizga qaytarildi")
            await send_telegram_message(cl.telegram_id, f"❌ Buyurtma #{order.id} bekor qilindi. " + "; ".join(parts) + ".")
    except Exception as e:
        print(f"Qaytim xabari yuborilmadi: {e}")

_CANCEL_OPEN = (OrderStatus.CREATED, OrderStatus.ACCEPTED_BY_PARTNER, OrderStatus.PREPARING,
                OrderStatus.LOOKING_FOR_COURIER, OrderStatus.ON_THE_WAY)


def _fm(v: float) -> str:
    return f"{float(v):,.0f}".replace(",", " ")


class CancelBody(BaseModel):
    init_data: str
    confirm: bool = False


async def _cancel_quote(db: AsyncSession, order: Order, setting, partner) -> dict:
    S = float(order.total_price or 0)
    D = float(order.delivery_fee or 0)
    cb = float(order.cashback_used or 0)
    pd = max(0.0, float(order.discount_amount or 0) - cb)      # promo chegirma (platforma hisobidan)
    out = {"can_cancel": False, "reason": "", "stage": None, "title": "", "lines": [], "forfeit": 0.0,
           "cashback_back": 0.0, "refund_cash": 0.0, "partner_credit": 0.0}
    if order.payment_method != "p2p":
        out["reason"] = "Naqd to'lovli buyurtmani rad etib bo'lmaydi."
        return out
    if order.status not in _CANCEL_OPEN:
        out["reason"] = "Bu buyurtmani endi rad etib bo'lmaydi."
        return out
    pct = (60.0 if setting.cancel_prep_percent is None else float(setting.cancel_prep_percent)) / 100.0
    c = ((partner.commission_rate if partner else 0) or 0) / 100.0

    if not order.payment_verified:
        pending = (await db.execute(select(func.count(PaymentReceipt.id)).where(
            PaymentReceipt.kind == "client_order", PaymentReceipt.order_id == order.id, PaymentReceipt.status == "pending"))).scalar() or 0
        if pending or order.payment_receipt_file_id:
            out["reason"] = "To'lov chekingiz tekshirilmoqda. Bekor qilish uchun administratsiyaga yozing."
            return out
        out.update(can_cancel=True, stage="unpaid", title="Buyurtmani rad etish",
                   cashback_back=cb, lines=["To'lov hali qilinmagan — buyurtma bekor qilinadi, hech narsa yechilmaydi."])
        return out

    st = order.status
    if st == OrderStatus.CREATED:
        stage, forfeit = "created", 0.0
        lines = ["Do'kon hali buyurtmani qabul qilmagan — to'langan pulingiz TO'LIQ qaytariladi."]
    elif st in (OrderStatus.ACCEPTED_BY_PARTNER, OrderStatus.PREPARING):
        stage, forfeit = "preparing", S * pct
        lines = [f"⚠️ Do'kon buyurtmani qabul qilib tayyorlashni boshlagan.",
                 f"Mahsulot narxining {pct * 100:g}% ({_fm(forfeit)} so'm) ushlab qolinadi.",
                 "Qolgan summa va yetkazish haqi qaytariladi."]
    elif st == OrderStatus.LOOKING_FOR_COURIER:
        stage, forfeit = "ready", S
        lines = ["⚠️ Buyurtma tayyor bo'lgan va kuryer izlanmoqda.",
                 f"Mahsulotning to'liq puli ({_fm(S)} so'm) ushlab qolinadi.",
                 f"Yetkazish haqi ({_fm(D)} so'm) qaytariladi."]
    else:
        stage, forfeit = "on_the_way", S + D
        lines = ["⚠️ Kuryer yo'lda — buyurtma kuryerga topshirilgan.",
                 "To'langan pul QAYTARILMAYDI."]
    V = max(0.0, S + D - pd)                         # mijoz to'lagan qiymat (pul + ishlatilgan keshbek)
    forfeit_eff = min(forfeit, V)
    R_total = V - forfeit_eff
    cb_back = min(cb, R_total)
    R_cash = R_total - cb_back
    f_prod = min(forfeit_eff, S)
    out.update(can_cancel=True, stage=stage, title="Buyurtmani rad etish", lines=lines, forfeit=forfeit_eff,
               cashback_back=cb_back, refund_cash=R_cash, partner_credit=f_prod * (1 - c))
    if stage != "on_the_way":
        lines.append(f"Sizga qaytariladi: {_fm(R_total)} so'm" + (f" (shundan {_fm(cb_back)} so'm keshbek balansingizga)" if cb_back > 0 else "") + ".")
    return out


async def _client_for_cancel(db: AsyncSession, init_data: str) -> User:
    tg_user = _get_telegram_user_or_403(init_data)
    client = (await db.execute(select(User).where(_ident_cond(tg_user), User.role == UserRole.CLIENT))).scalars().first()
    if not client:
        raise HTTPException(status_code=404, detail="Mijoz topilmadi")
    return client


@cancel_router.post("/api/shop/orders/{order_id}/cancel-info")
async def shop_cancel_info(order_id: int, body: CancelBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    order = (await db.execute(select(Order).where(Order.id == order_id, Order.client_id == client.id))).scalars().first()
    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    partner = (await db.execute(select(PartnerProfile).where(PartnerProfile.id == order.partner_id))).scalars().first() if order.partner_id else None
    q = await _cancel_quote(db, order, await _get_or_create_setting(db), partner)
    return q


@cancel_router.post("/api/shop/orders/{order_id}/cancel")
async def shop_cancel_order(order_id: int, body: CancelBody, db: AsyncSession = Depends(get_db)):
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Tasdiqlash kerak")
    client = await _client_for_cancel(db, body.init_data)
    order = (await db.execute(select(Order).where(Order.id == order_id, Order.client_id == client.id).with_for_update())).scalars().first()
    if not order:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    if order.financials_applied or order.cancel_stage:
        raise HTTPException(status_code=400, detail="Bu buyurtma allaqachon yopilgan")
    setting = await _get_or_create_setting(db)
    partner = (await db.execute(select(PartnerProfile).where(PartnerProfile.id == order.partner_id).with_for_update())).scalars().first() if order.partner_id else None
    q = await _cancel_quote(db, order, setting, partner)
    if not q["can_cancel"]:
        raise HTTPException(status_code=400, detail=q["reason"] or "Rad etib bo'lmaydi")

    stage = q["stage"]
    S = float(order.total_price or 0)
    D = float(order.delivery_fee or 0)
    now = datetime.utcnow()
    courier_user = None
    # 1) Mijoz: keshbek qaytadi, naqd qaytim "kutilmoqda" holatida adminga tushadi
    if q["cashback_back"] > 0:
        client.cashback_balance = (client.cashback_balance or 0) + q["cashback_back"]
    # 2) Hamkor: ushlab qolingan mahsulot puli (komissiya ayirilib)
    if partner and q["partner_credit"] > 0:
        partner.balance = (partner.balance or 0) + q["partner_credit"]
        db.add(Transaction(partner_id=partner.id, type=TransactionType.DEPOSIT, amount=q["partner_credit"],
                           note=f"Mijoz rad etgan buyurtma #{order.id} ({'tayyorlanayotgan' if stage == 'preparing' else 'tayyor' if stage == 'ready' else 'yo`lda'}) — kompensatsiya (komissiya ayirilgan)",
                           created_by_id=None))
    # 3) Kuryer yo'lda: mahsulot kuryerda qoladi
    if stage == "on_the_way" and order.courier_id:
        courier_user = (await db.execute(select(User).where(User.id == order.courier_id).options(selectinload(User.courier_profile)))).scalars().first()
        if courier_user and courier_user.courier_profile:
            cp = courier_user.courier_profile
            cp.balance = (cp.balance or 0) - S
            db.add(Transaction(user_id=courier_user.id, type=TransactionType.WITHDRAWAL, amount=S,
                               note=f"Rad etilgan buyurtma #{order.id} — mahsulot sizda qoldi: mahsulot va komissiya balansingizdan yechildi",
                               created_by_id=None))
            cp.balance = (cp.balance or 0) + S + D
            db.add(Transaction(user_id=courier_user.id, type=TransactionType.ORDER_FEE, amount=S + D,
                               note=f"Rad etilgan buyurtma #{order.id} — mijoz to'lovi (mahsulot {_fm(S)} + yetkazish {_fm(D)}); yetkazish haqi komissiyasiz",
                               created_by_id=None))
    # 4) Buyurtma holati
    order.status = OrderStatus.CANCELLED
    order.financials_applied = True
    order.cancel_stage = stage
    order.cancelled_at = now
    order.cancel_forfeit = q["forfeit"]
    if q["refund_cash"] > 0.5:
        order.refund_amount = q["refund_cash"]
        order.refund_status = "pending"
    await db.commit()

    # Xabarlar
    try:
        label = {"unpaid": "to'lovsiz", "created": "qabul qilinmagan", "preparing": "tayyorlanayotgan", "ready": "tayyor (kuryer kutilmoqda)", "on_the_way": "kuryer yo'lda"}[stage]
        if partner:
            pu = (await db.execute(select(User).where(User.id == partner.user_id))).scalars().first()
            if pu and pu.telegram_id:
                extra = f"\nKompensatsiya: {_fm(q['partner_credit'])} so'm balansingizga qo'shildi." if q["partner_credit"] > 0 else ""
                await send_telegram_message(pu.telegram_id, f"❌ Mijoz buyurtma #{order.id} ni rad etdi ({label}).{extra}")
        if courier_user and courier_user.telegram_id:
            await send_telegram_message(courier_user.telegram_id,
                f"❌ Mijoz buyurtma #{order.id} ni rad etdi. Mahsulot sizda qoladi — do'kondan oling. "
                f"Mahsulot puli ({_fm(S)} so'm) balansingizdan yechildi, mijoz to'lovi va yetkazish haqi ({_fm(D)} so'm) qo'shildi.")
    except Exception as e:
        print(f"Rad etish bildirishnomasi yuborilmadi: {e}")
    msg = "Buyurtma bekor qilindi."
    if q["refund_cash"] > 0.5:
        msg += f" {_fm(q['refund_cash'])} so'm tez orada kartangizga qaytariladi."
    if q["cashback_back"] > 0:
        msg += f" {_fm(q['cashback_back'])} so'm keshbek balansingizga qaytarildi."
    if stage == "on_the_way":
        msg += " Kuryer yo'lda bo'lgani uchun to'lov qaytarilmaydi."
    return {"ok": True, "stage": stage, "message": msg, "refund_cash": q["refund_cash"], "cashback_back": q["cashback_back"], "forfeit": q["forfeit"]}


# ---- Admin: mijozga qaytariladigan pullar ----
async def _refund_viewer(db: AsyncSession, user: User):
    if user.role == UserRole.OWNER:
        return None
    if not await operator_can_see(db, user, "buyurtmalar") or user.city_id is None:
        raise HTTPException(status_code=403, detail="Bu bo'limni ko'rish huquqingiz yo'q")
    return user.city_id


@cancel_router.get("/admin/refunds")
async def admin_refunds(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    scope_city = await _refund_viewer(db, current_user)
    q = (select(Order, User, PartnerProfile).join(User, User.id == Order.client_id)
         .outerjoin(PartnerProfile, PartnerProfile.id == Order.partner_id)
         .where(Order.refund_status.in_(["pending", "paid"])).order_by(Order.refund_status.desc(), Order.id.desc()).limit(60))
    if scope_city is not None:
        q = q.where(PartnerProfile.city_id == scope_city)
    items = []
    for o, u, pr in (await db.execute(q)).all():
        items.append({"order_id": o.id, "client": u.full_name or "", "phone": u.phone_number or "", "amount": o.refund_amount or 0,
                      "status": o.refund_status, "stage": o.cancel_stage, "partner": pr.brand_name if pr else "—",
                      "at": o.cancelled_at.isoformat() if o.cancelled_at else None})
    return {"items": items}


@cancel_router.post("/admin/refunds/{order_id}/paid")
async def admin_refund_paid(order_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    scope_city = await _refund_viewer(db, current_user)
    o = (await db.execute(select(Order).where(Order.id == order_id).with_for_update())).scalars().first()
    if not o or o.refund_status != "pending":
        raise HTTPException(status_code=404, detail="Kutilayotgan qaytim topilmadi")
    if scope_city is not None:
        pr = (await db.execute(select(PartnerProfile).where(PartnerProfile.id == o.partner_id))).scalars().first()
        if not pr or pr.city_id != scope_city:
            raise HTTPException(status_code=404, detail="Kutilayotgan qaytim topilmadi")
    o.refund_status = "paid"
    o.refund_paid_at = datetime.utcnow()
    await db.commit()
    try:
        cl = (await db.execute(select(User).where(User.id == o.client_id))).scalars().first()
        if cl and cl.telegram_id:
            await send_telegram_message(cl.telegram_id, f"💸 Buyurtma #{o.id} bo'yicha {_fm(o.refund_amount or 0)} so'm kartangizga qaytarildi.")
    except Exception as e:
        print(f"Qaytim xabari yuborilmadi: {e}")
    return {"ok": True}


@cancel_router.post("/admin/settings/cancel-policy")
async def admin_set_cancel_policy(cancel_prep_percent: float = Form(..., allow_inf_nan=False), db: AsyncSession = Depends(get_db),
                                  owner: User = Depends(require_owner)):
    if cancel_prep_percent < 0 or cancel_prep_percent > 100:
        raise HTTPException(status_code=400, detail="Foiz 0 dan 100 gacha bo'lishi kerak")
    setting = await _get_or_create_setting(db)
    setting.cancel_prep_percent = cancel_prep_percent
    await db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)



# ==================== POCHTA (SHAXSIY BUYUM YUBORISH) ====================
# Do'kon buyurtmalaridan alohida xizmat: mijoz buyumni A manzildan B manzilga yuboradi, kuryer olib boradi.
# To'lov — naqd, kuryerga (yuboruvchi olishda YOKI qabul qiluvchi topshirishda to'laydi). Kuryer o'z ulushini
# o'zida qoldiradi, platforma ulushi yetkazilgach kuryer balansidan yechiladi. Qabul qiluvchi 4 xonali kodni
# kuryerga aytadi — shundan keyingina "Topshirildi" bo'ladi.
parcel_router = APIRouter(tags=["Pochta"])
PARCEL_SIZES = {"small": "Kichik (hujjat, kichik paket)", "medium": "O'rta (sumka, quti)", "large": "Katta (katta quti)"}
PARCEL_STATUS_UZ = {"new": "Kuryer izlanmoqda", "accepted": "Kuryer qabul qildi", "picked_up": "Kuryer yo'lda",
                    "delivered": "Topshirildi", "cancelled": "Bekor qilindi"}


def _parcel_cfg(setting) -> dict:
    def g(name, d):
        v = getattr(setting, name, None)
        return d if v is None else v
    return {"enabled": bool(g("parcel_enabled", False)), "base_fee": float(g("parcel_base_fee", 8000.0)),
            "per_km": float(g("parcel_per_km", 2000.0)), "medium_fee": float(g("parcel_medium_fee", 3000.0)),
            "large_fee": float(g("parcel_large_fee", 8000.0)), "courier_percent": float(g("parcel_courier_percent", 80.0)),
            "max_km": float(g("parcel_max_km", 30.0))}


def _parcel_price(cfg: dict, km: float, size: str) -> float:
    extra = {"small": 0.0, "medium": cfg["medium_fee"], "large": cfg["large_fee"]}.get(size, 0.0)
    raw = cfg["base_fee"] + cfg["per_km"] * km + extra
    return float(math.ceil(raw / 500.0) * 500)


def _parcel_km_or_400(setting, a_lat, a_lng, b_lat, b_lng, cfg) -> float:
    km = _road_km(setting, a_lat, a_lng, b_lat, b_lng)
    if km is None:
        raise HTTPException(status_code=400, detail="Olish va topshirish nuqtalarini xaritada belgilang")
    if km > cfg["max_km"]:
        raise HTTPException(status_code=400, detail=f"Masofa juda uzoq ({km:.1f} km). Eng ko'pi bilan {cfg['max_km']:g} km.")
    return km


def _parcel_phone_ok(v: str) -> str:
    digits = "".join(ch for ch in (v or "") if ch.isdigit())
    if len(digits) < 9 or len(digits) > 15:
        raise HTTPException(status_code=400, detail="Qabul qiluvchining telefon raqamini to'g'ri kiriting")
    return ("+" + digits) if (v or "").strip().startswith("+") or len(digits) > 9 else digits


class ParcelQuoteBody(BaseModel):
    init_data: str
    pickup_lat: float
    pickup_lng: float
    dropoff_lat: float
    dropoff_lng: float
    size: str = "small"


class ParcelCreateBody(ParcelQuoteBody):
    pickup_address: str
    dropoff_address: str
    receiver_name: str
    receiver_phone: str
    item_description: str
    comment: Optional[str] = None
    payer: str = "sender"


class ParcelIdBody(BaseModel):
    init_data: str
    id: int


def _parcel_public(o: ParcelOrder, with_code: bool) -> dict:
    d = {"id": o.id, "status": o.status, "status_label": PARCEL_STATUS_UZ.get(o.status, o.status),
         "pickup_address": o.pickup_address, "dropoff_address": o.dropoff_address, "receiver_name": o.receiver_name,
         "receiver_phone": o.receiver_phone, "item": o.item_description, "size": o.size, "fee": o.fee, "payer": o.payer,
         "distance_km": o.distance_km, "created_at": o.created_at.isoformat() if o.created_at else None,
         "courier_name": None, "courier_phone": None}
    if o.courier is not None and o.status in ("accepted", "picked_up", "delivered"):
        d["courier_name"] = o.courier.full_name
        d["courier_phone"] = o.courier.phone_number
    if with_code and o.status in ("new", "accepted", "picked_up"):
        d["code"] = o.delivery_code
    return d


@parcel_router.get("/api/shop/parcel/config")
async def parcel_config(db: AsyncSession = Depends(get_db)):
    cfg = _parcel_cfg(await _get_or_create_setting(db))
    await db.commit()
    return {**cfg, "sizes": PARCEL_SIZES}


@parcel_router.post("/api/shop/parcel/quote")
async def parcel_quote(body: ParcelQuoteBody, db: AsyncSession = Depends(get_db)):
    await _client_for_cancel(db, body.init_data)
    setting = await _get_or_create_setting(db)
    cfg = _parcel_cfg(setting)
    if not cfg["enabled"]:
        raise HTTPException(status_code=403, detail="Pochta xizmati hozircha ishlamayapti")
    if body.size not in PARCEL_SIZES:
        raise HTTPException(status_code=400, detail="Hajmni tanlang")
    km = _parcel_km_or_400(setting, body.pickup_lat, body.pickup_lng, body.dropoff_lat, body.dropoff_lng, cfg)
    return {"km": km, "fee": _parcel_price(cfg, km, body.size)}


@parcel_router.post("/api/shop/parcel/create")
async def parcel_create(body: ParcelCreateBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    setting = await _get_or_create_setting(db)
    cfg = _parcel_cfg(setting)
    if not cfg["enabled"]:
        raise HTTPException(status_code=403, detail="Pochta xizmati hozircha ishlamayapti")
    if body.size not in PARCEL_SIZES or body.payer not in ("sender", "receiver"):
        raise HTTPException(status_code=400, detail="Ma'lumot noto'g'ri")
    pa, da = (body.pickup_address or "").strip()[:300], (body.dropoff_address or "").strip()[:300]
    rn, item = (body.receiver_name or "").strip()[:100], (body.item_description or "").strip()[:300]
    if len(pa) < 3 or len(da) < 3 or len(rn) < 2 or len(item) < 2:
        raise HTTPException(status_code=400, detail="Barcha maydonlarni to'ldiring")
    rphone = _parcel_phone_ok(body.receiver_phone)
    if client.city_id is None:
        raise HTTPException(status_code=400, detail="Avval shaharingizni tanlang")
    active = (await db.execute(select(func.count(ParcelOrder.id)).where(
        ParcelOrder.client_id == client.id, ParcelOrder.status.in_(["new", "accepted", "picked_up"])))).scalar() or 0
    if active >= 5:
        raise HTTPException(status_code=400, detail="Bir vaqtning o'zida 5 tadan ortiq faol pochta yubora olmaysiz")
    km = _parcel_km_or_400(setting, body.pickup_lat, body.pickup_lng, body.dropoff_lat, body.dropoff_lng, cfg)
    fee = _parcel_price(cfg, km, body.size)
    earning = round(fee * cfg["courier_percent"] / 100.0)
    code = f"{secrets.randbelow(9000) + 1000}"
    o = ParcelOrder(client_id=client.id, city_id=client.city_id, status="new", pickup_address=pa,
                    pickup_lat=body.pickup_lat, pickup_lng=body.pickup_lng, dropoff_address=da,
                    dropoff_lat=body.dropoff_lat, dropoff_lng=body.dropoff_lng, receiver_name=rn, receiver_phone=rphone,
                    item_description=item, size=body.size, comment=(body.comment or "").strip()[:500] or None,
                    payer=body.payer, distance_km=km, fee=fee, courier_earning=earning, platform_share=fee - earning,
                    delivery_code=code)
    db.add(o)
    await db.commit()
    # Shahardagi onlayn kuryerlarga xabar
    try:
        rows = (await db.execute(
            select(User).join(CourierProfile, CourierProfile.user_id == User.id).where(
                User.city_id == client.city_id, CourierProfile.is_online == True, CourierProfile.is_blocked == False,
                User.telegram_id.isnot(None)).limit(40))).scalars().all()
        for cu in rows:
            await send_telegram_message(cu.telegram_id, f"📮 Yangi pochta buyurtmasi #{o.id}: {pa} → {da}\n"
                                                         f"Masofa {km:.1f} km · sizga {_fm(earning)} so'm. Kuryer kabinetini oching.")
    except Exception as e:
        print(f"Pochta xabari yuborilmadi: {e}")
    return {"ok": True, "id": o.id, "fee": fee, "code": code, "km": km}


class ParcelInitBody(BaseModel):
    init_data: str


@parcel_router.post("/api/shop/parcel/my")
async def parcel_my(body: ParcelInitBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    rows = (await db.execute(select(ParcelOrder).options(selectinload(ParcelOrder.courier))
                             .where(ParcelOrder.client_id == client.id).order_by(ParcelOrder.id.desc()).limit(30))).scalars().all()
    return {"items": [_parcel_public(o, True) for o in rows]}


@parcel_router.post("/api/shop/parcel/cancel")
async def parcel_cancel(body: ParcelIdBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    o = (await db.execute(select(ParcelOrder).where(ParcelOrder.id == body.id, ParcelOrder.client_id == client.id)
                          .with_for_update())).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    if o.status not in ("new", "accepted"):
        raise HTTPException(status_code=400, detail="Kuryer buyumni olgandan keyin bekor qilib bo'lmaydi. Administratsiyaga yozing.")
    courier_id = o.courier_id
    o.status = "cancelled"
    o.cancelled_at = datetime.utcnow()
    o.cancelled_by = "client"
    await db.commit()
    if courier_id:
        try:
            cu = (await db.execute(select(User).where(User.id == courier_id))).scalars().first()
            if cu and cu.telegram_id:
                await send_telegram_message(cu.telegram_id, f"❌ Mijoz pochta buyurtmasi #{o.id} ni bekor qildi.")
        except Exception as e:
            print(f"Pochta bekor xabari yuborilmadi: {e}")
    return {"ok": True}


async def _parcel_notify_client(db: AsyncSession, o: ParcelOrder, text: str) -> None:
    try:
        cl = (await db.execute(select(User).where(User.id == o.client_id))).scalars().first()
        if cl and cl.telegram_id:
            await send_telegram_message(cl.telegram_id, text)
    except Exception as e:
        print(f"Pochta xabari yuborilmadi: {e}")


async def _parcel_courier_guard(db: AsyncSession, user: User) -> CourierProfile:
    cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == user.id).with_for_update())).scalars().first()
    if not cp or cp.application_status == "pending":
        raise HTTPException(status_code=403, detail="Kuryer arizangiz hali tasdiqlanmagan")
    if cp.is_blocked:
        raise HTTPException(status_code=403, detail="Siz administrator tomonidan vaqtincha bloklangansiz")
    return cp


async def _parcel_balance_check(db: AsyncSession, cp: CourierProfile, courier_id: int, need: float, setting) -> None:
    courier_pct = (setting.courier_share_percent or 0) / 100
    reserved = await _courier_reserved_hold(db, courier_id, courier_pct)
    free = (cp.balance or 0) - reserved
    if free < need:
        raise HTTPException(status_code=403, detail=(
            f"Balansingiz yetarli emas: bu pochta uchun kamida {_fm(need)} so'm kerak, sizda {_fm(max(free, 0))} so'm bor. "
            f"Avval balansingizni to'ldiring."))


# ---- Kuryer ----
courier_parcel_router = APIRouter(prefix="/courier/parcels", tags=["Kuryer: pochta"])


def _parcel_for_courier(o: ParcelOrder, my: bool, d_courier=None) -> dict:
    d = {"id": o.id, "status": o.status, "status_label": PARCEL_STATUS_UZ.get(o.status, o.status),
         "pickup_address": o.pickup_address, "dropoff_address": o.dropoff_address, "item": o.item_description,
         "size": o.size, "size_label": PARCEL_SIZES.get(o.size, o.size), "fee": o.fee, "earning": o.courier_earning,
         "platform_share": o.platform_share, "distance_km": o.distance_km, "payer": o.payer,
         "pickup_lat": o.pickup_lat, "pickup_lng": o.pickup_lng, "dropoff_lat": o.dropoff_lat, "dropoff_lng": o.dropoff_lng,
         "comment": o.comment, "from_me_km": round(d_courier, 1) if d_courier is not None else None}
    if my:
        d["receiver_name"] = o.receiver_name
        d["receiver_phone"] = o.receiver_phone
        d["sender_name"] = o.client.full_name if o.client else None
        d["sender_phone"] = o.client.phone_number if o.client else None
    return d


@courier_parcel_router.get("/available")
async def courier_parcels_available(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    setting = await _get_or_create_setting(db)
    if not _parcel_cfg(setting)["enabled"]:
        return {"enabled": False, "items": []}
    cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == user.id))).scalars().first()
    q = select(ParcelOrder).where(ParcelOrder.status == "new").order_by(ParcelOrder.id.desc()).limit(30)
    if user.city_id is not None:
        q = q.where(ParcelOrder.city_id == user.city_id)
    rows = (await db.execute(q)).scalars().all()
    out = []
    for o in rows:
        dk = _road_km(setting, cp.latitude, cp.longitude, o.pickup_lat, o.pickup_lng) if cp else None
        out.append(_parcel_for_courier(o, False, dk))
    out.sort(key=lambda x: (x["from_me_km"] is None, x["from_me_km"] or 0))
    return {"enabled": True, "items": out}


@courier_parcel_router.get("/mine")
async def courier_parcels_mine(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    rows = (await db.execute(select(ParcelOrder).options(selectinload(ParcelOrder.client))
                             .where(ParcelOrder.courier_id == user.id, ParcelOrder.status.in_(["accepted", "picked_up"]))
                             .order_by(ParcelOrder.id.desc()))).scalars().all()
    hist = (await db.execute(select(ParcelOrder).where(ParcelOrder.courier_id == user.id, ParcelOrder.status == "delivered")
                             .order_by(ParcelOrder.delivered_at.desc()).limit(15))).scalars().all()
    return {"active": [_parcel_for_courier(o, True) for o in rows],
            "history": [{"id": o.id, "fee": o.fee, "earning": o.courier_earning, "at": o.delivered_at.isoformat() if o.delivered_at else None,
                         "route": f"{o.pickup_address} → {o.dropoff_address}"} for o in hist]}


@courier_parcel_router.post("/{pid}/accept")
async def courier_parcel_accept(pid: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    cp = await _parcel_courier_guard(db, user)
    setting = await _get_or_create_setting(db)
    if not _parcel_cfg(setting)["enabled"]:
        raise HTTPException(status_code=403, detail="Pochta xizmati hozircha ishlamayapti")
    o = (await db.execute(select(ParcelOrder).where(ParcelOrder.id == pid).with_for_update())).scalars().first()
    if not o or o.status != "new":
        raise HTTPException(status_code=404, detail="Bu pochta allaqachon boshqa kuryer tomonidan olingan yoki bekor qilingan")
    if user.city_id is not None and o.city_id is not None and user.city_id != o.city_id:
        raise HTTPException(status_code=403, detail="Bu buyurtma boshqa shaharniki")
    mine = (await db.execute(select(func.count(ParcelOrder.id)).where(
        ParcelOrder.courier_id == user.id, ParcelOrder.status.in_(["accepted", "picked_up"])))).scalar() or 0
    if mine >= 3:
        raise HTTPException(status_code=400, detail="Bir vaqtda 3 tadan ortiq pochta ola olmaysiz")
    await _parcel_balance_check(db, cp, user.id, float(o.platform_share or 0), setting)
    o.status = "accepted"
    o.courier_id = user.id
    o.accepted_at = datetime.utcnow()
    await db.commit()
    await _parcel_notify_client(db, o, f"📮 Pochta #{o.id}: kuryer {user.full_name} ({user.phone_number}) buyurtmani qabul qildi.\n"
                                       f"Qabul qiluvchi kuryerga aytadigan kod: <b>{o.delivery_code}</b>")
    return {"ok": True}


async def _my_parcel(db: AsyncSession, pid: int, user: User, statuses: list) -> ParcelOrder:
    o = (await db.execute(select(ParcelOrder).where(ParcelOrder.id == pid, ParcelOrder.courier_id == user.id).with_for_update())).scalars().first()
    if not o or o.status not in statuses:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi yoki holati mos emas")
    return o


@courier_parcel_router.post("/{pid}/picked")
async def courier_parcel_picked(pid: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    o = await _my_parcel(db, pid, user, ["accepted"])
    o.status = "picked_up"
    o.picked_at = datetime.utcnow()
    await db.commit()
    await _parcel_notify_client(db, o, f"📮 Pochta #{o.id}: kuryer buyumni oldi va yo'lga chiqdi.")
    return {"ok": True}


@courier_parcel_router.post("/{pid}/release")
async def courier_parcel_release(pid: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    """Kuryer buyumni olishdan oldin buyurtmadan voz kechadi — u yana umumiy ro'yxatga qaytadi."""
    o = await _my_parcel(db, pid, user, ["accepted"])
    o.status = "new"
    o.courier_id = None
    o.accepted_at = None
    await db.commit()
    await _parcel_notify_client(db, o, f"📮 Pochta #{o.id}: kuryer voz kechdi, boshqa kuryer izlanmoqda.")
    return {"ok": True}


class ParcelCodeBody(BaseModel):
    code: str


@courier_parcel_router.post("/{pid}/delivered")
async def courier_parcel_delivered(pid: int, body: ParcelCodeBody, db: AsyncSession = Depends(get_db),
                                   user: User = Depends(get_current_courier_user)):
    o = await _my_parcel(db, pid, user, ["picked_up"])
    if not secrets.compare_digest((body.code or "").strip().encode(), (o.delivery_code or "x").encode()):
        raise HTTPException(status_code=400, detail="Kod noto'g'ri. Qabul qiluvchidan 4 xonali kodni so'rang.")
    cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == user.id).with_for_update())).scalars().first()
    if not o.financials_applied:
        share = float(o.platform_share or 0)
        if cp is not None and share > 0:
            cp.balance = (cp.balance or 0) - share
            db.add(Transaction(user_id=user.id, type=TransactionType.WITHDRAWAL, amount=share,
                               note=f"Pochta #{o.id} — platforma ulushi balansingizdan yechildi (naqd {_fm(o.fee)} so'm sizda)",
                               created_by_id=None))
        o.financials_applied = True
    o.status = "delivered"
    o.delivered_at = datetime.utcnow()
    await db.commit()
    await _parcel_notify_client(db, o, f"✅ Pochta #{o.id} qabul qiluvchiga topshirildi. Rahmat!")
    return {"ok": True, "earning": o.courier_earning}


# ---- Owner / operator paneli ----
admin_parcel_router = APIRouter(prefix="/admin/pochta", tags=["Pochta boshqaruvi"])


def _parcel_admin_dict(o: ParcelOrder) -> dict:
    return {"id": o.id, "status": o.status, "status_label": PARCEL_STATUS_UZ.get(o.status, o.status),
            "client": o.client.full_name if o.client else "", "client_phone": o.client.phone_number if o.client else "",
            "receiver_name": o.receiver_name, "receiver_phone": o.receiver_phone, "pickup_address": o.pickup_address,
            "dropoff_address": o.dropoff_address, "item": o.item_description, "size": o.size, "fee": o.fee,
            "earning": o.courier_earning, "payer": o.payer, "km": o.distance_km, "comment": o.comment,
            "courier": o.courier.full_name if o.courier else None, "courier_phone": o.courier.phone_number if o.courier else None,
            "courier_id": o.courier_id, "city_id": o.city_id, "cancel_reason": o.cancel_reason, "cancelled_by": o.cancelled_by,
            "at": o.created_at.isoformat() if o.created_at else None}


def _parcel_scope(current_user: User):
    return current_user.city_id if is_scoped_to_city(current_user) else None


@admin_parcel_router.get("/list")
async def admin_parcel_list(status_filter: Optional[str] = None, db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(require_section("pochta"))):
    scope = _parcel_scope(current_user)
    q = select(ParcelOrder).options(selectinload(ParcelOrder.client), selectinload(ParcelOrder.courier)).order_by(ParcelOrder.id.desc()).limit(150)
    if scope is not None:
        q = q.where(ParcelOrder.city_id == scope)
    if status_filter in PARCEL_STATUS_UZ:
        q = q.where(ParcelOrder.status == status_filter)
    rows = (await db.execute(q)).scalars().all()
    cq = select(ParcelOrder.status, func.count(ParcelOrder.id)).group_by(ParcelOrder.status)
    since = datetime.utcnow() - timedelta(hours=24)
    rq = select(func.coalesce(func.sum(ParcelOrder.fee), 0.0)).where(ParcelOrder.status == "delivered", ParcelOrder.delivered_at >= since)
    if scope is not None:
        cq = cq.where(ParcelOrder.city_id == scope)
        rq = rq.where(ParcelOrder.city_id == scope)
    counts = {k: v for k, v in (await db.execute(cq)).all()}
    setting = await _get_or_create_setting(db)
    return {"items": [_parcel_admin_dict(o) for o in rows], "counts": counts, "revenue_24h": float((await db.execute(rq)).scalar() or 0),
            "enabled": _parcel_cfg(setting)["enabled"], "can_edit_settings": current_user.role == UserRole.OWNER}


@admin_parcel_router.get("/couriers")
async def admin_parcel_couriers(db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("pochta"))):
    scope = _parcel_scope(current_user)
    q = (select(User, CourierProfile).join(CourierProfile, CourierProfile.user_id == User.id)
         .where(CourierProfile.is_blocked == False, CourierProfile.application_status != "pending").order_by(CourierProfile.is_online.desc(), User.full_name).limit(100))
    if scope is not None:
        q = q.where(User.city_id == scope)
    return {"items": [{"id": u.id, "name": u.full_name, "phone": u.phone_number, "online": bool(cp.is_online), "balance": cp.balance or 0}
                      for u, cp in (await db.execute(q)).all()]}


async def _admin_parcel_get(db: AsyncSession, pid: int, current_user: User, lock: bool = True) -> ParcelOrder:
    q = select(ParcelOrder).where(ParcelOrder.id == pid)
    if lock:
        q = q.with_for_update()
    o = (await db.execute(q)).scalars().first()
    scope = _parcel_scope(current_user)
    if not o or (scope is not None and o.city_id != scope):
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    return o


class ParcelAssignBody(BaseModel):
    courier_id: int


@admin_parcel_router.post("/{pid}/assign")
async def admin_parcel_assign(pid: int, body: ParcelAssignBody, db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(require_section("pochta"))):
    o = await _admin_parcel_get(db, pid, current_user)
    if o.status not in ("new", "accepted"):
        raise HTTPException(status_code=400, detail="Bu buyurtmaga kuryer biriktirib bo'lmaydi")
    cu = (await db.execute(select(User).where(User.id == body.courier_id))).scalars().first()
    if not cu or (o.city_id is not None and cu.city_id != o.city_id):
        raise HTTPException(status_code=404, detail="Kuryer topilmadi (boshqa shahardan bo'lishi mumkin)")
    cp = await _parcel_courier_guard(db, cu)
    setting = await _get_or_create_setting(db)
    old = o.courier_id
    if old != cu.id:
        await _parcel_balance_check(db, cp, cu.id, float(o.platform_share or 0), setting)
    o.courier_id = cu.id
    o.status = "accepted"
    o.accepted_at = datetime.utcnow()
    await db.commit()
    try:
        if cu.telegram_id:
            await send_telegram_message(cu.telegram_id, f"📮 Sizga pochta #{o.id} biriktirildi: {o.pickup_address} → {o.dropoff_address}")
        if old and old != cu.id:
            ou = (await db.execute(select(User).where(User.id == old))).scalars().first()
            if ou and ou.telegram_id:
                await send_telegram_message(ou.telegram_id, f"ℹ️ Pochta #{o.id} sizdan boshqa kuryerga o'tkazildi.")
    except Exception as e:
        print(f"Pochta biriktirish xabari yuborilmadi: {e}")
    await _parcel_notify_client(db, o, f"📮 Pochta #{o.id}: kuryer {cu.full_name} ({cu.phone_number}) biriktirildi.\nKod: <b>{o.delivery_code}</b>")
    return {"ok": True}


class ParcelCancelBody(BaseModel):
    reason: Optional[str] = ""


@admin_parcel_router.post("/{pid}/cancel")
async def admin_parcel_cancel(pid: int, body: ParcelCancelBody, db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(require_section("pochta"))):
    o = await _admin_parcel_get(db, pid, current_user)
    if o.status in ("delivered", "cancelled"):
        raise HTTPException(status_code=400, detail="Bu buyurtma allaqachon yakunlangan")
    courier_id = o.courier_id
    o.status = "cancelled"
    o.cancelled_at = datetime.utcnow()
    o.cancelled_by = "admin"
    o.cancel_reason = (body.reason or "").strip()[:200] or None
    await db.commit()
    reason = f"\nSabab: {o.cancel_reason}" if o.cancel_reason else ""
    await _parcel_notify_client(db, o, f"❌ Pochta #{o.id} bekor qilindi.{reason}")
    if courier_id:
        try:
            cu = (await db.execute(select(User).where(User.id == courier_id))).scalars().first()
            if cu and cu.telegram_id:
                await send_telegram_message(cu.telegram_id, f"❌ Pochta #{o.id} administrator tomonidan bekor qilindi.{reason}")
        except Exception as e:
            print(f"Pochta xabari yuborilmadi: {e}")
    return {"ok": True}


@admin_parcel_router.get("/settings")
async def admin_parcel_settings_get(db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("pochta"))):
    return _parcel_cfg(await _get_or_create_setting(db))


@admin_parcel_router.post("/settings")
async def admin_parcel_settings_set(
    parcel_enabled: Optional[str] = Form(None),
    base_fee: float = Form(..., allow_inf_nan=False), per_km: float = Form(..., allow_inf_nan=False),
    medium_fee: float = Form(0.0, allow_inf_nan=False), large_fee: float = Form(0.0, allow_inf_nan=False),
    courier_percent: float = Form(80.0, allow_inf_nan=False), max_km: float = Form(30.0, allow_inf_nan=False),
    db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner),
):
    if not (0 <= courier_percent <= 100) or min(base_fee, per_km, medium_fee, large_fee) < 0 or not (1 <= max_km <= 500) \
            or max(base_fee, per_km, medium_fee, large_fee) > 10_000_000:
        raise HTTPException(status_code=400, detail="Qiymatlar noto'g'ri")
    st = await _get_or_create_setting(db)
    st.parcel_enabled = bool(parcel_enabled)
    st.parcel_base_fee, st.parcel_per_km = base_fee, per_km
    st.parcel_medium_fee, st.parcel_large_fee = medium_fee, large_fee
    st.parcel_courier_percent, st.parcel_max_km = courier_percent, max_km
    await db.commit()
    return {"ok": True}


# ==================== TUMANLARARO POSILKA (terminalli zanjir) ====================
# Zanjir: eshik -> Fider-1 -> A-terminal (operator, QR) -> magistral haydovchi -> B-terminal (operator, QR) -> Fider-2 -> eshik (PIN)
from sms import send_sms, sms_configured

IC_STATUS_UZ = {
    "new": "Fider-1 kuryer izlanmoqda", "f1_accepted": "Kuryer buyumni olgani ketmoqda", "f1_picked": "Kuryer A-terminalga olib bormoqda",
    "at_a": "A-terminalda (magistralga biriktirilmoqda)", "in_transit": "Shaharlararo yo'lda", "at_b": "Manzil shahar terminalida",
    "out_for_delivery": "Kuryer eshikka olib bormoqda", "failed": "Topshirib bo'lmadi, terminalga qaytmoqda", "delivered": "Topshirildi",
    "returning": "Jo'natuvchiga qaytarilmoqda", "returned": "Qaytarildi (A-terminalda)", "returned_done": "Jo'natuvchiga topshirildi",
    "cancelled": "Bekor qilindi"}
IC_ACTIVE = ("new", "f1_accepted", "f1_picked", "at_a", "in_transit", "at_b", "out_for_delivery", "failed", "returning", "returned")
IC_STAGE = ["new", "f1_accepted", "f1_picked", "at_a", "in_transit", "at_b", "out_for_delivery", "delivered"]
_QR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _ic_cfg(setting) -> dict:
    def g(n, d):
        v = getattr(setting, n, None)
        return d if v is None else v
    return {"enabled": bool(g("express_enabled", False)), "leg_base": float(g("express_courier_base", 6000.0)),
            "leg_per_km": float(g("express_courier_per_km", 2000.0)), "max_leg_km": float(g("express_max_leg_km", 20.0)),
            "platform_percent": float(g("ic_platform_percent", 10.0)), "free_days": int(g("ic_free_days", 3)),
            "storage_fee_day": float(g("ic_storage_fee_day", 2000.0)), "max_days": int(g("ic_max_days", 8)),
            "max_value": float(g("ic_max_value", 1000000.0))}


def _ic_round(v: float) -> float:
    return float(math.ceil(v / 500.0) * 500)


def _ic_pin() -> str:
    return f"{secrets.randbelow(9000) + 1000}"


async def _ic_unique_qr(db: AsyncSession) -> str:
    for _ in range(20):
        code = "ZM" + "".join(secrets.choice(_QR_ALPHABET) for _ in range(6))
        if not (await db.execute(select(IntercityOrder.id).where(IntercityOrder.qr_code == code))).first():
            return code
    raise HTTPException(status_code=500, detail="Kod yaratib bo'lmadi, qayta urinib ko'ring")


def _ic_storage(o: IntercityOrder, cfg: dict, now=None):
    """Terminalda saqlash: (kun, to'lov). 3 kun bepul, 4-kundan har kuniga."""
    if o.status not in ("at_b", "out_for_delivery", "failed") or not o.at_b_at:
        return 0, 0.0
    el = ((now or datetime.utcnow()) - o.at_b_at).total_seconds() / 86400.0
    d = int(el)
    extra = d - cfg["free_days"] + 1 if d >= cfg["free_days"] else 0
    return d, float(max(extra, 0)) * cfg["storage_fee_day"]


async def _ic_event(db: AsyncSession, o: IntercityOrder, status: str, actor: Optional[User], role: str,
                    terminal_id=None, note: Optional[str] = None, lat=None, lng=None) -> None:
    db.add(IntercityEvent(order_id=o.id, status=status, actor_id=actor.id if actor else None, actor_role=role,
                          terminal_id=terminal_id, note=(note or None) and note[:200], lat=lat, lng=lng))


async def _ic_notify(db: AsyncSession, user_id, text: str) -> None:
    try:
        if not user_id:
            return
        u = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
        if u and u.telegram_id:
            await send_telegram_message(u.telegram_id, text)
    except Exception as e:
        print(f"Tumanlararo xabar yuborilmadi: {e}")


def _ic_track_url(o: IntercityOrder) -> str:
    return f"{get_public_base_url(None)}/track/{o.track_token}"


async def _ic_credit(db: AsyncSession, courier_id: int, amount: float, note: str, tx_type=None) -> None:
    cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == courier_id).with_for_update())).scalars().first()
    if cp is None or amount <= 0:
        return
    cp.balance = (cp.balance or 0) + amount
    db.add(Transaction(user_id=courier_id, type=tx_type or TransactionType.DEPOSIT, amount=amount, note=note))


async def _ic_debit(db: AsyncSession, courier_id: int, amount: float, note: str) -> None:
    cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == courier_id).with_for_update())).scalars().first()
    if cp is None or amount <= 0:
        return
    cp.balance = (cp.balance or 0) - amount
    db.add(Transaction(user_id=courier_id, type=TransactionType.WITHDRAWAL, amount=amount, note=note))


async def _ic_pay_driver(db: AsyncSession, driver_user_id: int, amount: float) -> None:
    dr = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == driver_user_id).with_for_update())).scalars().first()
    if dr and amount > 0:
        dr.balance = (dr.balance or 0) + amount


async def _ic_terminal_cash(db: AsyncSession, terminal_id: int, amount: float, user_id, kind: str, order_id=None, note=None) -> None:
    t = (await db.execute(select(Terminal).where(Terminal.id == terminal_id).with_for_update())).scalars().first()
    if t is None:
        return
    t.cash_balance = (t.cash_balance or 0) + amount
    db.add(TerminalCashLog(terminal_id=terminal_id, user_id=user_id, amount=amount, kind=kind, order_id=order_id, note=note))


async def _ic_active_terminal(db: AsyncSession, city_id: int) -> Terminal:
    t = (await db.execute(select(Terminal).where(Terminal.city_id == city_id, Terminal.is_active == True))).scalars().first()
    if not t:
        raise HTTPException(status_code=400, detail="Bu shaharda terminal hali ochilmagan")
    return t


async def _ic_fare(db: AsyncSession, from_city_id: int, to_city_id: int, size: str) -> float:
    f = (await db.execute(select(ExpressFare).where(ExpressFare.from_city_id == from_city_id, ExpressFare.to_city_id == to_city_id,
                                                    ExpressFare.is_active == True))).scalars().first()
    if not f:
        raise HTTPException(status_code=400, detail="Bu yo'nalishda tumanlararo posilka hozircha yo'q")
    return float({"small": f.price_small, "medium": f.price_medium, "large": f.price_large}.get(size) or f.price_small or 0)


async def _ic_quote(db: AsyncSession, setting, cfg, from_city_id: int, to_city_id: int, size: str, pl, pg, dl, dg) -> dict:
    if from_city_id == to_city_id:
        raise HTTPException(status_code=400, detail="Boshqa shaharni tanlang")
    ta, tb = await _ic_active_terminal(db, from_city_id), await _ic_active_terminal(db, to_city_id)
    k1 = _road_km(setting, pl, pg, ta.latitude, ta.longitude)
    k3 = _road_km(setting, tb.latitude, tb.longitude, dl, dg)
    k1 = k1 if k1 is not None else 3.0
    k3 = (k3 if k3 is not None else 3.0)
    for k in (k1, k3):
        if k > cfg["max_leg_km"]:
            raise HTTPException(status_code=400, detail=f"Manzil terminaldan juda uzoq ({k:.0f} km). Eng uzog'i {cfg['max_leg_km']:.0f} km.")
    fare = await _ic_fare(db, from_city_id, to_city_id, size)
    l1, l3 = _ic_round(cfg["leg_base"] + cfg["leg_per_km"] * k1), _ic_round(cfg["leg_base"] + cfg["leg_per_km"] * k3)
    sub = l1 + l3 + fare
    pct = min(max(cfg["platform_percent"], 0.0), 60.0) / 100.0
    total = _ic_round(sub / (1 - pct)) if pct > 0 else sub
    return {"leg1_fee": l1, "leg3_fee": l3, "fare": fare, "platform_fee": total - sub, "total": total, "km1": round(k1, 1), "km3": round(k3, 1),
            "ta": ta, "tb": tb}


_IC_LOAD = (selectinload(IntercityOrder.client), selectinload(IntercityOrder.f1), selectinload(IntercityOrder.f2),
            selectinload(IntercityOrder.trip).selectinload(LinehaulTrip.driver),
            selectinload(IntercityOrder.from_terminal), selectinload(IntercityOrder.to_terminal))


def _ic_public(o: IntercityOrder, cfg: dict, with_pin: bool) -> dict:
    d, fee = _ic_storage(o, cfg)
    r = {"id": o.id, "status": o.status, "status_label": IC_STATUS_UZ.get(o.status, o.status), "item": o.item_description, "size": o.size,
         "pickup_address": o.pickup_address, "dropoff_address": o.dropoff_address, "receiver_name": o.receiver_name,
         "receiver_phone": o.receiver_phone, "total": o.total, "payer": o.payer, "qr": o.qr_code,
         "from_terminal": o.from_terminal.name if o.from_terminal else None, "to_terminal": o.to_terminal.name if o.to_terminal else None,
         "stage": IC_STAGE.index(o.status) if o.status in IC_STAGE else -1, "stages": len(IC_STAGE),
         "f1_name": o.f1.full_name if o.f1 else None, "f1_phone": o.f1.phone_number if o.f1 else None,
         "f2_name": o.f2.full_name if o.f2 else None, "f2_phone": o.f2.phone_number if o.f2 else None,
         "driver_name": None, "driver_phone": None, "storage_days": d, "storage_fee": fee, "track_url": _ic_track_url(o),
         "created_at": o.created_at.isoformat() if o.created_at else None}
    if o.trip is not None and o.status in ("in_transit", "at_b", "out_for_delivery", "failed", "delivered", "returning"):
        r["driver_name"] = o.trip.driver.full_name if o.trip.driver else None
        r["driver_phone"] = o.trip.driver.phone_number if o.trip.driver else None
    if with_pin and o.status in IC_ACTIVE and not o.pin_sms_sent:
        r["pin"] = o.delivery_pin
    elif o.pin_sms_sent and o.status in IC_ACTIVE:
        r["pin_in_sms"] = True
    return r


# ---------------- Mijoz ----------------
express_router = APIRouter(tags=["Tumanlararo posilka: mijoz"])


class IcConfigBody(BaseModel):
    init_data: str


class IcQuoteBody(BaseModel):
    init_data: str
    to_city_id: int
    pickup_lat: float
    pickup_lng: float
    dropoff_lat: float
    dropoff_lng: float
    size: str = "small"


class IcCreateBody(IcQuoteBody):
    pickup_address: str
    dropoff_address: str
    receiver_name: str
    receiver_phone: str
    item_description: str
    declared_value: float = 0.0
    rules_ok: bool = False
    comment: Optional[str] = None
    payer: str = "sender"


class IcIdBody(BaseModel):
    init_data: str
    id: int


@express_router.post("/api/shop/express/config")
async def express_config(body: IcConfigBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    setting = await _get_or_create_setting(db)
    cfg = _ic_cfg(setting)
    dests = []
    if cfg["enabled"] and client.city_id is not None and (await db.execute(select(Terminal.id).where(Terminal.city_id == client.city_id, Terminal.is_active == True))).first():
        rows = (await db.execute(select(City.id, City.name, Terminal.latitude, Terminal.longitude).join(ExpressFare, ExpressFare.to_city_id == City.id)
                                 .join(Terminal, Terminal.city_id == City.id).where(ExpressFare.from_city_id == client.city_id, ExpressFare.is_active == True,
                                                                                    City.is_active == True, Terminal.is_active == True).order_by(City.name))).all()
        dests = [{"id": r[0], "name": r[1], "lat": r[2], "lng": r[3]} for r in rows]
    return {"enabled": cfg["enabled"] and bool(dests), "destinations": dests, "max_value": cfg["max_value"], "free_days": cfg["free_days"],
            "storage_fee_day": cfg["storage_fee_day"], "max_days": cfg["max_days"]}


@express_router.post("/api/shop/express/quote")
async def express_quote(body: IcQuoteBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    setting = await _get_or_create_setting(db)
    cfg = _ic_cfg(setting)
    if not cfg["enabled"]:
        raise HTTPException(status_code=403, detail="Tumanlararo posilka hozircha ishlamayapti")
    if client.city_id is None:
        raise HTTPException(status_code=400, detail="Avval shaharingizni tanlang")
    if body.size not in PARCEL_SIZES:
        raise HTTPException(status_code=400, detail="Hajmni tanlang")
    q = await _ic_quote(db, setting, cfg, client.city_id, body.to_city_id, body.size, body.pickup_lat, body.pickup_lng, body.dropoff_lat, body.dropoff_lng)
    return {"total": q["total"], "leg1_fee": q["leg1_fee"], "leg3_fee": q["leg3_fee"], "fare": q["fare"], "platform_fee": q["platform_fee"],
            "from_terminal": q["ta"].name, "to_terminal": q["tb"].name, "from_address": q["ta"].address, "to_address": q["tb"].address}


@express_router.post("/api/shop/express/create")
async def express_create(body: IcCreateBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    setting = await _get_or_create_setting(db)
    cfg = _ic_cfg(setting)
    if not cfg["enabled"]:
        raise HTTPException(status_code=403, detail="Tumanlararo posilka hozircha ishlamayapti")
    if client.city_id is None:
        raise HTTPException(status_code=400, detail="Avval shaharingizni tanlang")
    if body.size not in PARCEL_SIZES or body.payer not in ("sender", "receiver"):
        raise HTTPException(status_code=400, detail="Ma'lumot noto'g'ri")
    if not body.rules_ok:
        raise HTTPException(status_code=400, detail="Ta'qiqlangan buyumlar yo'qligini va qiymat to'g'riligini tasdiqlang")
    if body.declared_value < 0 or body.declared_value > cfg["max_value"]:
        raise HTTPException(status_code=400, detail=f"Buyum qiymati {_fm(cfg['max_value'])} so'mdan oshmasligi kerak")
    pa, da = (body.pickup_address or "").strip()[:300], (body.dropoff_address or "").strip()[:300]
    rn, item = (body.receiver_name or "").strip()[:100], (body.item_description or "").strip()[:300]
    if len(pa) < 3 or len(da) < 3 or len(rn) < 2 or len(item) < 2:
        raise HTTPException(status_code=400, detail="Barcha maydonlarni to'ldiring")
    rphone = _parcel_phone_ok(body.receiver_phone)
    active = (await db.execute(select(func.count(IntercityOrder.id)).where(IntercityOrder.client_id == client.id, IntercityOrder.status.in_(IC_ACTIVE)))).scalar() or 0
    if active >= 5:
        raise HTTPException(status_code=400, detail="Bir vaqtda 5 tadan ortiq faol tumanlararo posilka yubora olmaysiz")
    q = await _ic_quote(db, setting, cfg, client.city_id, body.to_city_id, body.size, body.pickup_lat, body.pickup_lng, body.dropoff_lat, body.dropoff_lng)
    o = IntercityOrder(client_id=client.id, from_city_id=client.city_id, to_city_id=body.to_city_id, from_terminal_id=q["ta"].id, to_terminal_id=q["tb"].id,
                       status="new", pickup_address=pa, pickup_lat=body.pickup_lat, pickup_lng=body.pickup_lng, dropoff_address=da,
                       dropoff_lat=body.dropoff_lat, dropoff_lng=body.dropoff_lng, receiver_name=rn, receiver_phone=rphone, item_description=item,
                       size=body.size, declared_value=body.declared_value, comment=(body.comment or "").strip()[:500] or None, payer=body.payer,
                       leg1_fee=q["leg1_fee"], leg3_fee=q["leg3_fee"], fare=q["fare"], platform_fee=q["platform_fee"], total=q["total"],
                       qr_code=await _ic_unique_qr(db), track_token=secrets.token_urlsafe(16), delivery_pin=_ic_pin())
    db.add(o)
    await db.flush()
    await _ic_event(db, o, "new", client, "client", note="Buyurtma berildi")
    await db.commit()
    link = _ic_track_url(o)
    sent = await send_sms(rphone, f"ZooMo: {client.full_name} sizga posilka yubordi. Kuzatish: {link}  Qabul PIN: {o.delivery_pin} (faqat qabul paytida kuryerga ayting)")
    if sent:
        o.pin_sms_sent = True
        await db.commit()
    try:
        rows = (await db.execute(select(User).join(CourierProfile, CourierProfile.user_id == User.id).where(
            User.city_id == client.city_id, CourierProfile.is_online == True, CourierProfile.is_blocked == False, User.telegram_id.isnot(None)).limit(40))).scalars().all()
        for cu in rows:
            await send_telegram_message(cu.telegram_id, f"📦 Tumanlararo posilka #{o.id}: {pa} → {q['tb'].name}\nSizga {_fm(q['leg1_fee'])} so'm. Kuryer kabinetini oching.")
    except Exception as e:
        print(f"Tumanlararo xabar yuborilmadi: {e}")
    return {"ok": True, "id": o.id, "total": o.total, "qr": o.qr_code, "track_url": link, "pin": None if sent else o.delivery_pin, "pin_sms": sent}


@express_router.post("/api/shop/express/my")
async def express_my(body: IcConfigBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    cfg = _ic_cfg(await _get_or_create_setting(db))
    rows = (await db.execute(select(IntercityOrder).options(*_IC_LOAD).where(IntercityOrder.client_id == client.id)
                             .order_by(IntercityOrder.id.desc()).limit(30))).scalars().all()
    return {"items": [_ic_public(o, cfg, True) for o in rows]}


@express_router.post("/api/shop/express/cancel")
async def express_cancel(body: IcIdBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    o = (await db.execute(select(IntercityOrder).where(IntercityOrder.id == body.id, IntercityOrder.client_id == client.id).with_for_update())).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    if o.status not in ("new", "f1_accepted"):
        raise HTTPException(status_code=400, detail="Buyum olingandan keyin bekor qilib bo'lmaydi. Administratorga murojaat qiling.")
    f1 = o.f1_id
    o.status, o.cancelled_at, o.cancelled_by = "cancelled", datetime.utcnow(), "client"
    await _ic_event(db, o, "cancelled", client, "client", note="Mijoz bekor qildi")
    await db.commit()
    await _ic_notify(db, f1, f"❌ Tumanlararo posilka #{o.id} mijoz tomonidan bekor qilindi.")
    return {"ok": True}


# ---------------- Ochiq kuzatuv (token bilan) ----------------
track_router = APIRouter(tags=["Posilka kuzatuvi"])


def _ic_holder(o: IntercityOrder, drv: Optional[ExpressDriver], f1cp, f2cp) -> dict:
    if o.status in ("new", "f1_accepted"):
        return {"label": "Buyurtma qabul qilindi", "lat": o.pickup_lat, "lng": o.pickup_lng, "live": False}
    if o.status == "f1_picked":
        return {"label": "Kuryer terminalga olib bormoqda", "lat": getattr(f1cp, "latitude", None), "lng": getattr(f1cp, "longitude", None), "live": True}
    if o.status == "at_a":
        return {"label": f"{o.from_terminal.name if o.from_terminal else 'A'}-terminalda", "lat": o.from_terminal.latitude if o.from_terminal else None,
                "lng": o.from_terminal.longitude if o.from_terminal else None, "live": False}
    if o.status in ("in_transit", "returning"):
        return {"label": "Magistral yo'lda", "lat": getattr(drv, "latitude", None), "lng": getattr(drv, "longitude", None), "live": True}
    if o.status in ("at_b", "failed"):
        return {"label": f"{o.to_terminal.name if o.to_terminal else 'B'}-terminalda", "lat": o.to_terminal.latitude if o.to_terminal else None,
                "lng": o.to_terminal.longitude if o.to_terminal else None, "live": False}
    if o.status == "out_for_delivery":
        return {"label": "Kuryer eshikka olib bormoqda", "lat": getattr(f2cp, "latitude", None), "lng": getattr(f2cp, "longitude", None), "live": True}
    return {"label": IC_STATUS_UZ.get(o.status, o.status), "lat": None, "lng": None, "live": False}


@track_router.get("/api/track/{token}")
async def api_track(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    if _rate_limited(f"tr:{ip}", limit=60, window=900):
        raise HTTPException(status_code=429, detail="Juda ko'p so'rov. Keyinroq urinib ko'ring.")
    _rate_hit(f"tr:{ip}")
    o = (await db.execute(select(IntercityOrder).options(*_IC_LOAD).where(IntercityOrder.track_token == token))).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Posilka topilmadi")
    drv = None
    if o.trip is not None:
        drv = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == o.trip.driver_id))).scalars().first()
    f1cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == o.f1_id))).scalars().first() if o.f1_id else None
    f2cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == o.f2_id))).scalars().first() if o.f2_id else None
    ev = (await db.execute(select(IntercityEvent).where(IntercityEvent.order_id == o.id).order_by(IntercityEvent.id))).scalars().all()
    cfg = _ic_cfg(await _get_or_create_setting(db))
    d, fee = _ic_storage(o, cfg)
    return {"id": o.id, "status": o.status, "status_label": IC_STATUS_UZ.get(o.status, o.status), "item": o.item_description,
            "from_city": o.from_terminal.name if o.from_terminal else "", "to_city": o.to_terminal.name if o.to_terminal else "",
            "stage": IC_STAGE.index(o.status) if o.status in IC_STAGE else -1, "stages": len(IC_STAGE),
            "holder": _ic_holder(o, drv, f1cp, f2cp),
            "terminals": [{"name": t.name, "address": t.address, "lat": t.latitude, "lng": t.longitude, "hours": t.work_hours} for t in (o.from_terminal, o.to_terminal) if t],
            "storage_days": d, "storage_fee": fee, "free_days": cfg["free_days"],
            "events": [{"status": IC_STATUS_UZ.get(e.status, e.status), "at": e.created_at.isoformat() if e.created_at else None} for e in ev]}


@track_router.get("/track/{token}", response_class=HTMLResponse)
async def track_page(token: str, request: Request):
    return templates.TemplateResponse(request=request, name="track.html", context={"token": token})


# ---------------- Kuryer (Fider-1 / Fider-2) ----------------
courier_express_router = APIRouter(prefix="/courier/express", tags=["Kuryer: tumanlararo posilka"])


def _ic_for_courier(o: IntercityOrder, role: str, cfg: dict, km=None) -> dict:
    d, fee = _ic_storage(o, cfg)
    r = {"id": o.id, "role": role, "status": o.status, "status_label": IC_STATUS_UZ.get(o.status, o.status), "item": o.item_description,
         "size": o.size, "size_label": PARCEL_SIZES.get(o.size, o.size), "payer": o.payer, "total": o.total, "qr": o.qr_code,
         "earning": o.leg1_fee if role == "f1" else o.leg3_fee, "from_me_km": round(km, 1) if km is not None else None, "comment": o.comment,
         "declared_value": o.declared_value}
    if role == "f1":
        r.update(pickup_address=o.pickup_address, pickup_lat=o.pickup_lat, pickup_lng=o.pickup_lng,
                 sender_name=o.client.full_name if o.client else None, sender_phone=o.client.phone_number if o.client else None,
                 terminal=o.from_terminal.name if o.from_terminal else None, terminal_address=o.from_terminal.address if o.from_terminal else None,
                 terminal_lat=o.from_terminal.latitude if o.from_terminal else None, terminal_lng=o.from_terminal.longitude if o.from_terminal else None)
    else:
        r.update(dropoff_address=o.dropoff_address, dropoff_lat=o.dropoff_lat, dropoff_lng=o.dropoff_lng, receiver_name=o.receiver_name, receiver_phone=o.receiver_phone,
                 terminal=o.to_terminal.name if o.to_terminal else None, terminal_address=o.to_terminal.address if o.to_terminal else None,
                 terminal_lat=o.to_terminal.latitude if o.to_terminal else None, terminal_lng=o.to_terminal.longitude if o.to_terminal else None,
                 storage_days=d, storage_fee=fee, collect=(o.total if o.payer == "receiver" else 0) + fee, reserved=(o.f2_id is not None))
    return r


async def _ic_my_order(db: AsyncSession, oid: int, user: User, role: str, statuses) -> IntercityOrder:
    col = IntercityOrder.f1_id if role == "f1" else IntercityOrder.f2_id
    o = (await db.execute(select(IntercityOrder).where(IntercityOrder.id == oid, col == user.id, IntercityOrder.status.in_(statuses)).with_for_update())).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi yoki holati mos emas")
    return o


@courier_express_router.get("/available")
async def courier_ic_available(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    setting = await _get_or_create_setting(db)
    cfg = _ic_cfg(setting)
    if not cfg["enabled"] or user.city_id is None:
        return {"enabled": False, "pickups": [], "deliveries": []}
    cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == user.id))).scalars().first()
    la, lg = (cp.latitude, cp.longitude) if cp else (None, None)
    p = (await db.execute(select(IntercityOrder).options(*_IC_LOAD).where(IntercityOrder.status == "new", IntercityOrder.from_city_id == user.city_id)
                          .order_by(IntercityOrder.id.desc()).limit(30))).scalars().all()
    dv = (await db.execute(select(IntercityOrder).options(*_IC_LOAD).where(IntercityOrder.status == "at_b", IntercityOrder.to_city_id == user.city_id,
                                                                           IntercityOrder.f2_id.is_(None)).order_by(IntercityOrder.id.desc()).limit(30))).scalars().all()
    return {"enabled": True,
            "pickups": [_ic_for_courier(o, "f1", cfg, _road_km(setting, la, lg, o.pickup_lat, o.pickup_lng)) for o in p],
            "deliveries": [_ic_for_courier(o, "f2", cfg, _road_km(setting, la, lg, o.to_terminal.latitude if o.to_terminal else None, o.to_terminal.longitude if o.to_terminal else None)) for o in dv]}


@courier_express_router.get("/mine")
async def courier_ic_mine(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    cfg = _ic_cfg(await _get_or_create_setting(db))
    a1 = (await db.execute(select(IntercityOrder).options(*_IC_LOAD).where(IntercityOrder.f1_id == user.id, IntercityOrder.status.in_(["f1_accepted", "f1_picked"]))
                           .order_by(IntercityOrder.id.desc()))).scalars().all()
    a2 = (await db.execute(select(IntercityOrder).options(*_IC_LOAD).where(IntercityOrder.f2_id == user.id, IntercityOrder.status.in_(["at_b", "out_for_delivery", "failed"]))
                           .order_by(IntercityOrder.id.desc()))).scalars().all()
    h = (await db.execute(select(IntercityOrder).where(or_(IntercityOrder.f1_id == user.id, IntercityOrder.f2_id == user.id), IntercityOrder.status == "delivered")
                          .order_by(IntercityOrder.delivered_at.desc()).limit(15))).scalars().all()
    held = (await db.execute(select(func.coalesce(func.sum(IntercityOrder.cash_held_amount), 0.0)).where(IntercityOrder.cash_held_by_id == user.id))).scalar() or 0
    return {"active": [_ic_for_courier(o, "f1", cfg) for o in a1] + [_ic_for_courier(o, "f2", cfg) for o in a2], "cash_held": float(held),
            "history": [{"id": o.id, "earning": o.leg1_fee if o.f1_id == user.id else o.leg3_fee, "at": o.delivered_at.isoformat() if o.delivered_at else None} for o in h]}


@courier_express_router.post("/{oid}/accept1")
async def courier_ic_accept1(oid: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    cp = await _parcel_courier_guard(db, user)
    setting = await _get_or_create_setting(db)
    if not _ic_cfg(setting)["enabled"]:
        raise HTTPException(status_code=403, detail="Tumanlararo posilka hozircha ishlamayapti")
    o = (await db.execute(select(IntercityOrder).where(IntercityOrder.id == oid).with_for_update())).scalars().first()
    if not o or o.status != "new":
        raise HTTPException(status_code=404, detail="Bu buyurtma allaqachon olingan yoki bekor qilingan")
    if user.city_id != o.from_city_id:
        raise HTTPException(status_code=403, detail="Bu buyurtma boshqa shahardan")
    n = (await db.execute(select(func.count(IntercityOrder.id)).where(IntercityOrder.f1_id == user.id, IntercityOrder.status.in_(["f1_accepted", "f1_picked"])))).scalar() or 0
    if n >= 3:
        raise HTTPException(status_code=400, detail="Bir vaqtda 3 tadan ortiq posilka ola olmaysiz")
    if o.payer == "sender":
        await _parcel_balance_check(db, cp, user.id, float(o.total or 0), setting)
    o.f1_id, o.status = user.id, "f1_accepted"
    await _ic_event(db, o, "f1_accepted", user, "fider1", note=user.full_name)
    await db.commit()
    await _parcel_notify_client(db, o, f"📦 Tumanlararo posilka #{o.id}: kuryer {user.full_name} ({user.phone_number}) buyumni olgani ketmoqda.")
    return {"ok": True}


@courier_express_router.post("/{oid}/release1")
async def courier_ic_release1(oid: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    o = await _ic_my_order(db, oid, user, "f1", ["f1_accepted"])
    o.f1_id, o.status = None, "new"
    await _ic_event(db, o, "new", user, "fider1", note="Kuryer rad etdi")
    await db.commit()
    return {"ok": True}


@courier_express_router.get("/{oid}/label", response_class=HTMLResponse)
async def courier_ic_label(oid: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    o = (await db.execute(select(IntercityOrder).options(*_IC_LOAD).where(IntercityOrder.id == oid, or_(IntercityOrder.f1_id == user.id, IntercityOrder.f2_id == user.id)))).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    return _ic_label_html(o)


def _ic_label_html(o: IntercityOrder) -> HTMLResponse:
    h = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Stiker {o.qr_code}</title>
<style>body{{font-family:system-ui,sans-serif;margin:0;padding:12px;text-align:center}}.lbl{{border:2px solid #000;border-radius:8px;width:300px;margin:0 auto;padding:10px}}
.code{{font-size:30px;font-weight:900;letter-spacing:3px}}.sm{{font-size:12px;margin:2px 0}}#qr svg,#qr img{{width:160px;height:160px}}@media print{{button{{display:none}}}}</style></head><body>
<div class="lbl"><div class="sm"><b>ZooMo Express</b> #{o.id}</div><div id="qr"></div><div class="code">{o.qr_code}</div>
<div class="sm">{(o.from_terminal.name if o.from_terminal else '')} → {(o.to_terminal.name if o.to_terminal else '')}</div>
<div class="sm">Hajm: {PARCEL_SIZES.get(o.size, o.size)}</div></div>
<p><button onclick="window.print()" style="padding:10px 18px;font-size:14px">🖨 Chop etish</button></p>
<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.js"></script>
<script>try{{var q=qrcode(0,'M');q.addData('{o.qr_code}');q.make();document.getElementById('qr').innerHTML=q.createSvgTag(5,0);}}catch(e){{}}</script></body></html>"""
    return HTMLResponse(h)


@courier_express_router.post("/{oid}/pickup1")
async def courier_ic_pickup1(oid: int, qr: str = Form(...), photo: UploadFile = File(...), db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_courier_user)):
    await _parcel_courier_guard(db, user)
    o = await _ic_my_order(db, oid, user, "f1", ["f1_accepted"])
    if not secrets.compare_digest((qr or "").strip().upper().encode(), o.qr_code.encode()):
        raise HTTPException(status_code=400, detail="Stiker kodi noto'g'ri. Posilkaga yopishtirilgan stikerdagi kodni kiriting.")
    data, mime = await process_uploaded_image(photo)
    o.pickup_photo, o.pickup_photo_mime = data, mime
    held = 0.0
    if o.payer == "sender":
        held = float(o.total or 0)
        await _ic_debit(db, user.id, held, f"Tumanlararo #{o.id}: jo'natuvchidan olingan naqd pul (terminalga topshirilguncha band)")
        o.cash_held_by_id, o.cash_held_amount, o.cash_held_terminal_id = user.id, held, o.from_terminal_id
    o.status = "f1_picked"
    await _ic_event(db, o, "f1_picked", user, "fider1", note="Rasmga olindi, stiker skanerlandi")
    await db.commit()
    await _parcel_notify_client(db, o, f"📦 Tumanlararo posilka #{o.id}: buyum kuryerda, A-terminalga olib borilmoqda.\nKuzatish: {_ic_track_url(o)}")
    return {"ok": True, "collect": held}


@courier_express_router.post("/{oid}/accept2")
async def courier_ic_accept2(oid: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    cp = await _parcel_courier_guard(db, user)
    setting = await _get_or_create_setting(db)
    cfg = _ic_cfg(setting)
    o = (await db.execute(select(IntercityOrder).where(IntercityOrder.id == oid).with_for_update())).scalars().first()
    if not o or o.status != "at_b" or o.f2_id is not None:
        raise HTTPException(status_code=404, detail="Bu buyurtma allaqachon olingan")
    if user.city_id != o.to_city_id:
        raise HTTPException(status_code=403, detail="Bu buyurtma boshqa shaharga")
    n = (await db.execute(select(func.count(IntercityOrder.id)).where(IntercityOrder.f2_id == user.id, IntercityOrder.status.in_(["at_b", "out_for_delivery", "failed"])))).scalar() or 0
    if n >= 3:
        raise HTTPException(status_code=400, detail="Bir vaqtda 3 tadan ortiq posilka ola olmaysiz")
    if o.payer == "receiver":
        await _parcel_balance_check(db, cp, user.id, float(o.total or 0) + _ic_storage(o, cfg)[1], setting)
    o.f2_id = user.id
    await _ic_event(db, o, "at_b", user, "fider2", terminal_id=o.to_terminal_id, note="Fider-2 biriktirildi: " + user.full_name)
    await db.commit()
    return {"ok": True}


@courier_express_router.post("/{oid}/release2")
async def courier_ic_release2(oid: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    o = await _ic_my_order(db, oid, user, "f2", ["at_b"])
    o.f2_id = None
    await db.commit()
    return {"ok": True}


class IcPinBody(BaseModel):
    pin: str


class IcFailBody(BaseModel):
    reason: Optional[str] = ""


@courier_express_router.post("/{oid}/delivered")
async def courier_ic_delivered(oid: int, body: IcPinBody, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    await _parcel_courier_guard(db, user)
    cfg = _ic_cfg(await _get_or_create_setting(db))
    o = await _ic_my_order(db, oid, user, "f2", ["out_for_delivery"])
    if not secrets.compare_digest((body.pin or "").strip().encode(), (o.delivery_pin or "x").encode()):
        raise HTTPException(status_code=400, detail="PIN noto'g'ri. Qabul qiluvchidan 4 xonali PIN kodni so'rang.")
    _, fee = _ic_storage(o, cfg)
    x = (float(o.total or 0) if o.payer == "receiver" else 0.0) + fee
    if x > 0:
        await _ic_debit(db, user.id, x, f"Tumanlararo #{o.id}: qabul qiluvchidan olingan naqd pul (terminalga topshirilguncha band)")
        o.cash_held_by_id, o.cash_held_amount, o.cash_held_terminal_id = user.id, x, o.to_terminal_id
    o.storage_fee_paid = fee
    if not o.f2_paid:
        await _ic_credit(db, user.id, float(o.leg3_fee or 0), f"Tumanlararo #{o.id}: Fider-2 haqi")
        o.f2_paid = True
    o.status, o.delivered_at = "delivered", datetime.utcnow()
    await _ic_event(db, o, "delivered", user, "fider2", note="PIN bilan topshirildi")
    await db.commit()
    await _parcel_notify_client(db, o, f"✅ Tumanlararo posilka #{o.id} topshirildi.")
    return {"ok": True, "collect": x}


@courier_express_router.post("/{oid}/failed")
async def courier_ic_failed(oid: int, body: IcFailBody, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    o = await _ic_my_order(db, oid, user, "f2", ["out_for_delivery"])
    o.status, o.delivery_attempts = "failed", (o.delivery_attempts or 0) + 1
    if not o.f2_paid:
        await _ic_credit(db, user.id, float(o.leg3_fee or 0), f"Tumanlararo #{o.id}: Fider-2 haqi (topshirib bo'lmadi)")
        o.f2_paid = True
    await _ic_event(db, o, "failed", user, "fider2", note=(body.reason or "Qabul qiluvchi topilmadi"))
    await db.commit()
    await _parcel_notify_client(db, o, f"⚠️ Tumanlararo posilka #{o.id}: qabul qiluvchiga topshirib bo'lmadi, posilka terminalga qaytariladi. Terminalda 3 kun bepul saqlanadi.")
    return {"ok": True}


# ---------------- Magistral haydovchi ----------------
driver_express_router = APIRouter(tags=["Magistral haydovchi"])


async def get_current_express_driver(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    uid = request.session.get("user_id")
    if not uid:
        raise RedirectToLogin()
    user = (await db.execute(select(User).where(User.id == uid))).scalars().first()
    if not user or not user.is_active:
        raise RedirectToLogin()
    dr = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == user.id, ExpressDriver.is_active == True))).scalars().first()
    if not dr:
        raise RedirectToLogin()
    return user


@driver_express_router.get("/driver", response_class=HTMLResponse)
async def driver_page(request: Request, user: User = Depends(get_current_express_driver)):
    return templates.TemplateResponse(request=request, name="driver.html", context={"user": user})


async def _ic_trip_orders(db: AsyncSession, t: LinehaulTrip):
    return (await db.execute(select(IntercityOrder).where(or_(IntercityOrder.trip_id == t.id, IntercityOrder.return_trip_id == t.id),
                                                          IntercityOrder.status.in_(["in_transit", "returning"])).order_by(IntercityOrder.id))).scalars().all()


@driver_express_router.get("/driver/express/state")
async def driver_ic_state(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_express_driver)):
    dr = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == user.id))).scalars().first()
    trips = (await db.execute(select(LinehaulTrip).options(selectinload(LinehaulTrip.from_terminal), selectinload(LinehaulTrip.to_terminal))
                              .where(LinehaulTrip.driver_id == user.id, LinehaulTrip.status.in_(["waiting", "departed", "arrived"])).order_by(LinehaulTrip.id.desc()))).scalars().all()
    out = []
    for t in trips:
        os_ = await _ic_trip_orders(db, t)
        if t.status == "arrived" and not os_:
            continue
        out.append({"id": t.id, "status": t.status, "from": t.from_terminal.name, "from_address": t.from_terminal.address, "from_lat": t.from_terminal.latitude, "from_lng": t.from_terminal.longitude,
                    "to": t.to_terminal.name, "to_address": t.to_terminal.address, "to_lat": t.to_terminal.latitude, "to_lng": t.to_terminal.longitude,
                    "parcels": [{"id": o.id, "qr": o.qr_code, "size": PARCEL_SIZES.get(o.size, o.size), "ret": o.status == "returning"} for o in os_],
                    "earn": sum((o.fare if o.status != "returning" else 0) for o in os_)})
    terms = {t.city_id: t for t in (await db.execute(select(Terminal).options(selectinload(Terminal.city)).where(Terminal.is_active == True))).scalars().all()}
    pairs = (await db.execute(select(ExpressFare.from_city_id, ExpressFare.to_city_id).where(ExpressFare.is_active == True))).all()
    hist = (await db.execute(select(IntercityOrder).join(LinehaulTrip, LinehaulTrip.id == IntercityOrder.trip_id).where(
        LinehaulTrip.driver_id == user.id, IntercityOrder.driver_paid == True).order_by(IntercityOrder.id.desc()).limit(15))).scalars().all()
    return {"name": user.full_name, "car": f"{dr.car_model or ''} {dr.car_plate or ''}".strip(), "balance": dr.balance or 0, "trips": out,
            "routes": [{"from": a, "to": b, "label": f"{terms[a].name} → {terms[b].name}"} for a, b in pairs if a in terms and b in terms],
            "history": [{"id": o.id, "fare": o.fare, "at": o.at_b_at.isoformat() if o.at_b_at else None} for o in hist]}


class IcTripCreate(BaseModel):
    from_city_id: int
    to_city_id: int


@driver_express_router.post("/driver/express/trips")
async def driver_ic_trip_create(body: IcTripCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_express_driver)):
    st = await _get_or_create_setting(db)
    if not _ic_cfg(st)["enabled"]:
        raise HTTPException(status_code=403, detail="Tumanlararo xizmat hozircha o'chirilgan")
    if _mag_cfg(st)["enabled"]:
        raise HTTPException(status_code=400, detail="Reys ochish uchun terminal yaqinida navbatga yoziling")
    if not (await db.execute(select(ExpressFare.id).where(ExpressFare.from_city_id == body.from_city_id, ExpressFare.to_city_id == body.to_city_id, ExpressFare.is_active == True))).first():
        raise HTTPException(status_code=400, detail="Bu yo'nalishda xizmat yo'q")
    ta, tb = await _ic_active_terminal(db, body.from_city_id), await _ic_active_terminal(db, body.to_city_id)
    for t in (await db.execute(select(LinehaulTrip).where(LinehaulTrip.driver_id == user.id, LinehaulTrip.status.in_(["waiting", "departed", "arrived"])))).scalars().all():
        if t.status != "arrived" or await _ic_trip_orders(db, t):
            raise HTTPException(status_code=400, detail="Sizda tugallanmagan reys bor")
    t = LinehaulTrip(driver_id=user.id, from_terminal_id=ta.id, to_terminal_id=tb.id, status="waiting")
    db.add(t)
    await db.commit()
    return {"ok": True, "id": t.id}


async def _ic_my_trip(db: AsyncSession, tid: int, user: User) -> LinehaulTrip:
    t = (await db.execute(select(LinehaulTrip).where(LinehaulTrip.id == tid, LinehaulTrip.driver_id == user.id).with_for_update())).scalars().first()
    if not t:
        raise HTTPException(status_code=404, detail="Reys topilmadi")
    return t


async def _ic_depart(db: AsyncSession, t: LinehaulTrip, actor: User, role: str) -> int:
    if t.status != "waiting":
        raise HTTPException(status_code=400, detail="Reys holati mos emas")
    os_ = await _ic_trip_orders(db, t)
    in_queue = await _mag_prepare_depart(db, t)
    if not os_ and not in_queue:
        raise HTTPException(status_code=400, detail="Reysga hali posilka yuklanmagan")
    t.status, t.departed_at = "departed", datetime.utcnow()
    for o in os_:
        await _ic_event(db, o, o.status, actor, role, terminal_id=t.from_terminal_id, note="Reys jo'nadi")
    await db.commit()
    for o in os_:
        await _parcel_notify_client(db, o, f"🚗 Tumanlararo posilka #{o.id}: magistral reysga yo'lga chiqdi.\nKuzatish: {_ic_track_url(o)}")
    return len(os_)


@driver_express_router.post("/driver/express/trips/{tid}/depart")
async def driver_ic_depart(tid: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_express_driver)):
    t = await _ic_my_trip(db, tid, user)
    return {"ok": True, "count": await _ic_depart(db, t, user, "driver")}


@driver_express_router.post("/driver/express/trips/{tid}/arrive")
async def driver_ic_arrive(tid: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_express_driver)):
    t = await _ic_my_trip(db, tid, user)
    if t.status != "departed":
        raise HTTPException(status_code=400, detail="Reys holati mos emas")
    t.status, t.arrived_at = "arrived", datetime.utcnow()
    await _mag_finish(db, t)
    await db.commit()
    return {"ok": True}


@driver_express_router.post("/driver/express/trips/{tid}/cancel")
async def driver_ic_trip_cancel(tid: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_express_driver)):
    t = await _ic_my_trip(db, tid, user)
    if t.status != "waiting" or await _ic_trip_orders(db, t):
        raise HTTPException(status_code=400, detail="Reysni bekor qilib bo'lmaydi (posilka yuklangan)")
    t.status = "cancelled"
    await db.commit()
    return {"ok": True}


class IcLocBody(BaseModel):
    lat: float
    lng: float


@driver_express_router.post("/driver/express/location")
async def driver_ic_location(body: IcLocBody, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_express_driver)):
    if not (-90 <= body.lat <= 90 and -180 <= body.lng <= 180):
        raise HTTPException(status_code=400, detail="Koordinata noto'g'ri")
    dr = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == user.id))).scalars().first()
    dr.latitude, dr.longitude, dr.location_at = body.lat, body.lng, datetime.utcnow()
    await db.commit()
    return {"ok": True}


# ---------------- Terminal operatori ----------------
terminal_router = APIRouter(tags=["Terminal operatori"])


async def get_current_terminal_op(request: Request, db: AsyncSession = Depends(get_db)):
    uid = request.session.get("user_id")
    if not uid:
        raise RedirectToLogin()
    user = (await db.execute(select(User).where(User.id == uid))).scalars().first()
    if not user or not user.is_active:
        raise RedirectToLogin()
    op = (await db.execute(select(TerminalOperator).options(selectinload(TerminalOperator.terminal)).where(
        TerminalOperator.user_id == user.id, TerminalOperator.is_active == True))).scalars().first()
    if not op or not op.terminal or not op.terminal.is_active:
        raise RedirectToLogin()
    return user, op.terminal


@terminal_router.get("/terminal", response_class=HTMLResponse)
async def terminal_page(request: Request, ctx=Depends(get_current_terminal_op)):
    return templates.TemplateResponse(request=request, name="terminal.html", context={"user": ctx[0], "terminal": ctx[1]})


def _ic_term_row(o: IntercityOrder, cfg: dict) -> dict:
    d, fee = _ic_storage(o, cfg)
    return {"id": o.id, "qr": o.qr_code, "status": o.status, "status_label": IC_STATUS_UZ.get(o.status, o.status), "item": o.item_description,
            "size": PARCEL_SIZES.get(o.size, o.size), "payer": o.payer, "total": o.total, "receiver_name": o.receiver_name, "receiver_phone": o.receiver_phone,
            "sender": o.client.full_name if o.client else "", "sender_phone": o.client.phone_number if o.client else "",
            "f1": o.f1.full_name if o.f1 else None, "f2": o.f2.full_name if o.f2 else None, "f2_phone": o.f2.phone_number if o.f2 else None,
            "from": o.from_terminal.name if o.from_terminal else "", "to": o.to_terminal.name if o.to_terminal else "",
            "days": d, "fee": fee, "can_return": d >= cfg["max_days"], "declared_value": o.declared_value,
            "cash_held": o.cash_held_amount if o.cash_held_by_id else 0, "driver": o.trip.driver.full_name if o.trip and o.trip.driver else None}


@terminal_router.get("/terminal/api/state")
async def terminal_state(db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    user, T = ctx
    cfg = _ic_cfg(await _get_or_create_setting(db))

    async def q(*conds):
        return [_ic_term_row(o, cfg) for o in (await db.execute(select(IntercityOrder).options(*_IC_LOAD).where(*conds).order_by(IntercityOrder.id))).scalars().all()]
    trips = (await db.execute(select(LinehaulTrip).options(selectinload(LinehaulTrip.driver), selectinload(LinehaulTrip.from_terminal), selectinload(LinehaulTrip.to_terminal)).where(
        or_(LinehaulTrip.from_terminal_id == T.id, LinehaulTrip.to_terminal_id == T.id), LinehaulTrip.status.in_(["waiting", "departed", "arrived"])).order_by(LinehaulTrip.id))).scalars().all()
    tr = []
    for t in trips:
        n = len(await _ic_trip_orders(db, t))
        if t.status == "arrived" and n == 0:
            continue
        tr.append({"id": t.id, "status": t.status, "driver": t.driver.full_name, "driver_phone": t.driver.phone_number, "from": t.from_terminal.name, "to": t.to_terminal.name,
                   "outgoing": t.from_terminal_id == T.id, "parcels": n})
    holders = {}
    for o in (await db.execute(select(IntercityOrder).options(selectinload(IntercityOrder.client)).where(IntercityOrder.cash_held_terminal_id == T.id, IntercityOrder.cash_held_amount > 0,
                                                                                            IntercityOrder.cash_held_by_id.isnot(None)))).scalars().all():
        holders[o.cash_held_by_id] = holders.get(o.cash_held_by_id, 0.0) + float(o.cash_held_amount or 0)
    cash_couriers = []
    for cid, amt in holders.items():
        u = (await db.execute(select(User).where(User.id == cid))).scalars().first()
        if u:
            cash_couriers.append({"id": cid, "name": u.full_name, "phone": u.phone_number, "amount": amt})
    return {"terminal": {"id": T.id, "name": T.name, "address": T.address, "cash": T.cash_balance or 0}, "operator": user.full_name, "cfg": {"max_days": cfg["max_days"], "free_days": cfg["free_days"]},
            "incoming_fider": await q(IntercityOrder.from_terminal_id == T.id, IntercityOrder.status == "f1_picked"),
            "at_a": await q(IntercityOrder.from_terminal_id == T.id, IntercityOrder.status == "at_a"),
            "incoming_driver": await q(IntercityOrder.to_terminal_id == T.id, IntercityOrder.status == "in_transit"),
            "at_b": await q(IntercityOrder.to_terminal_id == T.id, IntercityOrder.status.in_(["at_b", "failed"])),
            "out": await q(IntercityOrder.to_terminal_id == T.id, IntercityOrder.status == "out_for_delivery"),
            "returning_in": await q(IntercityOrder.from_terminal_id == T.id, IntercityOrder.status == "returning"),
            "returned": await q(IntercityOrder.from_terminal_id == T.id, IntercityOrder.status == "returned"),
            "trips": tr, "cash_couriers": cash_couriers}


class IcQrBody(BaseModel):
    qr: str
    trip_id: Optional[int] = None
    note: Optional[str] = None
    passport_ok: bool = False
    pin: Optional[str] = None
    cash_received: bool = False


async def _ic_by_qr(db: AsyncSession, qr: str) -> IntercityOrder:
    o = (await db.execute(select(IntercityOrder).where(IntercityOrder.qr_code == (qr or "").strip().upper()).with_for_update())).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Bunday QR/kod topilmadi")
    return o


@terminal_router.post("/terminal/api/receive-from-fider")
async def terminal_recv_fider(body: IcQrBody, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    user, T = ctx
    o = await _ic_by_qr(db, body.qr)
    if o.status != "f1_picked" or o.from_terminal_id != T.id:
        raise HTTPException(status_code=400, detail=f"Bu posilka hozir shu terminalga kutilmayapti (holati: {IC_STATUS_UZ.get(o.status, o.status)})")
    if o.cash_held_by_id and o.cash_held_amount and o.cash_held_terminal_id == T.id:
        amt, cid = float(o.cash_held_amount), o.cash_held_by_id
        await _ic_credit(db, cid, amt, f"Tumanlararo #{o.id}: naqd pul terminalga topshirildi (band qilingan summa qaytdi)", TransactionType.REFUND)
        await _ic_terminal_cash(db, T.id, amt, user.id, "courier_cash", o.id, "Fider-1 dan naqd")
        o.cash_held_by_id, o.cash_held_amount = None, 0.0
    if not o.f1_paid and o.f1_id:
        await _ic_credit(db, o.f1_id, float(o.leg1_fee or 0), f"Tumanlararo #{o.id}: Fider-1 haqi")
        o.f1_paid = True
    o.status, o.at_a_at = "at_a", datetime.utcnow()
    await _ic_event(db, o, "at_a", user, "operator", T.id, "A-terminalda qabul qilindi")
    await db.commit()
    await _parcel_notify_client(db, o, f"📦 Posilka #{o.id} {T.name} terminaliga yetib keldi.\nKuzatish: {_ic_track_url(o)}")
    return {"ok": True, "label": f"#{o.id} {o.item_description}"}


@terminal_router.post("/terminal/api/load")
async def terminal_load(body: IcQrBody, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    user, T = ctx
    o = await _ic_by_qr(db, body.qr)
    if o.status != "at_a" or o.from_terminal_id != T.id:
        raise HTTPException(status_code=400, detail="Posilka bu terminalda magistralga tayyor emas")
    t = (await db.execute(select(LinehaulTrip).where(LinehaulTrip.id == (body.trip_id or 0)).with_for_update())).scalars().first()
    if not t or t.status != "waiting" or t.from_terminal_id != T.id or t.to_terminal_id != o.to_terminal_id:
        raise HTTPException(status_code=400, detail="Reys mos emas (kutayotgan, to'g'ri yo'nalishdagi reysni tanlang)")
    ent = await _mag_entry_for_trip(db, t.id)
    if ent is not None:
        stg = await _get_or_create_setting(db)
        mcfg = _mag_cfg(stg)
        drv_ = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == t.driver_id))).scalars().first()
        inf = await _mag_info(db, ent, mcfg, None, drv_)
        if inf["capacity"] - inf["seats_booked"] - inf["parcel_seats"] < _mag_pseat(mcfg, o.size) - 1e-9:
            raise HTTPException(status_code=400, detail="Bu mashinada posilka uchun joy yetarli emas")
    o.trip_id, o.status = t.id, "in_transit"
    await _ic_event(db, o, "in_transit", user, "operator", T.id, f"Magistral haydovchiga topshirildi: {t.driver_id}")
    await db.commit()
    await _ic_notify(db, t.driver_id, f"📦 Sizga posilka {o.qr_code} yuklandi ({PARCEL_SIZES.get(o.size, o.size)}).")
    return {"ok": True}


@terminal_router.post("/terminal/api/trips/{tid}/depart")
async def terminal_depart(tid: int, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    user, T = ctx
    t = (await db.execute(select(LinehaulTrip).where(LinehaulTrip.id == tid, LinehaulTrip.from_terminal_id == T.id).with_for_update())).scalars().first()
    if not t:
        raise HTTPException(status_code=404, detail="Reys topilmadi")
    return {"ok": True, "count": await _ic_depart(db, t, user, "operator")}


@terminal_router.post("/terminal/api/receive-from-driver")
async def terminal_recv_driver(body: IcQrBody, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    user, T = ctx
    cfg = _ic_cfg(await _get_or_create_setting(db))
    o = await _ic_by_qr(db, body.qr)
    if o.status != "in_transit" or o.to_terminal_id != T.id:
        raise HTTPException(status_code=400, detail=f"Bu posilka shu terminalga yo'lda emas (holati: {IC_STATUS_UZ.get(o.status, o.status)})")
    t = (await db.execute(select(LinehaulTrip).where(LinehaulTrip.id == o.trip_id).with_for_update())).scalars().first()
    if not o.driver_paid and t:
        await _ic_pay_driver(db, t.driver_id, float(o.fare or 0))
        o.driver_paid = True
    o.status, o.at_b_at = "at_b", datetime.utcnow()
    if t and t.status == "departed":
        t.status, t.arrived_at = "arrived", datetime.utcnow()
        await _mag_finish(db, t)
    await _ic_event(db, o, "at_b", user, "operator", T.id, "B-terminalda qabul qilindi")
    await db.commit()
    link = _ic_track_url(o)
    await send_sms(o.receiver_phone, f"ZooMo: sizga yuborilgan posilka {T.name} terminaliga yetib keldi. {T.address}. Kuzatish: {link}. {cfg['free_days']} kun bepul saqlanadi.")
    await _parcel_notify_client(db, o, f"📦 Posilka #{o.id} {T.name} terminaliga yetib keldi, yetkazib berish tayinlanmoqda.\nKuzatish: {link}")
    return {"ok": True, "label": f"#{o.id} {o.item_description}"}


@terminal_router.post("/terminal/api/handover-fider2")
async def terminal_handover_f2(body: IcQrBody, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    user, T = ctx
    o = await _ic_by_qr(db, body.qr)
    if o.status != "at_b" or o.to_terminal_id != T.id:
        raise HTTPException(status_code=400, detail="Posilka bu terminalda yetkazishga tayyor emas")
    if not o.f2_id:
        raise HTTPException(status_code=400, detail="Hali Fider-2 kuryer qabul qilmagan (kuryer ilovada «Qabul qilish» ni bosishi kerak)")
    o.status = "out_for_delivery"
    await _ic_event(db, o, "out_for_delivery", user, "operator", T.id, "Fider-2 ga topshirildi")
    await db.commit()
    await send_sms(o.receiver_phone, f"ZooMo: kuryer posilkangiz bilan yo'lga chiqdi. Kuzatish: {_ic_track_url(o)}. PIN kodni kuryerga ayting.")
    await _parcel_notify_client(db, o, f"🛵 Posilka #{o.id}: kuryer eshikka olib bormoqda.")
    return {"ok": True}


@terminal_router.post("/terminal/api/receive-failed")
async def terminal_recv_failed(body: IcQrBody, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    user, T = ctx
    o = await _ic_by_qr(db, body.qr)
    if o.status != "failed" or o.to_terminal_id != T.id:
        raise HTTPException(status_code=400, detail="Bu posilka qaytarilgan holatda emas")
    o.status, o.f2_id = "at_b", None
    await _ic_event(db, o, "at_b", user, "operator", T.id, "Topshirilmagan posilka terminalga qaytdi")
    await db.commit()
    return {"ok": True}


@terminal_router.post("/terminal/api/claim")
async def terminal_claim(body: IcQrBody, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    """Qabul qiluvchi o'zi terminalga kelib oladi (PIN yoki pasport tasdig'i bilan)."""
    user, T = ctx
    cfg = _ic_cfg(await _get_or_create_setting(db))
    o = await _ic_by_qr(db, body.qr)
    if o.status != "at_b" or o.to_terminal_id != T.id:
        raise HTTPException(status_code=400, detail="Posilka bu terminalda topshirishga tayyor emas")
    pin_ok = bool(body.pin) and secrets.compare_digest(body.pin.strip().encode(), (o.delivery_pin or "x").encode())
    if not (pin_ok or body.passport_ok):
        raise HTTPException(status_code=400, detail="PIN kiriting yoki pasport tekshirilganini tasdiqlang")
    _, fee = _ic_storage(o, cfg)
    x = (float(o.total or 0) if o.payer == "receiver" else 0.0) + fee
    if x > 0:
        if not body.cash_received:
            raise HTTPException(status_code=400, detail=f"Qabul qiluvchidan {_fm(x)} so'm naqd olinishi kerak — olgach «naqd oldim» ni belgilang")
        await _ic_terminal_cash(db, T.id, x, user.id, "claim", o.id, "Terminalda olish: naqd")
    # Fider-2 ishlatilmadi — uning haqi jo'natuvchiga keshbek sifatida qaytariladi (to'langan bo'lsa)
    if not o.f2_paid and o.payer == "sender":
        cl = (await db.execute(select(User).where(User.id == o.client_id))).scalars().first()
        if cl:
            cl.cashback_balance = (cl.cashback_balance or 0) + float(o.leg3_fee or 0)
    o.f2_id, o.storage_fee_paid = None, fee
    o.status, o.delivered_at, o.claimed_at_terminal = "delivered", datetime.utcnow(), True
    await _ic_event(db, o, "delivered", user, "operator", T.id, ("PIN" if pin_ok else "Pasport tekshirildi") + " bilan terminalda topshirildi. " + (body.note or ""))
    await db.commit()
    await _parcel_notify_client(db, o, f"✅ Posilka #{o.id} qabul qiluvchiga terminalda topshirildi.")
    return {"ok": True}


@terminal_router.post("/terminal/api/return")
async def terminal_return(body: IcQrBody, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    """Saqlash muddati tugagan posilkani jo'natuvchiga qaytarish uchun teskari reysga yuklash."""
    user, T = ctx
    cfg = _ic_cfg(await _get_or_create_setting(db))
    o = await _ic_by_qr(db, body.qr)
    if o.status != "at_b" or o.to_terminal_id != T.id:
        raise HTTPException(status_code=400, detail="Bu posilkani qaytarib bo'lmaydi")
    if _ic_storage(o, cfg)[0] < cfg["max_days"]:
        raise HTTPException(status_code=400, detail=f"Posilka {cfg['max_days']} kundan keyingina qaytariladi")
    t = (await db.execute(select(LinehaulTrip).where(LinehaulTrip.id == (body.trip_id or 0)).with_for_update())).scalars().first()
    if not t or t.status != "waiting" or t.from_terminal_id != T.id or t.to_terminal_id != o.from_terminal_id:
        raise HTTPException(status_code=400, detail="Teskari yo'nalishdagi kutayotgan reysni tanlang")
    o.return_trip_id, o.status, o.f2_id = t.id, "returning", None
    await _ic_event(db, o, "returning", user, "operator", T.id, "Saqlash muddati tugadi, jo'natuvchiga qaytarilmoqda")
    await db.commit()
    await _parcel_notify_client(db, o, f"↩️ Posilka #{o.id} qabul qilinmadi va jo'natuvchiga qaytarilmoqda. Qaytarish narxini siz to'laysiz.")
    return {"ok": True}


@terminal_router.post("/terminal/api/receive-return")
async def terminal_recv_return(body: IcQrBody, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    user, T = ctx
    o = await _ic_by_qr(db, body.qr)
    if o.status != "returning" or o.from_terminal_id != T.id:
        raise HTTPException(status_code=400, detail="Bu posilka shu terminalga qaytmayapti")
    o.status = "returned"
    t = (await db.execute(select(LinehaulTrip).where(LinehaulTrip.id == o.return_trip_id).with_for_update())).scalars().first()
    if t and t.status == "departed":
        t.status, t.arrived_at = "arrived", datetime.utcnow()
    await _ic_event(db, o, "returned", user, "operator", T.id, "Qaytgan posilka A-terminalda")
    await db.commit()
    await _parcel_notify_client(db, o, f"↩️ Posilka #{o.id} {T.name} terminaliga qaytdi. Terminaldan olib keting (qaytarish haqini to'laysiz).")
    return {"ok": True}


@terminal_router.post("/terminal/api/claim-return")
async def terminal_claim_return(body: IcQrBody, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    user, T = ctx
    o = await _ic_by_qr(db, body.qr)
    if o.status != "returned" or o.from_terminal_id != T.id:
        raise HTTPException(status_code=400, detail="Posilka qaytarib topshirishga tayyor emas")
    pin_ok = bool(body.pin) and secrets.compare_digest(body.pin.strip().encode(), (o.delivery_pin or "x").encode())
    if not (body.passport_ok or pin_ok):
        raise HTTPException(status_code=400, detail="Jo'natuvchi shaxsini pasport bilan tasdiqlang")
    rf = (await db.execute(select(ExpressFare).where(ExpressFare.from_city_id == o.to_city_id, ExpressFare.to_city_id == o.from_city_id))).scalars().first()
    fee = float(({"small": rf.price_small, "medium": rf.price_medium, "large": rf.price_large}.get(o.size) if rf else o.fare) or o.fare or 0)
    if not body.cash_received:
        raise HTTPException(status_code=400, detail=f"Jo'natuvchidan qaytarish haqi {_fm(fee)} so'm naqd olinishi kerak — olgach «naqd oldim» ni belgilang")
    await _ic_terminal_cash(db, T.id, fee, user.id, "return_fee", o.id, "Qaytarish haqi (faqat magistralga)")
    t = (await db.execute(select(LinehaulTrip).where(LinehaulTrip.id == o.return_trip_id))).scalars().first()
    if t and not o.return_paid:
        await _ic_pay_driver(db, t.driver_id, fee)
        o.return_paid = True
    o.status = "returned_done"
    await _ic_event(db, o, "returned_done", user, "operator", T.id, "Jo'natuvchiga qaytarib topshirildi")
    await db.commit()
    return {"ok": True}


class IcCashBody(BaseModel):
    courier_id: Optional[int] = None
    amount: Optional[float] = None
    note: Optional[str] = None


@terminal_router.post("/terminal/api/cash/receive")
async def terminal_cash_receive(body: IcCashBody, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    """Fider-2 eshikdan olgan naqd pulni terminal operatoriga topshiradi."""
    user, T = ctx
    rows = (await db.execute(select(IntercityOrder).where(IntercityOrder.cash_held_by_id == body.courier_id, IntercityOrder.cash_held_terminal_id == T.id,
                                                          IntercityOrder.cash_held_amount > 0).with_for_update())).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="Bu kuryerda topshirilmagan naqd yo'q")
    tot = 0.0
    for o in rows:
        amt = float(o.cash_held_amount)
        await _ic_credit(db, body.courier_id, amt, f"Tumanlararo #{o.id}: naqd terminalga topshirildi (band qilingan summa qaytdi)", TransactionType.REFUND)
        await _ic_terminal_cash(db, T.id, amt, user.id, "courier_cash", o.id, "Fider-2 dan naqd")
        o.cash_held_by_id, o.cash_held_amount = None, 0.0
        tot += amt
    await db.commit()
    return {"ok": True, "total": tot}


@terminal_router.post("/terminal/api/cash/deposit")
async def terminal_cash_deposit(body: IcCashBody, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    """Kassadagi naqdni seyfga (inkassatsiyaga) o'tkazish yozuvi."""
    user, T = ctx
    t = (await db.execute(select(Terminal).where(Terminal.id == T.id).with_for_update())).scalars().first()
    amt = float(body.amount or 0)
    if amt <= 0 or amt > (t.cash_balance or 0):
        raise HTTPException(status_code=400, detail="Summa noto'g'ri yoki kassadagidan ko'p")
    await _ic_terminal_cash(db, T.id, -amt, user.id, "deposit", None, (body.note or "Seyfga")[:100])
    await db.commit()
    return {"ok": True}


@terminal_router.get("/terminal/api/label/{oid}", response_class=HTMLResponse)
async def terminal_label(oid: int, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    o = (await db.execute(select(IntercityOrder).options(*_IC_LOAD).where(IntercityOrder.id == oid))).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Topilmadi")
    return _ic_label_html(o)


@terminal_router.get("/terminal/api/photo/{oid}")
async def terminal_photo(oid: int, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    o = (await db.execute(select(IntercityOrder).where(IntercityOrder.id == oid))).scalars().first()
    if not o or not o.pickup_photo:
        raise HTTPException(status_code=404, detail="Rasm yo'q")
    return Response(content=o.pickup_photo, media_type=o.pickup_photo_mime or "image/jpeg")


# ---------------- Admin ----------------
admin_express_router = APIRouter(prefix="/admin/express", tags=["Tumanlararo boshqaruv"])


def _ic_scope_filter(q, scope):
    return q if scope is None else q.where(or_(IntercityOrder.from_city_id == scope, IntercityOrder.to_city_id == scope))


@admin_express_router.get("/overview")
async def admin_ic_overview(db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("pochta"))):
    st = await _get_or_create_setting(db)
    cfg = _ic_cfg(st)
    cities = (await db.execute(select(City).order_by(City.name))).scalars().all()
    fares = (await db.execute(select(ExpressFare).order_by(ExpressFare.id))).scalars().all()
    drivers = (await db.execute(select(ExpressDriver, User).join(User, User.id == ExpressDriver.user_id).order_by(User.full_name))).all()
    terms = (await db.execute(select(Terminal).order_by(Terminal.id))).scalars().all()
    ops = (await db.execute(select(TerminalOperator, User).join(User, User.id == TerminalOperator.user_id).order_by(TerminalOperator.id))).all()
    return {"cfg": cfg, "is_owner": current_user.role == UserRole.OWNER, "sms": sms_configured(),
            "cities": [{"id": c.id, "name": c.name} for c in cities],
            "terminals": [{"id": t.id, "city_id": t.city_id, "name": t.name, "address": t.address, "lat": t.latitude, "lng": t.longitude, "hours": t.work_hours,
                           "active": bool(t.is_active), "cash": t.cash_balance or 0} for t in terms],
            "operators": [{"user_id": u.id, "name": u.full_name, "phone": u.phone_number, "terminal_id": o.terminal_id, "active": bool(o.is_active)} for o, u in ops],
            "fares": [{"id": f.id, "from": f.from_city_id, "to": f.to_city_id, "small": f.price_small, "medium": f.price_medium, "large": f.price_large, "active": bool(f.is_active)} for f in fares],
            "drivers": [{"user_id": u.id, "name": u.full_name, "phone": u.phone_number, "car": f"{d.car_model or ''} {d.car_plate or ''}".strip(),
                         "active": bool(d.is_active), "balance": d.balance or 0} for d, u in drivers]}


@admin_express_router.post("/settings")
async def admin_ic_settings(express_enabled: Optional[str] = Form(None), courier_base: float = Form(..., allow_inf_nan=False),
                            courier_per_km: float = Form(..., allow_inf_nan=False), platform_percent: float = Form(10.0, allow_inf_nan=False),
                            max_leg_km: float = Form(20.0, allow_inf_nan=False), free_days: int = Form(3), storage_fee_day: float = Form(2000.0, allow_inf_nan=False),
                            max_days: int = Form(8), max_value: float = Form(1000000.0, allow_inf_nan=False),
                            db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    if not (0 <= platform_percent <= 60) or min(courier_base, courier_per_km, storage_fee_day, max_value) < 0 or not (1 <= max_leg_km <= 200) \
            or not (0 <= free_days <= 30) or not (free_days <= max_days <= 60) or max(courier_base, courier_per_km, storage_fee_day) > 10_000_000:
        raise HTTPException(status_code=400, detail="Qiymatlar noto'g'ri")
    st = await _get_or_create_setting(db)
    st.express_enabled = bool(express_enabled)
    st.express_courier_base, st.express_courier_per_km, st.ic_platform_percent = courier_base, courier_per_km, platform_percent
    st.express_max_leg_km, st.ic_free_days, st.ic_storage_fee_day, st.ic_max_days, st.ic_max_value = max_leg_km, free_days, storage_fee_day, max_days, max_value
    await db.commit()
    return {"ok": True}


class IcTerminalBody(BaseModel):
    city_id: int
    name: str
    address: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    hours: Optional[str] = None
    active: bool = True


@admin_express_router.post("/terminal")
async def admin_ic_terminal(body: IcTerminalBody, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    if len(body.name.strip()) < 2 or len(body.address.strip()) < 3:
        raise HTTPException(status_code=400, detail="Nom va manzilni kiriting")
    if body.lat is not None and not (-90 <= body.lat <= 90 and -180 <= (body.lng or 0) <= 180):
        raise HTTPException(status_code=400, detail="Koordinata noto'g'ri")
    t = (await db.execute(select(Terminal).where(Terminal.city_id == body.city_id))).scalars().first()
    if not t:
        if not (await db.execute(select(City.id).where(City.id == body.city_id))).first():
            raise HTTPException(status_code=404, detail="Shahar topilmadi")
        t = Terminal(city_id=body.city_id, name=body.name.strip(), address=body.address.strip())
        db.add(t)
    t.name, t.address, t.latitude, t.longitude = body.name.strip()[:80], body.address.strip()[:200], body.lat, body.lng
    t.work_hours, t.is_active = (body.hours or "").strip()[:80] or None, body.active
    await db.commit()
    return {"ok": True}


@admin_express_router.post("/operators/create")
async def admin_ic_operator_create(full_name: str = Form(...), phone_number: str = Form(...), password: str = Form(...), terminal_id: int = Form(...),
                                   db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    if not (await db.execute(select(Terminal.id).where(Terminal.id == terminal_id))).first():
        raise HTTPException(status_code=404, detail="Terminal topilmadi")
    u = await find_or_create_login_user(db, phone_number, password, full_name, None)
    if (await db.execute(select(TerminalOperator.id).where(TerminalOperator.user_id == u.id))).first():
        raise HTTPException(status_code=400, detail="Bu foydalanuvchi allaqachon terminal operatori")
    db.add(TerminalOperator(user_id=u.id, terminal_id=terminal_id, is_active=True))
    await db.commit()
    return {"ok": True}


class IcToggle(BaseModel):
    active: bool


@admin_express_router.post("/operators/{uid}/active")
async def admin_ic_operator_active(uid: int, body: IcToggle, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    o = (await db.execute(select(TerminalOperator).where(TerminalOperator.user_id == uid))).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Operator topilmadi")
    o.is_active = body.active
    await db.commit()
    return {"ok": True}


class IcHubBody(BaseModel):
    city_id: int
    lat: Optional[float] = None
    lng: Optional[float] = None


class IcFareBody(BaseModel):
    from_city_id: int
    to_city_id: int
    price_small: float
    price_medium: float = 0.0
    price_large: float = 0.0
    is_active: bool = True


@admin_express_router.post("/fare")
async def admin_ic_fare(body: IcFareBody, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    if body.from_city_id == body.to_city_id or min(body.price_small, body.price_medium, body.price_large) < 0 or max(body.price_small, body.price_medium, body.price_large) > 100_000_000:
        raise HTTPException(status_code=400, detail="Qiymatlar noto'g'ri")
    f = (await db.execute(select(ExpressFare).where(ExpressFare.from_city_id == body.from_city_id, ExpressFare.to_city_id == body.to_city_id))).scalars().first()
    if not f:
        f = ExpressFare(from_city_id=body.from_city_id, to_city_id=body.to_city_id)
        db.add(f)
    f.price_small, f.price_medium, f.price_large = body.price_small, body.price_medium or body.price_small, body.price_large or body.price_medium or body.price_small
    f.is_active = body.is_active
    await db.commit()
    return {"ok": True}


@admin_express_router.post("/fare/{fid}/delete")
async def admin_ic_fare_delete(fid: int, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    await db.execute(delete(ExpressFare).where(ExpressFare.id == fid))
    await db.commit()
    return {"ok": True}


@admin_express_router.post("/drivers/create")
async def admin_ic_driver_create(full_name: str = Form(...), phone_number: str = Form(...), password: str = Form(...),
                                 car_model: str = Form(""), car_plate: str = Form(""),
                                 db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    u = await find_or_create_login_user(db, phone_number, password, full_name, None)
    if (await db.execute(select(ExpressDriver.id).where(ExpressDriver.user_id == u.id))).first():
        raise HTTPException(status_code=400, detail="Bu foydalanuvchi allaqachon magistral haydovchi")
    db.add(ExpressDriver(user_id=u.id, car_model=car_model.strip()[:80] or None, car_plate=car_plate.strip()[:20] or None, is_active=True))
    await db.commit()
    return {"ok": True}


@admin_express_router.post("/drivers/{uid}/active")
async def admin_ic_driver_active(uid: int, body: IcToggle, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    d = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == uid))).scalars().first()
    if not d:
        raise HTTPException(status_code=404, detail="Haydovchi topilmadi")
    d.is_active = body.active
    await db.commit()
    return {"ok": True}


class IcPayout(BaseModel):
    amount: float


@admin_express_router.post("/drivers/{uid}/payout")
async def admin_ic_driver_payout(uid: int, body: IcPayout, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    d = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == uid).with_for_update())).scalars().first()
    if not d or body.amount <= 0 or body.amount > (d.balance or 0):
        raise HTTPException(status_code=400, detail="Summa noto'g'ri yoki haydovchi balansidan ko'p")
    d.balance = (d.balance or 0) - body.amount
    db.add(Transaction(user_id=uid, type=TransactionType.WITHDRAWAL, amount=body.amount, note="Magistral haydovchiga to'langan", created_by_id=owner.id))
    await db.commit()
    return {"ok": True}


@admin_express_router.get("/orders")
async def admin_ic_orders(status_filter: Optional[str] = None, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("pochta"))):
    scope = _parcel_scope(current_user)
    cfg = _ic_cfg(await _get_or_create_setting(db))
    q = _ic_scope_filter(select(IntercityOrder).options(*_IC_LOAD).order_by(IntercityOrder.id.desc()).limit(150), scope)
    if status_filter in IC_STATUS_UZ:
        q = q.where(IntercityOrder.status == status_filter)
    rows = (await db.execute(q)).scalars().all()
    cq = _ic_scope_filter(select(IntercityOrder.status, func.count(IntercityOrder.id)).group_by(IntercityOrder.status), scope)
    items = []
    for o in rows:
        r = _ic_public(o, cfg, False)
        r.update(client=o.client.full_name if o.client else "", client_phone=o.client.phone_number if o.client else "", leg1_fee=o.leg1_fee, leg3_fee=o.leg3_fee,
                 fare=o.fare, platform_fee=o.platform_fee, declared_value=o.declared_value, cancel_reason=o.cancel_reason, cash_held=o.cash_held_amount if o.cash_held_by_id else 0,
                 has_photo=bool(o.pickup_photo))
        items.append(r)
    return {"items": items, "counts": {k: v for k, v in (await db.execute(cq)).all()}}


@admin_express_router.get("/orders/{oid}/events")
async def admin_ic_events(oid: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("pochta"))):
    o = (await db.execute(select(IntercityOrder).where(IntercityOrder.id == oid))).scalars().first()
    scope = _parcel_scope(current_user)
    if not o or (scope is not None and scope not in (o.from_city_id, o.to_city_id)):
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    ev = (await db.execute(select(IntercityEvent, User).outerjoin(User, User.id == IntercityEvent.actor_id).where(IntercityEvent.order_id == oid).order_by(IntercityEvent.id))).all()
    return {"items": [{"status": IC_STATUS_UZ.get(e.status, e.status), "role": e.actor_role, "who": u.full_name if u else "", "note": e.note,
                       "at": e.created_at.isoformat() if e.created_at else None} for e, u in ev]}


@admin_express_router.get("/orders/{oid}/photo")
async def admin_ic_photo(oid: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("pochta"))):
    o = (await db.execute(select(IntercityOrder).where(IntercityOrder.id == oid))).scalars().first()
    scope = _parcel_scope(current_user)
    if not o or not o.pickup_photo or (scope is not None and scope not in (o.from_city_id, o.to_city_id)):
        raise HTTPException(status_code=404, detail="Rasm yo'q")
    return Response(content=o.pickup_photo, media_type=o.pickup_photo_mime or "image/jpeg")


@admin_express_router.post("/orders/{oid}/cancel")
async def admin_ic_cancel(oid: int, body: ParcelCancelBody, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("pochta"))):
    scope = _parcel_scope(current_user)
    o = (await db.execute(select(IntercityOrder).where(IntercityOrder.id == oid).with_for_update())).scalars().first()
    if not o or (scope is not None and scope not in (o.from_city_id, o.to_city_id)):
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    if o.status in ("delivered", "cancelled", "returned_done"):
        raise HTTPException(status_code=400, detail="Bu buyurtma allaqachon yakunlangan")
    people = [o.f1_id, o.f2_id]
    if o.cash_held_by_id and (o.cash_held_amount or 0) > 0:
        await _ic_credit(db, o.cash_held_by_id, float(o.cash_held_amount), f"Tumanlararo #{o.id} bekor qilindi: band qilingan summa qaytarildi", TransactionType.REFUND)
        o.cash_held_by_id, o.cash_held_amount = None, 0.0
    drv = o.trip.driver_id if o.trip_id and o.trip else None
    o.status, o.cancelled_at, o.cancelled_by = "cancelled", datetime.utcnow(), "admin"
    o.cancel_reason = (body.reason or "").strip()[:200] or None
    await _ic_event(db, o, "cancelled", current_user, "admin", note=o.cancel_reason or "Administrator bekor qildi")
    await db.commit()
    reason = f"\nSabab: {o.cancel_reason}" if o.cancel_reason else ""
    await _parcel_notify_client(db, o, f"❌ Tumanlararo posilka #{o.id} bekor qilindi.{reason}")
    for uid in people + ([drv] if drv else []):
        await _ic_notify(db, uid, f"❌ Tumanlararo posilka #{o.id} administrator tomonidan bekor qilindi.{reason}\nBuyum sizda bo'lsa, jo'natuvchiga qaytaring.")
    return {"ok": True}


class IcServiceBody(BaseModel):
    key: str
    value: bool


@admin_express_router.get("/services")
async def admin_ic_services(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_admin_user)):
    st = await _get_or_create_setting(db)
    return {"is_owner": current_user.role == UserRole.OWNER, "services": {
        "pochta_local": bool(getattr(st, "parcel_enabled", False)), "pochta_intercity": bool(getattr(st, "express_enabled", False)),
        "taxi_local": bool(getattr(st, "taxi_local_enabled", False)), "taxi_magistral": bool(getattr(st, "taxi_magistral_enabled", False))}}


@admin_express_router.post("/services")
async def admin_ic_services_set(body: IcServiceBody, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    col = {"pochta_local": "parcel_enabled", "pochta_intercity": "express_enabled", "taxi_local": "taxi_local_enabled", "taxi_magistral": "taxi_magistral_enabled"}.get(body.key)
    if not col:
        raise HTTPException(status_code=400, detail="Noma'lum xizmat")
    st = await _get_or_create_setting(db)
    setattr(st, col, bool(body.value))
    await db.commit()
    return {"ok": True}




# ==================== MAGISTRAL TAKSI (B bosqich): navbat, bron, geofence ====================
MAG_TOP = 5


def _mag_cfg(setting) -> dict:
    def g(n, d):
        v = getattr(setting, n, None)
        return d if v is None else v
    return {"enabled": bool(g("taxi_magistral_enabled", False)), "radius_m": int(g("mag_radius_m", 500)), "stale_min": int(g("mag_stale_min", 5)),
            "commission": float(g("mag_commission_percent", 10.0)), "min_deposit": float(g("mag_min_deposit", 0.0)),
            "seat_small": float(g("mag_seat_small", 0.25)), "seat_medium": float(g("mag_seat_medium", 0.5)), "seat_large": float(g("mag_seat_large", 1.0))}


def _mag_pseat(cfg: dict, size: str) -> float:
    return cfg.get("seat_" + (size or "small"), cfg["seat_small"])


def _mag_dist_m(drv, terminal) -> Optional[float]:
    if drv is None or terminal is None or None in (drv.latitude, drv.longitude, terminal.latitude, terminal.longitude):
        return None
    return _haversine_km(drv.latitude, drv.longitude, terminal.latitude, terminal.longitude) * 1000.0


async def _mag_route(db: AsyncSession, from_city_id: int, to_city_id: int) -> Optional[MagRoute]:
    return (await db.execute(select(MagRoute).where(MagRoute.from_city_id == from_city_id, MagRoute.to_city_id == to_city_id,
                                                    MagRoute.is_active == True))).scalars().first()


async def _mag_entry_for_trip(db: AsyncSession, trip_id: int) -> Optional[MagQueueEntry]:
    return (await db.execute(select(MagQueueEntry).where(MagQueueEntry.trip_id == trip_id))).scalars().first()


async def _mag_info(db: AsyncSession, e: MagQueueEntry, cfg: dict, route: Optional[MagRoute], drv: ExpressDriver) -> dict:
    bk = (await db.execute(select(MagBooking).options(selectinload(MagBooking.client)).where(MagBooking.entry_id == e.id, MagBooking.status == "booked").order_by(MagBooking.id))).scalars().all()
    ps = (await db.execute(select(IntercityOrder).where(IntercityOrder.trip_id == e.trip_id, IntercityOrder.status == "in_transit"))).scalars().all()
    rt = (await db.execute(select(IntercityOrder).where(IntercityOrder.return_trip_id == e.trip_id, IntercityOrder.status == "returning"))).scalars().all()
    pseats = sum(_mag_pseat(cfg, o.size) for o in ps + rt)
    sb = sum(b.seats for b in bk)
    income = sum(b.amount for b in bk) + sum(float(o.fare or 0) for o in ps)
    cap = (drv.seats if drv and drv.seats else 4)
    free = cap - sb - pseats
    minv = float(route.min_income) if route else 0.0
    full = free <= 1e-6
    ready = full or (income > 0 and income >= minv)
    return {"entry_id": e.id, "trip_id": e.trip_id, "capacity": cap, "seats_booked": sb, "parcel_seats": round(pseats, 2), "free_seats": max(int(free + 1e-9), 0),
            "income": income, "min_income": minv, "ready": ready, "full": full, "price": float(route.seat_price) if route else 0.0,
            "passengers": [{"id": b.id, "name": b.client.full_name if b.client else "", "phone": b.client.phone_number if b.client else "", "seats": b.seats, "amount": b.amount} for b in bk],
            "parcels": len(ps) + len(rt)}


async def _mag_release(db: AsyncSession, e: MagQueueEntry, status: str, reason: str) -> None:
    """Navbatdan chiqarish: posilkalar terminalga qaytariladi, bronlar bekor, komissiya depozitga qaytadi."""
    t = (await db.execute(select(LinehaulTrip).where(LinehaulTrip.id == e.trip_id).with_for_update())).scalars().first()
    if t is not None:
        for o in await _ic_trip_orders(db, t):
            if o.status == "in_transit" and o.trip_id == t.id:
                o.status, o.trip_id = "at_a", None
                await _ic_event(db, o, "at_a", None, "system", e.from_terminal_id, "Haydovchi navbatdan tushdi — posilka terminalga qaytarildi")
            elif o.status == "returning" and o.return_trip_id == t.id:
                o.status, o.return_trip_id = "at_b", None
        t.status = "cancelled"
    drv = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == e.driver_id).with_for_update())).scalars().first()
    bks = (await db.execute(select(MagBooking).where(MagBooking.entry_id == e.id, MagBooking.status == "booked"))).scalars().all()
    for b in bks:
        b.status = "dropped"
        if drv is not None:
            drv.deposit = (drv.deposit or 0) + (b.commission or 0)
        await _ic_notify(db, b.client_id, "⚠️ Haydovchi navbatdan tushdi, broningiz bekor bo'ldi. Iltimos, boshqa mashinani tanlang.")
    e.status, e.drop_reason, e.ended_at = status, reason, datetime.utcnow()
    if status == "dropped":
        await _ic_notify(db, e.driver_id, f"⚠️ Siz navbatdan tushdingiz: {reason}. Qayta navbatga yozilishingiz kerak.")


async def _mag_sweep(db: AsyncSession) -> None:
    """Geofencing: offlayn yoki terminaldan uzoqlashgan, qora ro'yxatdagi haydovchilar navbatdan tushadi."""
    cfg = _mag_cfg(await _get_or_create_setting(db))
    es = (await db.execute(select(MagQueueEntry).options(selectinload(MagQueueEntry.from_terminal)).where(MagQueueEntry.status == "waiting"))).scalars().all()
    changed = False
    now = datetime.utcnow()
    for e in es:
        drv = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == e.driver_id))).scalars().first()
        reason, status = None, "dropped"
        if drv is None or not drv.is_active or drv.blacklisted:
            reason, status = "qora ro'yxat yoki o'chirilgan", "removed"
        elif drv.location_at is None or (now - drv.location_at).total_seconds() > cfg["stale_min"] * 60:
            reason = "onlayn emas (joylashuv kelmayapti)"
        else:
            d = _mag_dist_m(drv, e.from_terminal)
            if d is not None and d > cfg["radius_m"] * 2:
                reason = f"terminaldan uzoqlashdi ({int(d)} m)"
        if reason:
            await _mag_release(db, e, status, reason)
            changed = True
    if changed:
        await db.commit()


async def _mag_waiting(db: AsyncSession, from_tid: int, to_tid: int):
    return (await db.execute(select(MagQueueEntry).options(selectinload(MagQueueEntry.driver)).where(
        MagQueueEntry.from_terminal_id == from_tid, MagQueueEntry.to_terminal_id == to_tid, MagQueueEntry.status == "waiting")
        .order_by(MagQueueEntry.applied_at, MagQueueEntry.id))).scalars().all()


async def _mag_prepare_depart(db: AsyncSession, trip: LinehaulTrip) -> bool:
    """Navbatli reys uchun jo'nash shartlari. Navbat yozuvi bo'lsa True."""
    e = await _mag_entry_for_trip(db, trip.id)
    if e is None:
        return False
    if e.status != "waiting":
        raise HTTPException(status_code=400, detail="Bu mashina navbatda emas")
    q = await _mag_waiting(db, e.from_terminal_id, e.to_terminal_id)
    if not q or q[0].id != e.id:
        raise HTTPException(status_code=400, detail="Hali sizning navbatingiz emas — oldingi mashina jo'nashi kerak")
    cfg = _mag_cfg(await _get_or_create_setting(db))
    drv = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == e.driver_id))).scalars().first()
    fcity = (await db.execute(select(Terminal.city_id).where(Terminal.id == e.from_terminal_id))).scalar()
    tcity = (await db.execute(select(Terminal.city_id).where(Terminal.id == e.to_terminal_id))).scalar()
    info = await _mag_info(db, e, cfg, await _mag_route(db, fcity, tcity), drv)
    if not info["ready"]:
        raise HTTPException(status_code=400, detail=f"Jo'nash uchun daromad yetarli emas: {_fm(info['income'])} / {_fm(info['min_income'])} so'm")
    e.status = "departed"
    return True


async def _mag_finish(db: AsyncSession, trip: LinehaulTrip) -> None:
    e = await _mag_entry_for_trip(db, trip.id)
    if e is None or e.status != "departed":
        return
    e.status, e.ended_at = "arrived", datetime.utcnow()
    for b in (await db.execute(select(MagBooking).where(MagBooking.entry_id == e.id, MagBooking.status == "booked"))).scalars().all():
        b.status = "done"


# ---------------- Haydovchi ----------------
driver_mag_router = APIRouter(prefix="/driver/mag", tags=["Magistral taksi: haydovchi"])


@driver_mag_router.get("/state")
async def driver_mag_state(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_express_driver)):
    await _mag_sweep(db)
    st = await _get_or_create_setting(db)
    cfg = _mag_cfg(st)
    drv = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == user.id))).scalars().first()
    terms = {t.city_id: t for t in (await db.execute(select(Terminal).where(Terminal.is_active == True))).scalars().all()}
    routes = (await db.execute(select(MagRoute).where(MagRoute.is_active == True))).scalars().all()
    out_routes = []
    for r in routes:
        a, b = terms.get(r.from_city_id), terms.get(r.to_city_id)
        if a and b:
            d = _mag_dist_m(drv, a)
            out_routes.append({"from": r.from_city_id, "to": r.to_city_id, "label": f"{a.name} → {b.name}", "seat_price": r.seat_price, "min_income": r.min_income,
                               "dist_m": None if d is None else int(d)})
    e = (await db.execute(select(MagQueueEntry).options(selectinload(MagQueueEntry.from_terminal), selectinload(MagQueueEntry.to_terminal)).where(
        MagQueueEntry.driver_id == user.id, MagQueueEntry.status.in_(["waiting", "departed"])).order_by(MagQueueEntry.id.desc()))).scalars().first()
    entry = None
    if e:
        route = await _mag_route(db, e.from_terminal.city_id, e.to_terminal.city_id)
        info = await _mag_info(db, e, cfg, route, drv)
        pos, total = None, None
        if e.status == "waiting":
            q = await _mag_waiting(db, e.from_terminal_id, e.to_terminal_id)
            total = len(q)
            pos = next((i + 1 for i, x in enumerate(q) if x.id == e.id), None)
        entry = {**info, "status": e.status, "position": pos, "queue_len": total, "from": e.from_terminal.name, "to": e.to_terminal.name}
    last = (await db.execute(select(MagQueueEntry).where(MagQueueEntry.driver_id == user.id, MagQueueEntry.status.in_(["dropped", "removed"])).order_by(MagQueueEntry.id.desc()).limit(1))).scalars().first()
    return {"enabled": cfg["enabled"], "radius_m": cfg["radius_m"], "deposit": drv.deposit or 0, "min_deposit": cfg["min_deposit"], "commission": cfg["commission"],
            "blacklisted": bool(drv.blacklisted), "blacklist_reason": drv.blacklist_reason, "routes": out_routes, "entry": entry,
            "last_drop": {"reason": last.drop_reason, "at": last.ended_at.isoformat() if last.ended_at else None} if last and not entry else None}


class MagApplyBody(BaseModel):
    from_city_id: int
    to_city_id: int


@driver_mag_router.post("/apply")
async def driver_mag_apply(body: MagApplyBody, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_express_driver)):
    st = await _get_or_create_setting(db)
    cfg = _mag_cfg(st)
    if not cfg["enabled"]:
        raise HTTPException(status_code=403, detail="Magistral taksi hozircha yoqilmagan")
    drv = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == user.id).with_for_update())).scalars().first()
    if drv.blacklisted:
        raise HTTPException(status_code=403, detail="Siz qora ro'yxatdasiz" + (f": {drv.blacklist_reason}" if drv.blacklist_reason else ""))
    if (drv.deposit or 0) < cfg["min_deposit"]:
        raise HTTPException(status_code=400, detail=f"Depozit yetarli emas (kerak: {_fm(cfg['min_deposit'])} so'm, sizda: {_fm(drv.deposit or 0)})")
    route = await _mag_route(db, body.from_city_id, body.to_city_id)
    if not route:
        raise HTTPException(status_code=400, detail="Bu yo'nalishda magistral taksi yo'q")
    ta, tb = await _ic_active_terminal(db, body.from_city_id), await _ic_active_terminal(db, body.to_city_id)
    if (await db.execute(select(MagQueueEntry.id).where(MagQueueEntry.driver_id == user.id, MagQueueEntry.status.in_(["waiting", "departed"])))).first():
        raise HTTPException(status_code=400, detail="Siz allaqachon navbatdasiz yoki yo'ldasiz")
    for t in (await db.execute(select(LinehaulTrip).where(LinehaulTrip.driver_id == user.id, LinehaulTrip.status.in_(["waiting", "departed", "arrived"])))).scalars().all():
        if t.status != "arrived" or await _ic_trip_orders(db, t):
            raise HTTPException(status_code=400, detail="Sizda tugallanmagan reys bor")
    now = datetime.utcnow()
    if drv.location_at is None or (now - drv.location_at).total_seconds() > cfg["stale_min"] * 60:
        raise HTTPException(status_code=400, detail="Joylashuvingiz aniqlanmadi. Geolokatsiyani yoqing va qayta urinib ko'ring.")
    d = _mag_dist_m(drv, ta)
    if d is None:
        raise HTTPException(status_code=400, detail="Terminal koordinatasi belgilanmagan")
    if d > cfg["radius_m"]:
        raise HTTPException(status_code=400, detail=f"Terminaldan {int(d)} m uzoqdasiz. Navbatga yozilish uchun {cfg['radius_m']} m ichida bo'ling.")
    trip = LinehaulTrip(driver_id=user.id, from_terminal_id=ta.id, to_terminal_id=tb.id, status="waiting")
    db.add(trip)
    await db.flush()
    db.add(MagQueueEntry(driver_id=user.id, trip_id=trip.id, from_terminal_id=ta.id, to_terminal_id=tb.id, status="waiting"))
    await db.commit()
    return {"ok": True}


@driver_mag_router.post("/leave")
async def driver_mag_leave(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_express_driver)):
    e = (await db.execute(select(MagQueueEntry).where(MagQueueEntry.driver_id == user.id, MagQueueEntry.status == "waiting").with_for_update())).scalars().first()
    if not e:
        raise HTTPException(status_code=404, detail="Siz navbatda emassiz")
    await _mag_release(db, e, "removed", "Haydovchi o'zi chiqdi")
    await db.commit()
    return {"ok": True}


# ---------------- Mijoz (yo'lovchi) ----------------
mag_client_router = APIRouter(prefix="/api/shop/mag", tags=["Magistral taksi: yo'lovchi"])


@mag_client_router.post("/config")
async def mag_config(body: IcConfigBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    cfg = _mag_cfg(await _get_or_create_setting(db))
    dests = []
    if cfg["enabled"] and client.city_id is not None:
        rows = (await db.execute(select(MagRoute.to_city_id, City.name, MagRoute.seat_price).join(City, City.id == MagRoute.to_city_id).where(
            MagRoute.from_city_id == client.city_id, MagRoute.is_active == True, City.is_active == True).order_by(City.name))).all()
        for cid, nm, pr in rows:
            if (await db.execute(select(Terminal.id).where(Terminal.city_id == cid, Terminal.is_active == True))).first():
                dests.append({"id": cid, "name": nm, "price": pr})
        if not (await db.execute(select(Terminal.id).where(Terminal.city_id == client.city_id, Terminal.is_active == True))).first():
            dests = []
    return {"enabled": bool(dests), "destinations": dests}


class MagQueueBody(BaseModel):
    init_data: str
    to_city_id: int


@mag_client_router.post("/queue")
async def mag_queue(body: MagQueueBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    await _mag_sweep(db)
    cfg = _mag_cfg(await _get_or_create_setting(db))
    if not cfg["enabled"] or client.city_id is None:
        raise HTTPException(status_code=403, detail="Magistral taksi hozircha ishlamayapti")
    route = await _mag_route(db, client.city_id, body.to_city_id)
    if not route:
        raise HTTPException(status_code=400, detail="Bu yo'nalishda xizmat yo'q")
    ta, tb = await _ic_active_terminal(db, client.city_id), await _ic_active_terminal(db, body.to_city_id)
    q = (await _mag_waiting(db, ta.id, tb.id))[:MAG_TOP]
    items = []
    for i, e in enumerate(q):
        drv = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == e.driver_id))).scalars().first()
        info = await _mag_info(db, e, cfg, route, drv)
        per_comm = round(route.seat_price * cfg["commission"] / 100.0)
        items.append({"entry_id": e.id, "position": i + 1, "driver": e.driver.full_name, "car": f"{drv.car_model or ''} {drv.car_plate or ''}".strip(),
                      "free_seats": info["free_seats"], "capacity": info["capacity"], "ready": info["ready"],
                      "bookable": info["free_seats"] >= 1 and (drv.deposit or 0) >= per_comm})
    return {"price": route.seat_price, "from_terminal": ta.name, "from_address": ta.address, "to_terminal": tb.name, "items": items}


class MagBookBody(BaseModel):
    init_data: str
    entry_id: int
    seats: int = 1


@mag_client_router.post("/book")
async def mag_book(body: MagBookBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    await _mag_sweep(db)
    cfg = _mag_cfg(await _get_or_create_setting(db))
    if not cfg["enabled"]:
        raise HTTPException(status_code=403, detail="Magistral taksi hozircha ishlamayapti")
    if body.seats < 1 or body.seats > 6:
        raise HTTPException(status_code=400, detail="O'rindiqlar soni noto'g'ri")
    if (await db.execute(select(func.count(MagBooking.id)).where(MagBooking.client_id == client.id, MagBooking.status == "booked"))).scalar():
        raise HTTPException(status_code=400, detail="Sizda allaqachon faol bron bor. Avval uni bekor qiling.")
    e = (await db.execute(select(MagQueueEntry).options(selectinload(MagQueueEntry.from_terminal), selectinload(MagQueueEntry.to_terminal)).where(
        MagQueueEntry.id == body.entry_id, MagQueueEntry.status == "waiting").with_for_update())).scalars().first()
    if not e or e.from_terminal.city_id != client.city_id:
        raise HTTPException(status_code=404, detail="Bu mashina navbatda emas")
    q = (await _mag_waiting(db, e.from_terminal_id, e.to_terminal_id))[:MAG_TOP]
    if e.id not in [x.id for x in q]:
        raise HTTPException(status_code=400, detail="Bu mashina hali birinchi 5 talikda emas")
    route = await _mag_route(db, e.from_terminal.city_id, e.to_terminal.city_id)
    if not route:
        raise HTTPException(status_code=400, detail="Yo'nalish topilmadi")
    drv = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == e.driver_id).with_for_update())).scalars().first()
    info = await _mag_info(db, e, cfg, route, drv)
    if body.seats > info["free_seats"]:
        raise HTTPException(status_code=400, detail=f"Bo'sh o'rindiq yetarli emas (bo'sh: {info['free_seats']})")
    amount = float(route.seat_price) * body.seats
    comm = float(round(amount * cfg["commission"] / 100.0))
    if (drv.deposit or 0) < comm:
        raise HTTPException(status_code=400, detail="Bu haydovchida depozit yetarli emas, boshqa mashinani tanlang")
    drv.deposit = (drv.deposit or 0) - comm
    b = MagBooking(entry_id=e.id, client_id=client.id, seats=body.seats, amount=amount, commission=comm)
    db.add(b)
    await db.commit()
    await _ic_notify(db, e.driver_id, f"🧍 Yangi yo'lovchi: {client.full_name} ({client.phone_number}), {body.seats} o'rindiq — {_fm(amount)} so'm (naqd).")
    return {"ok": True, "id": b.id, "amount": amount, "driver": e.driver.full_name if e.driver else None}


@mag_client_router.post("/my")
async def mag_my(body: IcConfigBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    await _mag_sweep(db)
    rows = (await db.execute(select(MagBooking).options(selectinload(MagBooking.entry).selectinload(MagQueueEntry.driver),
                                                        selectinload(MagBooking.entry).selectinload(MagQueueEntry.from_terminal),
                                                        selectinload(MagBooking.entry).selectinload(MagQueueEntry.to_terminal))
                             .where(MagBooking.client_id == client.id).order_by(MagBooking.id.desc()).limit(10))).scalars().all()
    out = []
    for b in rows:
        e = b.entry
        drv = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == e.driver_id))).scalars().first()
        pos = None
        if b.status == "booked" and e.status == "waiting":
            q = await _mag_waiting(db, e.from_terminal_id, e.to_terminal_id)
            pos = next((i + 1 for i, x in enumerate(q) if x.id == e.id), None)
        show = b.status in ("booked", "done")
        out.append({"id": b.id, "status": b.status, "trip_status": e.status, "seats": b.seats, "amount": b.amount, "position": pos,
                    "from": e.from_terminal.name, "from_address": e.from_terminal.address, "to": e.to_terminal.name,
                    "driver": e.driver.full_name if show else None, "driver_phone": e.driver.phone_number if show else None,
                    "car": f"{drv.car_model or ''} {drv.car_plate or ''}".strip() if show and drv else None,
                    "created_at": b.created_at.isoformat() if b.created_at else None})
    return {"items": out}


@mag_client_router.post("/cancel")
async def mag_cancel(body: IcIdBody, db: AsyncSession = Depends(get_db)):
    client = await _client_for_cancel(db, body.init_data)
    b = (await db.execute(select(MagBooking).where(MagBooking.id == body.id, MagBooking.client_id == client.id).with_for_update())).scalars().first()
    if not b or b.status != "booked":
        raise HTTPException(status_code=404, detail="Bron topilmadi")
    e = (await db.execute(select(MagQueueEntry).where(MagQueueEntry.id == b.entry_id))).scalars().first()
    if e.status != "waiting":
        raise HTTPException(status_code=400, detail="Mashina yo'lga chiqib bo'lgan, bekor qilib bo'lmaydi")
    drv = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == e.driver_id).with_for_update())).scalars().first()
    if drv:
        drv.deposit = (drv.deposit or 0) + (b.commission or 0)
    b.status = "cancelled"
    await db.commit()
    await _ic_notify(db, e.driver_id, f"❌ Yo'lovchi {client.full_name} broni bekor qildi ({b.seats} o'rindiq).")
    return {"ok": True}


# ---------------- Terminal operatori ----------------
@terminal_router.get("/terminal/api/mag")
async def terminal_mag(db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    user, T = ctx
    await _mag_sweep(db)
    cfg = _mag_cfg(await _get_or_create_setting(db))
    es = (await db.execute(select(MagQueueEntry).options(selectinload(MagQueueEntry.driver), selectinload(MagQueueEntry.to_terminal)).where(
        MagQueueEntry.from_terminal_id == T.id, MagQueueEntry.status == "waiting").order_by(MagQueueEntry.applied_at, MagQueueEntry.id))).scalars().all()
    out, seen = [], {}
    for e in es:
        drv = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == e.driver_id))).scalars().first()
        route = await _mag_route(db, T.city_id, e.to_terminal.city_id)
        seen[e.to_terminal_id] = seen.get(e.to_terminal_id, 0) + 1
        info = await _mag_info(db, e, cfg, route, drv)
        out.append({**info, "driver": e.driver.full_name, "phone": e.driver.phone_number, "car": f"{drv.car_model or ''} {drv.car_plate or ''}".strip(),
                    "to": e.to_terminal.name, "position": seen[e.to_terminal_id], "can_depart": seen[e.to_terminal_id] == 1 and info["ready"]})
    return {"items": out, "enabled": cfg["enabled"]}


@terminal_router.post("/terminal/api/mag/{eid}/remove")
async def terminal_mag_remove(eid: int, db: AsyncSession = Depends(get_db), ctx=Depends(get_current_terminal_op)):
    user, T = ctx
    e = (await db.execute(select(MagQueueEntry).where(MagQueueEntry.id == eid, MagQueueEntry.from_terminal_id == T.id, MagQueueEntry.status == "waiting").with_for_update())).scalars().first()
    if not e:
        raise HTTPException(status_code=404, detail="Navbat yozuvi topilmadi")
    await _mag_release(db, e, "removed", "Operator navbatdan chiqardi")
    await db.commit()
    return {"ok": True}


# ---------------- Admin ----------------
admin_mag_router = APIRouter(prefix="/admin/mag", tags=["Magistral taksi boshqaruvi"])


@admin_mag_router.get("/overview")
async def admin_mag_overview(db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("taksi"))):
    await _mag_sweep(db)
    st = await _get_or_create_setting(db)
    cfg = _mag_cfg(st)
    cities = (await db.execute(select(City).order_by(City.name))).scalars().all()
    routes = (await db.execute(select(MagRoute).order_by(MagRoute.id))).scalars().all()
    drivers = (await db.execute(select(ExpressDriver, User).join(User, User.id == ExpressDriver.user_id).order_by(User.full_name))).all()
    es = (await db.execute(select(MagQueueEntry).options(selectinload(MagQueueEntry.driver), selectinload(MagQueueEntry.from_terminal), selectinload(MagQueueEntry.to_terminal)).where(
        MagQueueEntry.status.in_(["waiting", "departed"])).order_by(MagQueueEntry.applied_at))).scalars().all()
    queue = []
    for e in es:
        drv = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == e.driver_id))).scalars().first()
        info = await _mag_info(db, e, cfg, await _mag_route(db, e.from_terminal.city_id, e.to_terminal.city_id), drv)
        queue.append({**info, "status": e.status, "driver": e.driver.full_name, "from": e.from_terminal.name, "to": e.to_terminal.name})
    return {"cfg": cfg, "is_owner": current_user.role == UserRole.OWNER, "cities": [{"id": c.id, "name": c.name} for c in cities],
            "routes": [{"id": r.id, "from": r.from_city_id, "to": r.to_city_id, "seat_price": r.seat_price, "min_income": r.min_income, "active": bool(r.is_active)} for r in routes],
            "drivers": [{"user_id": u.id, "name": u.full_name, "phone": u.phone_number, "deposit": d.deposit or 0, "blacklisted": bool(d.blacklisted), "reason": d.blacklist_reason,
                         "seats": d.seats or 4, "active": bool(d.is_active)} for d, u in drivers], "queue": queue}


@admin_mag_router.post("/settings")
async def admin_mag_settings(radius_m: int = Form(500), stale_min: int = Form(5), commission: float = Form(10.0, allow_inf_nan=False),
                             min_deposit: float = Form(0.0, allow_inf_nan=False), seat_small: float = Form(0.25, allow_inf_nan=False),
                             seat_medium: float = Form(0.5, allow_inf_nan=False), seat_large: float = Form(1.0, allow_inf_nan=False),
                             db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    if not (50 <= radius_m <= 20000) or not (1 <= stale_min <= 120) or not (0 <= commission <= 60) or min_deposit < 0 or min_deposit > 100_000_000 \
            or not all(0 < v <= 6 for v in (seat_small, seat_medium, seat_large)):
        raise HTTPException(status_code=400, detail="Qiymatlar noto'g'ri")
    st = await _get_or_create_setting(db)
    st.mag_radius_m, st.mag_stale_min, st.mag_commission_percent, st.mag_min_deposit = radius_m, stale_min, commission, min_deposit
    st.mag_seat_small, st.mag_seat_medium, st.mag_seat_large = seat_small, seat_medium, seat_large
    await db.commit()
    return {"ok": True}


class MagRouteBody(BaseModel):
    from_city_id: int
    to_city_id: int
    seat_price: float
    min_income: float = 0.0
    is_active: bool = True


@admin_mag_router.post("/route")
async def admin_mag_route(body: MagRouteBody, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    if body.from_city_id == body.to_city_id or not (0 < body.seat_price <= 100_000_000) or not (0 <= body.min_income <= 1_000_000_000):
        raise HTTPException(status_code=400, detail="Qiymatlar noto'g'ri")
    r = (await db.execute(select(MagRoute).where(MagRoute.from_city_id == body.from_city_id, MagRoute.to_city_id == body.to_city_id))).scalars().first()
    if not r:
        r = MagRoute(from_city_id=body.from_city_id, to_city_id=body.to_city_id)
        db.add(r)
    r.seat_price, r.min_income, r.is_active = body.seat_price, body.min_income, body.is_active
    await db.commit()
    return {"ok": True}


@admin_mag_router.post("/route/{rid}/delete")
async def admin_mag_route_delete(rid: int, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    await db.execute(delete(MagRoute).where(MagRoute.id == rid))
    await db.commit()
    return {"ok": True}


class MagDepositBody(BaseModel):
    amount: float


@admin_mag_router.post("/drivers/{uid}/deposit")
async def admin_mag_deposit(uid: int, body: MagDepositBody, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    d = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == uid).with_for_update())).scalars().first()
    if not d or body.amount == 0 or abs(body.amount) > 1_000_000_000 or (d.deposit or 0) + body.amount < 0:
        raise HTTPException(status_code=400, detail="Summa noto'g'ri")
    d.deposit = (d.deposit or 0) + body.amount
    db.add(Transaction(user_id=uid, type=TransactionType.DEPOSIT if body.amount > 0 else TransactionType.WITHDRAWAL, amount=abs(body.amount),
                       note="Magistral depozit " + ("to'ldirildi" if body.amount > 0 else "yechildi"), created_by_id=owner.id))
    await db.commit()
    return {"ok": True, "deposit": d.deposit}


class MagBlackBody(BaseModel):
    value: bool
    reason: Optional[str] = None


@admin_mag_router.post("/drivers/{uid}/blacklist")
async def admin_mag_blacklist(uid: int, body: MagBlackBody, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    d = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == uid).with_for_update())).scalars().first()
    if not d:
        raise HTTPException(status_code=404, detail="Haydovchi topilmadi")
    d.blacklisted, d.blacklist_reason = body.value, (body.reason or "").strip()[:200] or None if body.value else None
    await db.commit()
    await _mag_sweep(db)
    return {"ok": True}


class MagSeatsBody(BaseModel):
    seats: int


@admin_mag_router.post("/drivers/{uid}/seats")
async def admin_mag_seats(uid: int, body: MagSeatsBody, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    d = (await db.execute(select(ExpressDriver).where(ExpressDriver.user_id == uid))).scalars().first()
    if not d or not (1 <= body.seats <= 12):
        raise HTTPException(status_code=400, detail="Qiymat noto'g'ri")
    d.seats = body.seats
    await db.commit()
    return {"ok": True}


@admin_mag_router.post("/queue/{eid}/remove")
async def admin_mag_remove(eid: int, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    e = (await db.execute(select(MagQueueEntry).where(MagQueueEntry.id == eid, MagQueueEntry.status == "waiting").with_for_update())).scalars().first()
    if not e:
        raise HTTPException(status_code=404, detail="Topilmadi")
    await _mag_release(db, e, "removed", "Administrator navbatdan chiqardi")
    await db.commit()
    return {"ok": True}



app.include_router(ai_admin_router)
app.include_router(banner_req_admin_router)
app.include_router(cancel_router)
app.include_router(parcel_router)
app.include_router(courier_parcel_router)
app.include_router(admin_parcel_router)
app.include_router(express_router)
app.include_router(track_router)
app.include_router(terminal_router)
app.include_router(driver_mag_router)
app.include_router(mag_client_router)
app.include_router(admin_mag_router)
app.include_router(courier_express_router)
app.include_router(driver_express_router)
app.include_router(admin_express_router)
app.include_router(support_admin_router)
app.include_router(partner_router)
app.include_router(shop_router)
app.include_router(app_api_router)
app.include_router(applications_router)
app.include_router(finance_router)

@app.get("/", response_class=HTMLResponse)
@app.get("/app", response_class=HTMLResponse)
async def app_home(request: Request):
    """ZooMo asosiy kirish sahifasi: yuklanish animatsiyasi, bosh sahifa va
    ro'yxatdan o'tish (mijoz / kuryer / hamkor)."""
    return templates.TemplateResponse(request=request, name="app.html", context={})