"""ZooMo qo'shimcha modullar: SHAXSIY BUYUM YUBORISH (parcel) va TAXI.

Asosiy buyurtma/pul oqimiga TEGMAYDI — alohida jadvallar va alohida router.
Egasi (OWNER) har birini bitta tugma bilan yoqadi/o'chiradi (FeatureFlag).
O'chiq bo'lsa barcha mijoz/haydovchi endpointlari 403 qaytaradi va ilovada tugma ko'rinmaydi.
"""
import json
import math
import random
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from database import Base, get_db
from models import User, UserRole, CourierProfile, SystemSetting, Transaction, TransactionType
from auth import get_current_admin_user, require_owner, get_current_courier_user, is_scoped_to_city


# ============================ JADVALLAR ============================
class FeatureFlag(Base):
    __tablename__ = "feature_flags"
    key = Column(String, primary_key=True)          # "parcel" | "taxi"
    enabled = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by_id = Column(Integer, nullable=True)


class ExtraSetting(Base):
    __tablename__ = "extra_settings"
    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)


class ParcelOrder(Base):
    __tablename__ = "parcel_orders"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("users.id"), index=True)
    courier_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    city_id = Column(Integer, nullable=True)
    status = Column(String, default="new", index=True)   # new|accepted|picked|delivered|cancelled
    pickup_address = Column(String, nullable=False)
    pickup_lat = Column(Float, nullable=True)
    pickup_lng = Column(Float, nullable=True)
    drop_address = Column(String, nullable=False)
    drop_lat = Column(Float, nullable=True)
    drop_lng = Column(Float, nullable=True)
    sender_phone = Column(String, nullable=True)
    receiver_name = Column(String, nullable=False)
    receiver_phone = Column(String, nullable=False)
    item_desc = Column(String, nullable=False)
    weight_kg = Column(Float, default=1.0)
    size = Column(String, default="small")
    comment = Column(String, nullable=True)
    distance_km = Column(Float, nullable=True)
    price = Column(Float, nullable=False)                # mijoz to'laydigan summa (naqd, kuryerga)
    platform_share = Column(Float, default=0.0)          # platforma ulushi (kuryer balansidan hisoblanadi)
    delivery_code = Column(String, nullable=True)        # qabul qiluvchi kuryerga aytadigan 4 xonali kod
    financials_applied = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    accepted_at = Column(DateTime, nullable=True)
    picked_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)


class TaxiDriver(Base):
    __tablename__ = "taxi_drivers"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, index=True)
    car_model = Column(String, nullable=False)
    car_plate = Column(String, nullable=False)
    car_color = Column(String, nullable=True)
    status = Column(String, default="pending")           # pending|approved|blocked
    is_online = Column(Boolean, default=False)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    seen_at = Column(DateTime, nullable=True)
    balance = Column(Float, default=0.0)                 # oldindan to'ldirilgan hisob (komissiya shundan ushlanadi)
    created_at = Column(DateTime, default=datetime.utcnow)


class TaxiRide(Base):
    __tablename__ = "taxi_rides"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("users.id"), index=True)
    driver_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    city_id = Column(Integer, nullable=True)
    status = Column(String, default="searching", index=True)  # searching|accepted|arrived|started|completed|cancelled
    pickup_address = Column(String, nullable=False)
    pickup_lat = Column(Float, nullable=False)
    pickup_lng = Column(Float, nullable=False)
    drop_address = Column(String, nullable=True)
    drop_lat = Column(Float, nullable=True)
    drop_lng = Column(Float, nullable=True)
    passengers = Column(Integer, default=1)
    client_phone = Column(String, nullable=True)
    comment = Column(String, nullable=True)
    distance_km = Column(Float, nullable=True)
    price = Column(Float, nullable=False)
    commission = Column(Float, default=0.0)
    financials_applied = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    accepted_at = Column(DateTime, nullable=True)
    arrived_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)


# ============================ SOZLAMALAR ============================
FLAGS = ("parcel", "taxi")
DEFAULTS = {
    # Posilka
    "parcel_base_price": 12000.0, "parcel_per_km": 2500.0, "parcel_free_kg": 5.0,
    "parcel_per_extra_kg": 1000.0, "parcel_max_kg": 30.0, "parcel_min_price": 12000.0,
    "parcel_courier_share": 80.0,
    # Taxi
    "taxi_base_price": 6000.0, "taxi_per_km": 2200.0, "taxi_min_price": 8000.0,
    "taxi_commission_percent": 12.0, "taxi_search_radius_km": 7.0,
    "taxi_free_wait_min": 15.0,
}
PROHIBITED_TEXT = ("Taqiqlangan: giyohvand/psixotrop moddalar, qurol va o'q-dorilar, portlovchi va yonuvchi moddalar, "
                   "jonli hayvonlar, pul va qimmatli qog'ozlar, qonun bilan taqiqlangan har qanday narsa.")


async def flag_on(db: AsyncSession, key: str) -> bool:
    row = (await db.execute(select(FeatureFlag).where(FeatureFlag.key == key))).scalars().first()
    return bool(row and row.enabled)


async def need_flag(db: AsyncSession, key: str):
    if not await flag_on(db, key):
        raise HTTPException(status_code=403, detail="Bu xizmat hozircha yoqilmagan")


async def get_cfg(db: AsyncSession) -> dict:
    cfg = dict(DEFAULTS)
    for r in (await db.execute(select(ExtraSetting))).scalars().all():
        if r.key in DEFAULTS:
            try:
                v = float(r.value)
                if math.isfinite(v) and v >= 0:
                    cfg[r.key] = v
            except (TypeError, ValueError):
                pass
    return cfg


def _round500(x: float) -> float:
    return float(int(round(x / 500.0)) * 500)


def _km(a_lat, a_lng, b_lat, b_lng) -> float:
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = math.radians(b_lat - a_lat), math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


async def _road_factor(db: AsyncSession) -> float:
    s = (await db.execute(select(SystemSetting))).scalars().first()
    f = float(getattr(s, "km_road_factor", None) or 1.3)
    return f if 1.0 <= f <= 3.0 else 1.3


def parcel_price(cfg: dict, km: float, weight: float) -> float:
    p = cfg["parcel_base_price"] + cfg["parcel_per_km"] * max(0.0, km)
    if weight > cfg["parcel_free_kg"]:
        p += cfg["parcel_per_extra_kg"] * (weight - cfg["parcel_free_kg"])
    return _round500(max(p, cfg["parcel_min_price"]))


def taxi_price(cfg: dict, km: float) -> float:
    p = cfg["taxi_base_price"] + cfg["taxi_per_km"] * max(0.0, km)
    return _round500(max(p, cfg["taxi_min_price"]))


# ============================ SO'ROV MODELLARI ============================
Lat = Field(..., ge=-90, le=90)
Lng = Field(..., ge=-180, le=180)


class ParcelQuoteBody(BaseModel):
    init_data: Optional[str] = None
    pickup_lat: float = Lat
    pickup_lng: float = Lng
    drop_lat: float = Lat
    drop_lng: float = Lng
    weight_kg: float = Field(1.0, gt=0, le=1000)


class ParcelCreateBody(ParcelQuoteBody):
    pickup_address: str = Field(..., min_length=3, max_length=300)
    drop_address: str = Field(..., min_length=3, max_length=300)
    receiver_name: str = Field(..., min_length=2, max_length=100)
    receiver_phone: str = Field(..., min_length=7, max_length=20)
    sender_phone: Optional[str] = Field(None, max_length=20)
    item_desc: str = Field(..., min_length=2, max_length=300)
    size: str = Field("small", max_length=10)
    comment: Optional[str] = Field(None, max_length=300)
    confirm_allowed: bool = False       # "taqiqlangan narsa emas" tasdig'i


class InitBody(BaseModel):
    init_data: Optional[str] = None


class IdBody(BaseModel):
    init_data: Optional[str] = None
    id: int


class TaxiQuoteBody(BaseModel):
    init_data: Optional[str] = None
    pickup_lat: float = Lat
    pickup_lng: float = Lng
    drop_lat: Optional[float] = Field(None, ge=-90, le=90)
    drop_lng: Optional[float] = Field(None, ge=-180, le=180)


class TaxiCreateBody(TaxiQuoteBody):
    pickup_address: str = Field(..., min_length=2, max_length=300)
    drop_address: Optional[str] = Field(None, max_length=300)
    passengers: int = Field(1, ge=1, le=8)
    client_phone: Optional[str] = Field(None, max_length=20)
    comment: Optional[str] = Field(None, max_length=300)


class DriverApplyBody(BaseModel):
    car_model: str = Field(..., min_length=2, max_length=60)
    car_plate: str = Field(..., min_length=3, max_length=15)
    car_color: Optional[str] = Field(None, max_length=30)


class LocBody(BaseModel):
    lat: float = Lat
    lng: float = Lng


class CodeBody(BaseModel):
    code: str = Field(..., min_length=4, max_length=4)


class FlagBody(BaseModel):
    key: str
    enabled: bool


class CfgBody(BaseModel):
    values: dict


class BalanceBody(BaseModel):
    amount: float = Field(..., ge=-100000000, le=100000000)
    note: Optional[str] = Field(None, max_length=200)


# ============================ YORDAMCHILAR ============================
_deps = {}   # main.py dan setup() orqali beriladi


def setup(app, get_ident, ident_cond, send_tg):
    _deps["ident"] = get_ident
    _deps["cond"] = ident_cond
    _deps["tg"] = send_tg
    app.include_router(public_router)
    app.include_router(client_router)
    app.include_router(courier_parcel_router)
    app.include_router(taxi_driver_router)
    app.include_router(extras_admin_router)


async def _client(db: AsyncSession, init_data: Optional[str]) -> User:
    if not init_data:
        raise HTTPException(status_code=403, detail="Avval tizimga kiring")
    ident = _deps["ident"](init_data)
    u = (await db.execute(select(User).where(_deps["cond"](ident)))).scalars().first()
    if not u or not u.is_active:
        raise HTTPException(status_code=403, detail="Foydalanuvchi topilmadi")
    return u


async def _tg(db: AsyncSession, user_id: Optional[int], text: str):
    try:
        if not user_id:
            return
        u = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
        if u and u.telegram_id:
            await _deps["tg"](u.telegram_id, text)
    except Exception:
        pass


def _p(o: ParcelOrder, with_code: bool = False) -> dict:
    d = {"id": o.id, "status": o.status, "pickup_address": o.pickup_address, "drop_address": o.drop_address,
         "receiver_name": o.receiver_name, "receiver_phone": o.receiver_phone, "item_desc": o.item_desc,
         "weight_kg": o.weight_kg, "price": o.price, "distance_km": o.distance_km, "comment": o.comment,
         "pickup_lat": o.pickup_lat, "pickup_lng": o.pickup_lng, "drop_lat": o.drop_lat, "drop_lng": o.drop_lng,
         "created_at": o.created_at.isoformat() if o.created_at else None}
    if with_code:
        d["delivery_code"] = o.delivery_code
    return d


def _r(o: TaxiRide) -> dict:
    return {"id": o.id, "status": o.status, "pickup_address": o.pickup_address, "drop_address": o.drop_address,
            "pickup_lat": o.pickup_lat, "pickup_lng": o.pickup_lng, "drop_lat": o.drop_lat, "drop_lng": o.drop_lng,
            "price": o.price, "distance_km": o.distance_km, "passengers": o.passengers, "comment": o.comment,
            "created_at": o.created_at.isoformat() if o.created_at else None}


# ============================ OMMAVIY ============================
public_router = APIRouter(tags=["Qo'shimcha xizmatlar"])


@public_router.get("/api/extras/flags")
async def extras_flags(db: AsyncSession = Depends(get_db)):
    cfg = await get_cfg(db)
    return {"parcel": await flag_on(db, "parcel"), "taxi": await flag_on(db, "taxi"),
            "prohibited": PROHIBITED_TEXT,
            "parcel_max_kg": cfg["parcel_max_kg"],
            "taxi_base": cfg["taxi_base_price"], "taxi_per_km": cfg["taxi_per_km"]}


# ============================ MIJOZ: POSILKA ============================
client_router = APIRouter(tags=["Mijoz: posilka va taxi"])


async def _quote_parcel(db, b: ParcelQuoteBody):
    cfg = await get_cfg(db)
    if b.weight_kg > cfg["parcel_max_kg"]:
        raise HTTPException(status_code=400, detail=f"Og'irlik {cfg['parcel_max_kg']:g} kg dan oshmasin")
    km = _km(b.pickup_lat, b.pickup_lng, b.drop_lat, b.drop_lng) * await _road_factor(db)
    return cfg, round(km, 2), parcel_price(cfg, km, b.weight_kg)


@client_router.post("/api/parcel/quote")
async def parcel_quote(b: ParcelQuoteBody, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "parcel")
    cfg, km, price = await _quote_parcel(db, b)
    return {"distance_km": km, "price": price}


@client_router.post("/api/parcel/create")
async def parcel_create(b: ParcelCreateBody, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "parcel")
    u = await _client(db, b.init_data)
    if not b.confirm_allowed:
        raise HTTPException(status_code=400, detail="Taqiqlangan narsa yubormasligingizni tasdiqlang")
    if b.size not in ("small", "medium", "large"):
        raise HTTPException(status_code=400, detail="O'lcham noto'g'ri")
    busy = (await db.execute(select(func.count(ParcelOrder.id)).where(
        ParcelOrder.client_id == u.id, ParcelOrder.status.in_(("new", "accepted", "picked"))))).scalar() or 0
    if busy >= 5:
        raise HTTPException(status_code=400, detail="Faol jo'natmalar ko'p — avval mavjudlarini yakunlang")
    cfg, km, price = await _quote_parcel(db, b)
    share = _round500(price * (100.0 - cfg["parcel_courier_share"]) / 100.0)
    o = ParcelOrder(client_id=u.id, city_id=u.city_id, status="new",
                    pickup_address=b.pickup_address.strip(), pickup_lat=b.pickup_lat, pickup_lng=b.pickup_lng,
                    drop_address=b.drop_address.strip(), drop_lat=b.drop_lat, drop_lng=b.drop_lng,
                    sender_phone=(b.sender_phone or u.phone_number), receiver_name=b.receiver_name.strip(),
                    receiver_phone=b.receiver_phone.strip(), item_desc=b.item_desc.strip(), weight_kg=b.weight_kg,
                    size=b.size, comment=(b.comment or "").strip() or None, distance_km=km, price=price,
                    platform_share=share, delivery_code=f"{random.randint(0, 9999):04d}")
    db.add(o)
    await db.commit()
    await db.refresh(o)
    return {"ok": True, "order": _p(o, True)}


@client_router.post("/api/parcel/my")
async def parcel_my(b: InitBody, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "parcel")
    u = await _client(db, b.init_data)
    rows = (await db.execute(select(ParcelOrder).where(ParcelOrder.client_id == u.id)
                             .order_by(ParcelOrder.id.desc()).limit(30))).scalars().all()
    out = []
    for o in rows:
        d = _p(o, o.status in ("new", "accepted", "picked"))
        if o.courier_id and o.status in ("accepted", "picked"):
            c = (await db.execute(select(User).where(User.id == o.courier_id))).scalars().first()
            if c:
                d["courier"] = {"name": c.full_name, "phone": c.phone_number}
        out.append(d)
    return {"items": out}


@client_router.post("/api/parcel/cancel")
async def parcel_cancel(b: IdBody, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "parcel")
    u = await _client(db, b.init_data)
    o = (await db.execute(select(ParcelOrder).where(ParcelOrder.id == b.id, ParcelOrder.client_id == u.id)
                          .with_for_update())).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Jo'natma topilmadi")
    if o.status not in ("new", "accepted"):
        raise HTTPException(status_code=400, detail="Buyum olib ketilgan yoki yakunlangan — bekor qilib bo'lmaydi")
    cid = o.courier_id
    o.status = "cancelled"
    o.cancelled_at = datetime.utcnow()
    await db.commit()
    await _tg(db, cid, f"❌ Jo'natma #{o.id} mijoz tomonidan bekor qilindi.")
    return {"ok": True}


# ============================ MIJOZ: TAXI ============================
async def _quote_taxi(db, b: TaxiQuoteBody):
    cfg = await get_cfg(db)
    km = None
    if b.drop_lat is not None and b.drop_lng is not None:
        km = round(_km(b.pickup_lat, b.pickup_lng, b.drop_lat, b.drop_lng) * await _road_factor(db), 2)
    price = taxi_price(cfg, km if km is not None else 0.0)
    return cfg, km, price


@client_router.post("/api/taxi/quote")
async def taxi_quote(b: TaxiQuoteBody, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    cfg, km, price = await _quote_taxi(db, b)
    return {"distance_km": km, "price": price, "exact": km is not None}


@client_router.post("/api/taxi/create")
async def taxi_create(b: TaxiCreateBody, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    u = await _client(db, b.init_data)
    active = (await db.execute(select(func.count(TaxiRide.id)).where(
        TaxiRide.client_id == u.id, TaxiRide.status.in_(("searching", "accepted", "arrived", "started"))))).scalar() or 0
    if active:
        raise HTTPException(status_code=400, detail="Sizda faol taxi buyurtmasi bor")
    cfg, km, price = await _quote_taxi(db, b)
    r = TaxiRide(client_id=u.id, city_id=u.city_id, pickup_address=b.pickup_address.strip(),
                 pickup_lat=b.pickup_lat, pickup_lng=b.pickup_lng,
                 drop_address=(b.drop_address or "").strip() or None, drop_lat=b.drop_lat, drop_lng=b.drop_lng,
                 passengers=b.passengers, client_phone=(b.client_phone or u.phone_number),
                 comment=(b.comment or "").strip() or None, distance_km=km, price=price,
                 commission=_round500(price * cfg["taxi_commission_percent"] / 100.0))
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return {"ok": True, "ride": _r(r)}


@client_router.post("/api/taxi/my")
async def taxi_my(b: InitBody, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    u = await _client(db, b.init_data)
    r = (await db.execute(select(TaxiRide).where(TaxiRide.client_id == u.id)
                          .order_by(TaxiRide.id.desc()).limit(1))).scalars().first()
    if not r:
        return {"ride": None}
    d = _r(r)
    if r.driver_id and r.status in ("accepted", "arrived", "started"):
        c = (await db.execute(select(User).where(User.id == r.driver_id))).scalars().first()
        dr = (await db.execute(select(TaxiDriver).where(TaxiDriver.user_id == r.driver_id))).scalars().first()
        if c and dr:
            d["driver"] = {"name": c.full_name, "phone": c.phone_number, "car": f"{dr.car_model} {dr.car_color or ''}".strip(),
                           "plate": dr.car_plate, "lat": dr.lat, "lng": dr.lng}
    return {"ride": d}


@client_router.post("/api/taxi/cancel")
async def taxi_cancel(b: IdBody, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    u = await _client(db, b.init_data)
    r = (await db.execute(select(TaxiRide).where(TaxiRide.id == b.id, TaxiRide.client_id == u.id)
                          .with_for_update())).scalars().first()
    if not r:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    if r.status not in ("searching", "accepted", "arrived"):
        raise HTTPException(status_code=400, detail="Safar boshlangan yoki yakunlangan — bekor qilib bo'lmaydi")
    did = r.driver_id
    r.status = "cancelled"
    r.cancelled_at = datetime.utcnow()
    await db.commit()
    await _tg(db, did, f"❌ Taxi buyurtma #{r.id} mijoz tomonidan bekor qilindi.")
    return {"ok": True}


# ============================ KURYER: POSILKA ============================
courier_parcel_router = APIRouter(prefix="/courier/parcels", tags=["Kuryer: posilka"])


async def _courier_profile(db, user: User) -> CourierProfile:
    return (await db.execute(select(CourierProfile).where(CourierProfile.user_id == user.id))).scalars().first()


@courier_parcel_router.get("/flag")
async def cp_flag(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    return {"enabled": await flag_on(db, "parcel")}


@courier_parcel_router.get("/available")
async def cp_available(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    await need_flag(db, "parcel")
    q = select(ParcelOrder).where(ParcelOrder.status == "new")
    if user.city_id is not None:
        q = q.where(ParcelOrder.city_id == user.city_id)
    rows = (await db.execute(q.order_by(ParcelOrder.id).limit(30))).scalars().all()
    return {"items": [_p(o) for o in rows]}


@courier_parcel_router.get("/mine")
async def cp_mine(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    await need_flag(db, "parcel")
    rows = (await db.execute(select(ParcelOrder).where(
        ParcelOrder.courier_id == user.id, ParcelOrder.status.in_(("accepted", "picked")))
        .order_by(ParcelOrder.id))).scalars().all()
    return {"items": [_p(o) for o in rows]}


@courier_parcel_router.post("/{pid}/accept")
async def cp_accept(pid: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    await need_flag(db, "parcel")
    cp = await _courier_profile(db, user)
    if cp.is_blocked or not cp.is_online:
        raise HTTPException(status_code=400, detail="Avval onlayn bo'ling (hisobingiz bloklanmagan bo'lishi kerak)")
    o = (await db.execute(select(ParcelOrder).where(ParcelOrder.id == pid).with_for_update())).scalars().first()
    if not o or o.status != "new" or o.courier_id is not None:
        raise HTTPException(status_code=409, detail="Bu jo'natma allaqachon olingan yoki bekor qilingan")
    if user.city_id is not None and o.city_id is not None and o.city_id != user.city_id:
        raise HTTPException(status_code=403, detail="Boshqa shahar jo'natmasi")
    if (cp.balance or 0.0) < (o.platform_share or 0.0):
        raise HTTPException(status_code=400, detail="Hisobingizda mablag' yetarli emas — balansni to'ldiring")
    mine = (await db.execute(select(func.count(ParcelOrder.id)).where(
        ParcelOrder.courier_id == user.id, ParcelOrder.status.in_(("accepted", "picked"))))).scalar() or 0
    if mine >= 3:
        raise HTTPException(status_code=400, detail="Bir vaqtda 3 tadan ortiq jo'natma olib bo'lmaydi")
    o.courier_id = user.id
    o.status = "accepted"
    o.accepted_at = datetime.utcnow()
    await db.commit()
    await _tg(db, o.client_id, f"📦 Jo'natma #{o.id}: kuryer {user.full_name} ({user.phone_number}) qabul qildi.")
    return {"ok": True, "order": _p(o)}


async def _my_parcel(db, pid, user, statuses) -> ParcelOrder:
    o = (await db.execute(select(ParcelOrder).where(ParcelOrder.id == pid, ParcelOrder.courier_id == user.id)
                          .with_for_update())).scalars().first()
    if not o:
        raise HTTPException(status_code=404, detail="Jo'natma topilmadi")
    if o.status not in statuses:
        raise HTTPException(status_code=400, detail="Holat mos emas")
    return o


@courier_parcel_router.post("/{pid}/picked")
async def cp_picked(pid: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_courier_user)):
    await need_flag(db, "parcel")
    o = await _my_parcel(db, pid, user, ("accepted",))
    o.status = "picked"
    o.picked_at = datetime.utcnow()
    await db.commit()
    await _tg(db, o.client_id, f"📦 Jo'natma #{o.id} kuryerda — yo'lda. Qabul qiluvchi kuryerga kodni ayting: {o.delivery_code}")
    return {"ok": True}


@courier_parcel_router.post("/{pid}/delivered")
async def cp_delivered(pid: int, body: CodeBody, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_courier_user)):
    await need_flag(db, "parcel")
    o = await _my_parcel(db, pid, user, ("picked",))
    if (body.code or "").strip() != (o.delivery_code or ""):
        raise HTTPException(status_code=400, detail="Tasdiq kodi noto'g'ri — qabul qiluvchidan so'rang")
    o.status = "delivered"
    o.delivered_at = datetime.utcnow()
    if not o.financials_applied:
        o.financials_applied = True
        cp = (await db.execute(select(CourierProfile).where(CourierProfile.user_id == user.id).with_for_update())).scalars().first()
        if cp and (o.platform_share or 0) > 0:
            cp.balance = (cp.balance or 0.0) - o.platform_share
            db.add(Transaction(user_id=user.id, type=TransactionType.ORDER_FEE, amount=-o.platform_share,
                               note=f"Posilka #{o.id}: platforma ulushi", created_by_id=user.id))
    await db.commit()
    await _tg(db, o.client_id, f"✅ Jo'natma #{o.id} topshirildi. Rahmat!")
    return {"ok": True}


# ============================ TAXI: HAYDOVCHI ============================
taxi_driver_router = APIRouter(prefix="/taxi/driver", tags=["Taxi haydovchi"])


async def _session_user(request: Request, db: AsyncSession) -> User:
    uid = request.session.get("user_id")
    u = (await db.execute(select(User).where(User.id == uid))).scalars().first() if uid else None
    if not u or not u.is_active:
        raise HTTPException(status_code=401, detail="Avval tizimga kiring")
    return u


async def _driver(request: Request, db: AsyncSession, need_approved: bool = True, lock: bool = False):
    u = await _session_user(request, db)
    q = select(TaxiDriver).where(TaxiDriver.user_id == u.id)
    if lock:
        q = q.with_for_update()
    d = (await db.execute(q)).scalars().first()
    if not d:
        raise HTTPException(status_code=404, detail="Siz haydovchi sifatida ro'yxatdan o'tmagansiz")
    if need_approved and d.status != "approved":
        raise HTTPException(status_code=403, detail="Haydovchi arizangiz hali tasdiqlanmagan yoki bloklangan")
    return u, d


@taxi_driver_router.get("/page", response_class=HTMLResponse)
async def td_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not request.session.get("user_id"):
        return RedirectResponse(url="/login", status_code=303)
    if not await flag_on(db, "taxi"):
        return HTMLResponse("<meta name=viewport content='width=device-width'><body style='font-family:sans-serif;padding:20px'>Taxi xizmati hozircha yoqilmagan. <a href='/courier'>Orqaga</a></body>", status_code=200)
    import os
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "taxi_driver.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@taxi_driver_router.post("/apply")
async def td_apply(b: DriverApplyBody, request: Request, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    u = await _session_user(request, db)
    ex = (await db.execute(select(TaxiDriver).where(TaxiDriver.user_id == u.id))).scalars().first()
    if ex:
        raise HTTPException(status_code=400, detail="Ariza allaqachon yuborilgan")
    db.add(TaxiDriver(user_id=u.id, car_model=b.car_model.strip(), car_plate=b.car_plate.strip().upper(),
                      car_color=(b.car_color or "").strip() or None, status="pending"))
    await db.commit()
    return {"ok": True, "message": "Ariza yuborildi — admin tasdiqlagach ishlay olasiz"}


@taxi_driver_router.get("/me")
async def td_me(request: Request, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    u = await _session_user(request, db)
    d = (await db.execute(select(TaxiDriver).where(TaxiDriver.user_id == u.id))).scalars().first()
    if not d:
        return {"registered": False}
    return {"registered": True, "status": d.status, "is_online": d.is_online, "balance": d.balance,
            "car": f"{d.car_model} {d.car_color or ''}".strip(), "plate": d.car_plate}


@taxi_driver_router.post("/online")
async def td_online(body: FlagBody, request: Request, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    u, d = await _driver(request, db)
    d.is_online = bool(body.enabled)
    d.seen_at = datetime.utcnow()
    await db.commit()
    return {"ok": True, "is_online": d.is_online}


@taxi_driver_router.post("/location")
async def td_location(b: LocBody, request: Request, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    u, d = await _driver(request, db)
    d.lat, d.lng, d.seen_at = b.lat, b.lng, datetime.utcnow()
    await db.commit()
    return {"ok": True}


@taxi_driver_router.get("/available")
async def td_available(request: Request, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    u, d = await _driver(request, db)
    if not d.is_online or d.lat is None:
        return {"items": [], "note": "Onlayn bo'ling va joylashuvni yoqing"}
    cfg = await get_cfg(db)
    q = select(TaxiRide).where(TaxiRide.status == "searching", TaxiRide.created_at > datetime.utcnow() - timedelta(minutes=30))
    if u.city_id is not None:
        q = q.where(TaxiRide.city_id == u.city_id)
    out = []
    for r in (await db.execute(q.order_by(TaxiRide.id).limit(50))).scalars().all():
        dist = _km(d.lat, d.lng, r.pickup_lat, r.pickup_lng)
        if dist <= cfg["taxi_search_radius_km"]:
            x = _r(r)
            x["pickup_dist_km"] = round(dist, 1)
            x["commission"] = r.commission
            out.append(x)
    out.sort(key=lambda x: x["pickup_dist_km"])
    return {"items": out}


@taxi_driver_router.post("/{rid}/accept")
async def td_accept(rid: int, request: Request, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    u, d = await _driver(request, db, lock=True)
    if not d.is_online:
        raise HTTPException(status_code=400, detail="Avval onlayn bo'ling")
    busy = (await db.execute(select(func.count(TaxiRide.id)).where(
        TaxiRide.driver_id == u.id, TaxiRide.status.in_(("accepted", "arrived", "started"))))).scalar() or 0
    if busy:
        raise HTTPException(status_code=400, detail="Avval joriy safarni yakunlang")
    r = (await db.execute(select(TaxiRide).where(TaxiRide.id == rid).with_for_update())).scalars().first()
    if not r or r.status != "searching" or r.driver_id is not None:
        raise HTTPException(status_code=409, detail="Bu buyurtma allaqachon olingan yoki bekor qilingan")
    if r.client_id == u.id:
        raise HTTPException(status_code=400, detail="O'z buyurtmangizni qabul qila olmaysiz")
    if (d.balance or 0.0) < (r.commission or 0.0):
        raise HTTPException(status_code=400, detail="Hisobingizda komissiya uchun mablag' yetarli emas — balansni to'ldiring")
    r.driver_id = u.id
    r.status = "accepted"
    r.accepted_at = datetime.utcnow()
    await db.commit()
    await _tg(db, r.client_id, f"🚕 Taxi #{r.id}: {u.full_name}, {d.car_model} {d.car_color or ''} ({d.car_plate}) yo'lga chiqdi. Tel: {u.phone_number}")
    return {"ok": True, "ride": _r(r)}


@taxi_driver_router.get("/active")
async def td_active(request: Request, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    u, d = await _driver(request, db)
    r = (await db.execute(select(TaxiRide).where(
        TaxiRide.driver_id == u.id, TaxiRide.status.in_(("accepted", "arrived", "started"))))).scalars().first()
    if not r:
        return {"ride": None}
    x = _r(r)
    c = (await db.execute(select(User).where(User.id == r.client_id))).scalars().first()
    x["client_phone"] = r.client_phone or (c.phone_number if c else None)
    x["client_name"] = c.full_name if c else None
    return {"ride": x}


async def _my_ride(request, db, rid, statuses):
    u, d = await _driver(request, db)
    r = (await db.execute(select(TaxiRide).where(TaxiRide.id == rid, TaxiRide.driver_id == u.id)
                          .with_for_update())).scalars().first()
    if not r:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    if r.status not in statuses:
        raise HTTPException(status_code=400, detail="Holat mos emas")
    return u, d, r


@taxi_driver_router.post("/{rid}/arrived")
async def td_arrived(rid: int, request: Request, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    u, d, r = await _my_ride(request, db, rid, ("accepted",))
    r.status, r.arrived_at = "arrived", datetime.utcnow()
    await db.commit()
    await _tg(db, r.client_id, f"🚕 Taxi #{r.id} yetib keldi — {d.car_plate}. Iltimos, chiqing.")
    return {"ok": True}


@taxi_driver_router.post("/{rid}/start")
async def td_start(rid: int, request: Request, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    u, d, r = await _my_ride(request, db, rid, ("arrived", "accepted"))
    r.status, r.started_at = "started", datetime.utcnow()
    await db.commit()
    return {"ok": True}


@taxi_driver_router.post("/{rid}/complete")
async def td_complete(rid: int, request: Request, db: AsyncSession = Depends(get_db)):
    await need_flag(db, "taxi")
    u, d, r = await _my_ride(request, db, rid, ("started",))
    r.status, r.completed_at = "completed", datetime.utcnow()
    if not r.financials_applied:
        r.financials_applied = True
        dd = (await db.execute(select(TaxiDriver).where(TaxiDriver.user_id == u.id).with_for_update())).scalars().first()
        if dd and (r.commission or 0) > 0:
            dd.balance = (dd.balance or 0.0) - r.commission
            db.add(Transaction(user_id=u.id, type=TransactionType.ORDER_FEE, amount=-r.commission,
                               note=f"Taxi #{r.id}: platforma komissiyasi", created_by_id=u.id))
    await db.commit()
    await _tg(db, r.client_id, f"✅ Taxi #{r.id} safari yakunlandi. To'lov: {r.price:,.0f} so'm (naqd). Rahmat!")
    return {"ok": True}


# ============================ ADMIN ============================
extras_admin_router = APIRouter(prefix="/admin/extras", tags=["Admin: qo'shimcha xizmatlar"])


@extras_admin_router.get("/state")
async def ex_state(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_admin_user)):
    cfg = await get_cfg(db)
    return {"flags": {k: await flag_on(db, k) for k in FLAGS}, "config": cfg, "is_owner": user.role == UserRole.OWNER}


@extras_admin_router.post("/flag")
async def ex_flag(b: FlagBody, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    if b.key not in FLAGS:
        raise HTTPException(status_code=400, detail="Noma'lum xizmat")
    row = (await db.execute(select(FeatureFlag).where(FeatureFlag.key == b.key))).scalars().first()
    if not row:
        row = FeatureFlag(key=b.key)
        db.add(row)
    row.enabled = bool(b.enabled)
    row.updated_at = datetime.utcnow()
    row.updated_by_id = owner.id
    await db.commit()
    return {"ok": True, "key": b.key, "enabled": row.enabled}


@extras_admin_router.post("/config")
async def ex_config(b: CfgBody, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    for k, v in (b.values or {}).items():
        if k not in DEFAULTS:
            raise HTTPException(status_code=400, detail=f"Noma'lum sozlama: {k}")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{k}: son kiriting")
        if not math.isfinite(f) or f < 0 or f > 100000000:
            raise HTTPException(status_code=400, detail=f"{k}: qiymat noto'g'ri")
        if k in ("parcel_courier_share", "taxi_commission_percent") and f > 100:
            raise HTTPException(status_code=400, detail=f"{k}: 0–100 oralig'ida bo'lsin")
        row = (await db.execute(select(ExtraSetting).where(ExtraSetting.key == k))).scalars().first()
        if not row:
            row = ExtraSetting(key=k)
            db.add(row)
        row.value = repr(f)
    await db.commit()
    return {"ok": True, "config": await get_cfg(db)}


@extras_admin_router.get("/parcels")
async def ex_parcels(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_admin_user)):
    q = select(ParcelOrder).order_by(ParcelOrder.id.desc()).limit(100)
    if is_scoped_to_city(user):
        q = q.where(ParcelOrder.city_id == user.city_id)
    return {"items": [_p(o, True) for o in (await db.execute(q)).scalars().all()]}


@extras_admin_router.get("/rides")
async def ex_rides(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_admin_user)):
    q = select(TaxiRide).order_by(TaxiRide.id.desc()).limit(100)
    if is_scoped_to_city(user):
        q = q.where(TaxiRide.city_id == user.city_id)
    return {"items": [_r(o) for o in (await db.execute(q)).scalars().all()]}


@extras_admin_router.get("/drivers")
async def ex_drivers(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_admin_user)):
    rows = (await db.execute(select(TaxiDriver, User).join(User, User.id == TaxiDriver.user_id)
                             .order_by(TaxiDriver.id.desc()).limit(200))).all()
    out = []
    for d, u in rows:
        if is_scoped_to_city(user) and u.city_id != user.city_id:
            continue
        out.append({"id": d.id, "user_id": u.id, "name": u.full_name, "phone": u.phone_number, "status": d.status,
                    "car": f"{d.car_model} {d.car_color or ''}".strip(), "plate": d.car_plate,
                    "is_online": d.is_online, "balance": d.balance})
    return {"items": out}


@extras_admin_router.post("/drivers/{did}/status")
async def ex_driver_status(did: int, body: dict, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_admin_user)):
    st = (body or {}).get("status")
    if st not in ("approved", "blocked", "pending"):
        raise HTTPException(status_code=400, detail="Holat noto'g'ri")
    d = (await db.execute(select(TaxiDriver).where(TaxiDriver.id == did))).scalars().first()
    if not d:
        raise HTTPException(status_code=404, detail="Haydovchi topilmadi")
    if is_scoped_to_city(user):
        du = (await db.execute(select(User).where(User.id == d.user_id))).scalars().first()
        if not du or du.city_id != user.city_id:
            raise HTTPException(status_code=403, detail="Boshqa shahar haydovchisi")
    d.status = st
    if st != "approved":
        d.is_online = False
    await db.commit()
    await _tg(db, d.user_id, "✅ Taxi haydovchi arizangiz tasdiqlandi!" if st == "approved" else "ℹ️ Taxi haydovchi holatingiz o'zgardi: " + st)
    return {"ok": True}


@extras_admin_router.post("/drivers/{did}/balance")
async def ex_driver_balance(did: int, b: BalanceBody, db: AsyncSession = Depends(get_db), owner: User = Depends(require_owner)):
    d = (await db.execute(select(TaxiDriver).where(TaxiDriver.id == did).with_for_update())).scalars().first()
    if not d:
        raise HTTPException(status_code=404, detail="Haydovchi topilmadi")
    d.balance = (d.balance or 0.0) + b.amount
    db.add(Transaction(user_id=d.user_id, type=TransactionType.DEPOSIT if b.amount >= 0 else TransactionType.WITHDRAWAL,
                       amount=b.amount, note=b.note or "Taxi hisobi (admin)", created_by_id=owner.id))
    await db.commit()
    return {"ok": True, "balance": d.balance}
