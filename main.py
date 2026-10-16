import re
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
from sqlalchemy import select, func, text, extract, or_, and_, inspect as sa_inspect
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
    FavoriteProduct,
    Card,
    OperatorPermission,
    PaymentReceipt,
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
import extras   # posilka + taxi (alohida jadvallar; FeatureFlag bilan yoqiladi)

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
    print("DIQQAT: SESSION_SECRET_KEY .env'da yo'q — vaqtinchalik tasodifiy kalit ishlatilyapti.")
    import secrets as _secrets
    SESSION_SECRET_KEY = _secrets.token_hex(32)

_https_env = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").startswith("https://")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY, same_site="lax", https_only=_https_env)


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
    "/api/parcel/", "/api/taxi/",   # initData/token bilan tasdiqlanadi (cookie emas)
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
    "xizmatlar": "🧩 Taxi va posilka (haydovchi tasdiqlash, jo'natmalar)",
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
    "xizmatlar": True,
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

    if not (can_admin or can_partner or can_courier):
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
    if any(_rate_limited(k, limit=(8 if k.startswith("lp:") else 30)) for k in _lg_keys):
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
    available = [can_admin, can_partner, can_courier].count(True)

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
        if setting.cashback_earn_percent and setting.cashback_earn_percent > 0:
            cashback_earned = order.total_price * (setting.cashback_earn_percent / 100)
            client.cashback_balance = (client.cashback_balance or 0) + cashback_earned
            order.cashback_earned = cashback_earned

        # Referal bonusi — faqat BIRINCHI marta, faqat kimdir taklif qilgan bo'lsa
        if client.referred_by_id and client.referred_by_id != client.id and not client.referral_bonus_given and (setting.referral_bonus_amount or 0) > 0:
            delivered_count_result = await db.execute(
                select(func.count(Order.id)).where(Order.client_id == client.id, Order.status == OrderStatus.DELIVERED)
            )
            delivered_count = delivered_count_result.scalar() or 0
            if delivered_count <= 1:  # aynan shu buyurtma — birinchisi
                referrer_result = await db.execute(select(User).where(User.id == client.referred_by_id))
                referrer = referrer_result.scalars().first()
                if referrer:
                    bonus = setting.referral_bonus_amount
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
    return sum(_courier_cash_hold(o, courier_pct) for o in res.scalars().all())


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


@app.post("/admin/banners/create")
async def create_banner(
    title: str = Form(""),
    text_content: str = Form(""),
    link_url: str = Form(""),
    display_order: int = Form(0),
    banner_image: Optional[UploadFile] = File(None),
    bg_color: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_section("banner")),
):
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


@app.post("/admin/banners/{banner_id}/color")
async def banner_set_color(banner_id: int, bg_color: str = Form(""), db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("banner"))):
    banner = (await db.execute(select(Banner).where(Banner.id == banner_id))).scalars().first()
    if not banner:
        raise HTTPException(status_code=404, detail="Banner topilmadi")
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

    if courier_id:
        await _validate_assignable_courier(db, courier_id)
        order.courier_id = courier_id
        if not order.courier_pickup_bonus:
            await _apply_pickup_bonus(db, order, courier_id)

    if order.status == OrderStatus.DELIVERED and old_status != OrderStatus.DELIVERED:
        _assert_can_deliver(order)
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
    commission_rate: float = Form(10.0, allow_inf_nan=False),
    opening_time: str = Form("09:00"),
    closing_time: str = Form("23:00"),
    min_order_amount: float = Form(0.0, allow_inf_nan=False),
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
            category=body.category.strip()[:80], address=body.address.strip()[:300],
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
                "bg_color": b.bg_color,
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
    out = [{"id": p.id, "name": p.brand_name, "category": p.category, "address": p.address,
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
            "ai_led_on": _ai_flags(await _get_or_create_setting(db))["led"],
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
    if latitude is not None and longitude is not None:
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
    ("health", "Shamollash/og'riq uchun iliq va foydali taomlar",
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
    lat: Optional[float] = None
    lng: Optional[float] = None


@shop_router.get("/ai/status")
async def shop_ai_status(db: AsyncSession = Depends(get_db)):
    setting = await _get_or_create_setting(db)
    await db.commit()
    fl = _ai_flags(setting)
    return {"enabled": fl["client"], "help": fl["help"]}


@shop_router.post("/ai/assist")
async def shop_ai_assist(body: AIAssistBody, db: AsyncSession = Depends(get_db)):
    """Mijoz matnini (yoki ovozdan matnga aylangan so'zini) tushunib, MIJOZGA ENG YAQIN do'kondan mos mahsulotlarni yig'adi.
    Mijoz do'kon nomini aytsa — faqat o'sha do'kondan. Buyurtma faqat mijoz tasdig'i va to'lovdan keyin yuboriladi."""
    tg_user = _get_telegram_user_or_403(body.init_data)
    await _ai_require(db, "client")
    q = _ai_norm(body.query).strip()[:300]
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Nima xohlayotganingizni yozing")

    client = (await db.execute(select(User).where(_ident_cond(tg_user)))).scalars().first()
    stmt = (
        select(Product, PartnerProfile)
        .join(PartnerProfile, Product.partner_id == PartnerProfile.id)
        .where(Product.is_available == True, PartnerProfile.is_open == True)
    )
    if client and client.city_id:
        stmt = stmt.where(PartnerProfile.city_id == client.city_id)
    rows = (await db.execute(stmt.limit(1200))).all()

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
        bn = _ai_norm(pr.brand_name or "").strip()
        if len(bn) >= 3 and bn in q:
            named[pr.id] = pr.brand_name
    if named:
        rows = [(p, pr) for p, pr in rows if pr.id in named]

    def hay(p):
        return _ai_norm(f"{p.name} {p.description or ''} {p.category or ''}")

    def mk(p, pr, qty, kind, reason):
        d = dist_cache.get(pr.id)
        return {"product_id": p.id, "name": p.name, "price": p.price, "partner_id": pr.id,
                "partner_name": pr.brand_name, "qty": qty, "kind": kind, "reason": reason,
                "image_url": p.image_url, "distance_km": round(d, 1) if d is not None else None}

    tokens = [t for t in re.findall(r"[\w'‘’-]+", q) if t]
    # 1) Aniq nom bo'yicha (miqdor bilan): "uchta lavash va bitta kola"
    wanted = []   # [(stems, qty)]
    for i, tok in enumerate(tokens):
        if len(tok) < 3 or tok in _AI_STOP or tok in _AI_QTY_WORDS or tok.isdigit():
            continue
        if any(tok in _ai_norm(n) for n in named.values()):
            continue  # do'kon nomining o'zi mahsulot emas
        stems = _AI_ALIASES.get(tok, [tok])
        stems = [s[:max(4, len(s) - 2)] if len(s) > 5 else s for s in stems]
        qty = 1
        for j in (i - 1, i - 2, i + 1):
            if 0 <= j < len(tokens):
                t = tokens[j]
                if t in _AI_QTY_WORDS:
                    qty = _AI_QTY_WORDS[t]
                    break
                if t.isdigit() and 0 < int(t) <= 30:
                    qty = int(t)
                    break
        wanted.append((stems, min(qty, 20)))
    # Faqat menyuda bor so'zlarni "buyurtma so'zi" deb olamiz
    cand = {}  # partner_id -> {wanted_index: (product, partner, qty)}
    for wi, (stems, qty) in enumerate(wanted):
        for p, pr in rows:
            if any(s in _ai_norm(p.name) for s in stems):
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
        if not any(any(d in _ai_norm(i["name"]) for d in _AI_DRINK) for i in items):
            drinks = sorted([(p, pr) for p, pr in rows if pr.id == best_pid and any(d in hay(p) for d in _AI_DRINK)],
                            key=lambda x: x[0].price)
            if drinks:
                items.append(mk(drinks[0][0], drinks[0][1], 1, "addon", "Ichimlik ham qo'shilsinmi?"))
        d0 = dist_cache.get(best_pid)
        where = f"«{pr0.brand_name}»" + (f" ({d0:.1f} km)" if d0 is not None else "")
        missing = len(wanted) - len(cand[best_pid])
        summary = (f"{where} do'konidan topdim" if named else f"Sizga eng yaqin: {where}") + \
                  (f". {missing} ta narsa bu do'konda yo'q." if missing > 0 else ".") + " Tasdiqlang."
    else:
        # 2) Niyat bo'yicha (kayfiyat, salomatlik...) — yaqin do'konlar birinchi
        padded = q + " "
        for key, label, triggers, w in _AI_INTENTS:
            if any(t in padded for t in triggers):
                scored = []
                for p, pr in rows:
                    h = hay(p)
                    sc = sum(1 for x in w if x in h)
                    if sc:
                        scored.append((p, pr, sc))
                scored.sort(key=lambda x: (dkey(x[1].id), -x[2], x[0].price))
                seen = {}
                for p, pr, sc in scored:
                    if seen.get(pr.id, 0) >= 2:
                        continue
                    seen[pr.id] = seen.get(pr.id, 0) + 1
                    items.append(mk(p, pr, 1, "idea", label))
                    if len(items) >= 8:
                        break
                summary = label + (" — eng yaqin joylardan" if have_loc else "")
                break
        if not items:
            words = [t for t in tokens if len(t) >= 4 and t not in _AI_STOP]
            scored = []
            for p, pr in rows:
                h = hay(p)
                sc = sum(1 for x in words if x[:max(4, len(x) - 2)] in h)
                if sc:
                    scored.append((p, pr, sc))
            scored.sort(key=lambda x: (-x[2], dkey(x[1].id), x[0].price))
            for p, pr, sc in scored[:8]:
                items.append(mk(p, pr, 1, "idea", "Qidiruv bo'yicha"))
            if not items and named:
                summary = "«" + ", ".join(named.values()) + "» do'konida bunday mahsulot topilmadi. Do'kon nomisiz yozib ko'ring — yaqin joylardan qidiraman."
            else:
              summary = "Qidiruv natijalari" if items else \
                "Mos mahsulot topilmadi. Boshqacha yozib ko'ring (masalan: «uchta lavash va bitta kola» yoki «tomog'im og'riyapti»)."
    return {"enabled": True, "summary": summary, "items": items, "have_location": have_loc,
            "named_partner": bool(named), "needs_payment": True}


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
    ads = (await db.execute(select(LedAd).options(selectinload(LedAd.partner)).where(LedAd.partner_id == partner.id)
                            .order_by(LedAd.id.desc()))).scalars().all()
    return {"ads": [_led_dict(a) for a in ads], "pay_card": await _get_topup_card_info(db)}


@partner_router.post("/ai/led")
async def partner_led_create(body: LedAdBody, db: AsyncSession = Depends(get_db),
                             current_user: User = Depends(get_current_partner_user)):
    """Hamkor reklama SO'ROVI yuboradi. U ekranga chiqmaydi — admin to'lovni tasdiqlab yoqmaguncha."""
    await _ai_require(db, "led")
    partner = await _get_own_partner(db, current_user)
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
async def admin_sup_announce(body: AnnounceBody, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("xabarlar"))):
    """Butun bo'limga (barcha hamkor / kuryer / mijoz) majburiy e'lon: Telegram + ilova ichida «Tushundim» bosilguncha chiqadi."""
    audience = _sup_section(body.audience)
    text = (body.text or "").strip()[:SUP_MAX_LEN]
    if len(text) < 2:
        raise HTTPException(status_code=400, detail="E'lon matnini yozing")
    # Operator — faqat o'z shahriga; egasi — barcha shaharlarga
    scope_city = current_user.city_id if (current_user.role == UserRole.ADMIN and current_user.city_id is not None) else None
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
        aq = aq.where(Announcement.city_id == current_user.city_id)
    rows = (await db.execute(aq.order_by(Announcement.id.desc()).limit(15))).scalars().all()
    acks = dict((await db.execute(select(AnnouncementAck.announcement_id, func.count(AnnouncementAck.id)).where(
        AnnouncementAck.announcement_id.in_([a.id for a in rows] or [0])).group_by(AnnouncementAck.announcement_id))).all())
    return {"items": [{"id": a.id, "text": a.text, "at": a.created_at.isoformat(), "sent": a.sent_count or 0, "seen": acks.get(a.id, 0)} for a in rows]}


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
async def admin_ratings_summary(db: AsyncSession = Depends(get_db), current_user: User = Depends(require_section("xabarlar"))):
    scope = []
    if current_user.role == UserRole.ADMIN and current_user.city_id is not None:
        scope.append(OrderRating.user_id.in_(select(User.id).where(User.city_id == current_user.city_id)))
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
app.include_router(ai_admin_router)
app.include_router(support_admin_router)
app.include_router(partner_router)
app.include_router(shop_router)
app.include_router(app_api_router)
app.include_router(applications_router)
app.include_router(finance_router)
extras.setup(app, _get_telegram_user_or_403, _ident_cond, send_telegram_message)

@app.get("/", response_class=HTMLResponse)
@app.get("/app", response_class=HTMLResponse)
async def app_home(request: Request):
    """ZooMo asosiy kirish sahifasi: yuklanish animatsiyasi, bosh sahifa va
    ro'yxatdan o'tish (mijoz / kuryer / hamkor)."""
    return templates.TemplateResponse(request=request, name="app.html", context={})