import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey, DateTime, Date, Enum, Text, LargeBinary
from sqlalchemy.orm import relationship
from database import Base


class UserRole(enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    PARTNER = "partner"
    COURIER = "courier"
    CLIENT = "client"


class OrderStatus(enum.Enum):
    CREATED = "created"
    ACCEPTED_BY_PARTNER = "accepted_by_partner"
    PREPARING = "preparing"
    LOOKING_FOR_COURIER = "looking_for_courier"
    ON_THE_WAY = "on_the_way"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class WeatherCondition(enum.Enum):
    CLEAR = "clear"
    HOT = "hot"
    COLD = "cold"
    RAIN = "rain"
    SNOW = "snow"
    WINDY = "windy"


class WithdrawalStatus(enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TransactionType(enum.Enum):
    DEPOSIT = "deposit"        # balansga pul qo'shish (masalan, naqd pulni "hisobga olish")
    WITHDRAWAL = "withdrawal"  # balansdan pul yechish (masalan, kuryerga naqd to'lab, balansdan ayirish)
    ORDER_FEE = "order_fee"    # buyurtma orqali avtomatik hisoblangan komissiya/haq (kelajakda batafsil auditing uchun)
    REFUND = "refund"          # bekor qilingan/xato buyurtma bo'yicha qaytarilgan pul


class City(Base):
    __tablename__ = "cities"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True)  # masalan: "Uchquduq", "Zarafshon"
    is_active = Column(Boolean, default=True)

    partners = relationship("PartnerProfile", back_populates="city")
    operators = relationship("User", back_populates="city")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(String, unique=True, index=True, nullable=True)
    full_name = Column(String, nullable=False)
    phone_number = Column(String, unique=True, nullable=False)
    role = Column(Enum(UserRole), default=UserRole.CLIENT)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Faqat OWNER/ADMIN (operator) rollari uchun ishlatiladi — mijoz/kuryer/hamkorda bo'sh qoladi
    password_hash = Column(String, nullable=True)

    # Operator (ADMIN) uchun: qaysi shaharga biriktirilgan. OWNER uchun NULL — cheklovsiz.
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=True)
    city = relationship("City", back_populates="operators")

    # Faqat mijoz (CLIENT) uchun ishlatiladi — tug'ilgan kun bonusi tizimi uchun.
    # Mijoz o'z kabinetida (keyingi bosqichda) kiritadi, hozircha bo'sh qoladi.
    birth_date = Column(Date, nullable=True)

    # Faqat mijoz (CLIENT) uchun — keshbek balli (buyurtmadan qaytadigan
    # foizlar shu yerga to'planadi, keyingi buyurtmada ishlatiladi)
    cashback_balance = Column(Float, default=0.0)

    # Referal tizimi: har bir mijozning o'ziga xos kodi bor (do'stlariga
    # ulashadi), va agar u kimningdir kodi orqali kelgan bo'lsa, o'sha
    # odam shu yerda saqlanadi (bonus faqat BIRINCHI buyurtmada beriladi).
    referral_code = Column(String, unique=True, nullable=True, index=True)
    referred_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    referral_bonus_given = Column(Boolean, default=False)

    # cascade="all, delete-orphan": kuryer/hamkor User o'chirilganda,
    # unga bog'liq profil ham avtomatik o'chadi (aks holda FK xatolik beradi)
    courier_profile = relationship("CourierProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")
    partner_profile = relationship("PartnerProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")


class CourierProfile(Base):
    __tablename__ = "courier_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    passport_data = Column(String, nullable=True)
    transport_type = Column(String, default="walking")
    is_approved = Column(Boolean, default=False)
    is_online = Column(Boolean, default=False)

    # `balance` — kuryerning NAQD PUL QARZI: mijozlardan naqd yig'ilgan, lekin
    # hali egasiga topshirilmagan summa (musbat = qarz bor). Buyurtma
    # yetkazilganda avtomatik oshadi (qarang apply_cod_delivery_financials),
    # egasiga naqd topshirilganda admin panelidan kamaytiriladi.
    balance = Column(Float, default=0.0)

    # Qarz shu chegaradan oshsa, kuryerga YANGI BUYURTMA berish avtomatik
    # to'xtatiladi (blok tizimi) — qarang courier_accept_order va
    # auto_assign_nearest_courier.
    credit_limit = Column(Float, default=300000.0)
    is_blocked = Column(Boolean, default=False)

    # Kuryer "Pul yechish" so'rovi yuborganda (bu FAQAT egasi kuryerga
    # qarzdor bo'lgan — balance MANFIY bo'lgan holatda mumkin, masalan bonus)
    # so'ralgan summa shu yerga "muzlatiladi" — admin tasdiqlagunga qadar
    # ikki marta so'ralib (double-spend) ketmasligi uchun.
    frozen_balance = Column(Float, default=0.0)

    terms_accepted_at = Column(DateTime, nullable=True)

    # Kuryerning JONLI joylashuvi — brauzer/Telegram Mini App'dagi Geolocation
    # API orqali muntazam (har 15-20 sekundda) yuboriladi. Shu orqali: (1)
    # mijoz buyurtmasini xaritada jonli kuzatadi, (2) admin/operator
    # panelida barcha kuryerlar xaritada ko'rinadi, (3) yangi buyurtma
    # kelganda ENG YAQIN online kuryerga avtomatik belgilash (auto-assign)
    # uchun masofa hisoblanadi.
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    location_updated_at = Column(DateTime, nullable=True)

    # Yangi buyurtma kelganda kuryer kabinetida qaysi signal ovozi chalinishi —
    # hamkor profilidagi notification_sound bilan bir xil mantiq: 3 ta
    # tayyor variantdan biri, frontend Web Audio API orqali generatsiya qiladi.
    notification_sound = Column(String, default="chime1")

    user = relationship("User", back_populates="courier_profile")


class PartnerProfile(Base):
    __tablename__ = "partner_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=True)
    brand_name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    address = Column(String, nullable=False)
    is_open = Column(Boolean, default=True)

    # `balance` — egasi hamkorga QARZDOR bo'lgan summa (buyurtmalardan
    # tushgan, komissiya chegirilgan sof daromad) — hamkor buni "Pul yechish"
    # orqali so'rab oladi.
    balance = Column(Float, default=0.0)

    # Pul yechish so'rovi yuborilganda summa shu yerga "muzlatiladi" —
    # admin tasdiqlagunga/rad etgunga qadar balansdan allaqachon chiqarilgan,
    # lekin hali "yakunlanmagan" holatda turadi (race-condition himoyasi).
    frozen_balance = Column(Float, default=0.0)

    commission_rate = Column(Float, default=10.0)
    opening_time = Column(String, default="09:00")
    closing_time = Column(String, default="23:00")

    # Minimal buyurtma summasi — bundan kam summaga buyurtma qabul qilinmaydi
    min_order_amount = Column(Float, default=0.0)

    # Do'konning xaritadagi joylashuvi — hamkor o'zi xaritadan belgilaydi
    # (yoki brauzer orqali avtomatik aniqlanadi). Admin/operator va
    # kuryer/mijoz xaritalarida "🏪" belgisi shu koordinata bo'yicha chiziladi.
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    terms_accepted_at = Column(DateTime, nullable=True)

    # Yangi buyurtma kelganda hamkor kabinetida qaysi signal ovozi
    # chalinishi — 3 ta tayyor variantdan biri (frontend Web Audio API
    # orqali generatsiya qiladi, fayl saqlash shart emas)
    notification_sound = Column(String, default="chime1")

    user = relationship("User", back_populates="partner_profile")
    city = relationship("City", back_populates="partners")
    products = relationship("Product", back_populates="partner", cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="partner")


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    partner_id = Column(Integer, ForeignKey("partner_profiles.id"))
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    price = Column(Float, nullable=False)
    is_available = Column(Boolean, default=True)

    # Rasm va kategoriya — mijoz ilovasida menyu chiroyli va tartibli
    # ko'rinishi uchun (masalan "Ichimliklar", "Pitsalar")
    # DIQQAT: rasm ENDI serverning vaqtinchalik diskiga emas, to'g'ridan-to'g'ri
    # shu yerga (bazaga) saqlanadi — sabab: Render kabi hosting'larda server
    # qayta ishga tushganda (deploy, "uyquga ketish" va h.k.) diskka yozilgan
    # fayllar YO'QOLIB QOLADI, lekin baza doim saqlanadi. image_url endi
    # haqiqiy fayl yo'li emas, balki "/media/product/{id}" kabi rasmni
    # bazadan o'qib beradigan manzilni ko'rsatadi.
    image_data = Column(LargeBinary, nullable=True)
    image_mime = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
    category = Column(String, nullable=True, default="Boshqa")

    partner = relationship("PartnerProfile", back_populates="products")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("users.id"))
    partner_id = Column(Integer, ForeignKey("partner_profiles.id"), nullable=True)
    courier_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    status = Column(Enum(OrderStatus), default=OrderStatus.CREATED)
    total_price = Column(Float, nullable=False)
    delivery_fee = Column(Float, nullable=False)
    delivery_address = Column(String, nullable=False)

    # Mijozning yetkazib berish nuqtasining ANIQ GPS koordinatasi — Mini App
    # ichida buyurtma berayotganda brauzer/Telegram Geolocation orqali
    # avtomatik olinadi (agar mijoz ruxsat bergan bo'lsa). Faqat matn manzil
    # (delivery_address) emas, aynan shu koordinata orqali: (1) admin/operator
    # xaritasida buyurtma nuqtasi ko'rinadi, (2) kuryer o'z kabinetida
    # do'kon->mijoz yo'nalishini xaritada ko'radi. Mijoz koordinata bermagan
    # bo'lsa — NULL qoladi, xarita shunchaki bu buyurtmani ko'rsatmaydi.
    delivery_latitude = Column(Float, nullable=True)
    delivery_longitude = Column(Float, nullable=True)

    # ---- P2P (KARTADAN-KARTAGA) TO'LOV TEKSHIRUVI ----
    # payment_method == "p2p" bo'lgan buyurtmalar uchun: mijoz pulni
    # OWNER'ning shaxsiy kartasiga o'zi o'tkazadi, keyin chek rasmini
    # botga yuboradi. Operator/OWNER botda "Tasdiqlash" bosgunga qadar
    # bu buyurtma hamkorga KO'RINMAYDI (qarang partner_dashboard'dagi
    # filtr) — aks holda hali to'lanmagan buyurtma tayyorlanib qolishi
    # mumkin edi. Naqd/boshqa usullar uchun bu maydon ahamiyatsiz —
    # shuning uchun standart qiymati True.
    payment_verified = Column(Boolean, default=True)
    payment_receipt_file_id = Column(String, nullable=True)
    payment_verified_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    payment_verified_at = Column(DateTime, nullable=True)

    # Mijozning maxsus istaklari ("Piyozsiz", "Achchiq bo'lmasin" va h.k.)
    client_comment = Column(Text, nullable=True)

    # "delivery" — yetkazib berish (odatiy), "dine_in" — do'konning o'zida ovqatlanish
    order_type = Column(String, nullable=False, default="delivery")

    # "cash" (naqd, hozircha yagona to'liq ishlaydigani), "click", "payme"
    # — oxirgi ikkitasi hozircha faqat "mijoz shuni tanladi" deb yoziladi,
    # haqiqiy to'lov o'tkazish (merchant integratsiyasi) hali ulanmagan.
    payment_method = Column(String, nullable=False, default="cash")

    # Ishlatilgan promo-kod va u orqali qancha chegirma qilingani
    promo_code_id = Column(Integer, ForeignKey("promo_codes.id"), nullable=True)
    discount_amount = Column(Float, default=0.0)

    # Shu buyurtmadan mijozga qancha keshbek qaytarilgani (ballarga qo'shildi)
    cashback_earned = Column(Float, default=0.0)
    # Shu buyurtmada mijoz oldingi keshbek ballaridan qancha ishlatgani
    cashback_used = Column(Float, default=0.0)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Buyurtma tarkibidagi mahsulotlar (nechta lavash, nechta kola va h.k.)
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

    # Mijoz va kuryerning ismi/telefonini qulay ko'rsatish uchun (admin/hamkor
    # panellarida "kimga qo'ng'iroq qilish kerak" degan savolga javob beradi)
    client = relationship("User", foreign_keys=[client_id])
    courier = relationship("User", foreign_keys=[courier_id])
    partner = relationship("PartnerProfile", back_populates="orders")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    # product_id nullable=True: agar mahsulot keyinchalik o'chirilsa ham,
    # buyurtma tarixi (order_item) saqlanib qoladi — faqat bog'lanish uziladi
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)

    # DIQQAT: nom va narx shu yerda "suratga olinadi" (snapshot).
    # Sabab: agar ertaga admin mahsulot narxini o'zgartirsa yoki nomini
    # tahrirlasa, ESKI buyurtmalar o'sha vaqtdagi haqiqiy narx/nomni
    # ko'rsatishi kerak — hozirgi narxni emas. Aks holda hisobotlar
    # (masalan, "shu oy qancha sotildi") noto'g'ri chiqib qoladi.
    product_name = Column(String, nullable=False)
    unit_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False, default=1)

    order = relationship("Order", back_populates="items")
    product = relationship("Product")


class SystemSetting(Base):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, index=True)
    base_delivery_fee = Column(Float, default=10000.0)
    service_commission_percent = Column(Float, default=10.0)
    weather_condition = Column(Enum(WeatherCondition), default=WeatherCondition.CLEAR)
    weather_multiplier = Column(Float, default=1.0)
    auto_weather_pricing = Column(Boolean, default=True)

    # Yetkazish narxining necha foizi kuryerga tegishli ekani (qolgani egasiga qoladi).
    # Masalan 80.0 = yetkazish narxining 80%i kuryerga, 20%i egasiga.
    courier_share_percent = Column(Float, default=80.0)

    # Kuryer/hamkor o'z kabinetidan ko'pi bilan nechta plastik karta
    # qo'sha olishi — OWNER bu yerdan belgilaydi (qarang _add_card_for_user).
    # OWNER/operator'ning o'z (P2P) kartalariga bu cheklov TEGMAYDI.
    max_cards_per_courier = Column(Integer, default=3)
    max_cards_per_partner = Column(Integer, default=3)

    # Tug'ilgan kun bonusi, referal va cashback dasturlari — bularning
    # barchasini faqat OWNER qo'lda kiritadi/o'zgartiradi.
    birthday_bonus_amount = Column(Float, default=0.0)
    referral_program_text = Column(Text, nullable=True)
    # Referal orqali kelgan yangi mijoz BIRINCHI buyurtmasini bergach,
    # ikkalasiga ham (taklif qilgan va taklif qilingan) shuncha keshbek beriladi.
    referral_bonus_amount = Column(Float, default=0.0)
    bonus_cashback_text = Column(Text, nullable=True)

    # Cashback dasturining HAQIQIY hisob-kitob qoidasi — matndan (yuqoridagi
    # bonus_cashback_text) farqli, bu ANIQ SON, avtomatik hisoblash uchun.
    # Masalan 2.0 = har bir yetkazilgan buyurtmaning 2%i mijozning cashback
    # balansiga qo'shiladi.
    cashback_earn_percent = Column(Float, default=0.0)

    # Referal/Bonus dasturini mijozga ko'rsatish yoki yashirish — matn tayyor
    # bo'lmasa yoki dastur vaqtincha to'xtatilgan bo'lsa, buni yoqmasdan turib
    # yashirish uchun (matnni o'chirmasdan).
    referral_visible = Column(Boolean, default=True)
    cashback_visible = Column(Boolean, default=True)

    # Har bir rol uchun alohida shartlar — odam shu rolni tanlaganda
    # birinchi bo'lib shu matn ko'rsatiladi, "Roziman" bosmasa davom etolmaydi.
    courier_terms = Column(Text, nullable=True)
    partner_terms = Column(Text, nullable=True)
    client_terms = Column(Text, nullable=True)

    # Admin panelidagi qaysi bo'limlarni OPERATOR (ADMIN roli) ko'rMASLIGI
    # kerakligi — OWNER o'zi tanlaydi. JSON massiv sifatida saqlanadi,
    # masalan: '["moliya", "operatorlar"]'. Bo'sh/NULL bo'lsa — operator
    # OWNER-only bo'lmagan barcha bo'limlarni ko'radi (standart holat).
    operator_hidden_sections = Column(Text, nullable=True)


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)

    # Tranzaksiya yoki kuryerga (user_id), yoki hamkorga (partner_id) tegishli bo'ladi —
    # ikkalasi bir vaqtda to'lmaydi, faqat bittasi ishlatiladi.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    partner_id = Column(Integer, ForeignKey("partner_profiles.id"), nullable=True)

    type = Column(Enum(TransactionType), nullable=False)
    amount = Column(Float, nullable=False)
    note = Column(String, nullable=True)

    # Kim amalga oshirganini bilish uchun (hisobot va shaffoflik uchun muhim —
    # kim, qachon, kimning balansiga qo'l tekkizganini keyin tekshirish mumkin bo'lishi kerak)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])
    partner = relationship("PartnerProfile", foreign_keys=[partner_id])
    created_by = relationship("User", foreign_keys=[created_by_id])


class WithdrawalRequest(Base):
    """Kuryer yoki hamkorning 'pulimni bermoqchiman/yechib olmoqchiman'
    so'rovi. Hozircha haqiqiy bank/karta o'tkazmasi AVTOMATIK emas —
    OWNER buni ko'rib, real hayotda (Click/Payme orqali) pulni jismonan
    o'tkazadi, keyin shu yerda 'Tasdiqlash' bosadi."""
    __tablename__ = "withdrawal_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    partner_id = Column(Integer, ForeignKey("partner_profiles.id"), nullable=True)

    amount = Column(Float, nullable=False)
    status = Column(Enum(WithdrawalStatus), default=WithdrawalStatus.PENDING)

    # Pul qaysi kartaga o'tkazilishi kerak — P2P o'tkazma uchun OWNER'ga
    # to'liq (deshifrlangan) raqam ko'rinishi kerak, qarang card_security.py
    card_id = Column(Integer, ForeignKey("cards.id"), nullable=True)

    requested_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)
    # DIQQAT: nomi tarixiy sabablarga ko'ra `processed_by_id`, lekin bu
    # har doim bir ADMIN/OPERATOR akkauntiga (users.id) ishora qiladi —
    # ya'ni aslida "processed_by_admin_id" bilan bir xil narsa.
    processed_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    note = Column(String, nullable=True)
    # Rad etish sababi — faqat status=REJECTED bo'lganda to'ldiriladi,
    # foydalanuvchiga ko'rsatiladi (shaffoflik uchun).
    reject_reason = Column(String, nullable=True)

    # ---- BOT ORQALI CHEK YUBORISH / QABUL TASDIG'I ----
    # Admin/operator pulni real o'tkazgach, chek rasmini botga yuboradi —
    # shu rasm shu yerga (Telegram file_id sifatida) saqlanadi va
    # kuryer/hamkorga "✅ Pulni oldim" tugmasi bilan birga yuboriladi.
    receipt_file_id = Column(String, nullable=True)
    receipt_sent_at = Column(DateTime, nullable=True)
    # Qabul qiluvchi (kuryer/hamkor) botda "✅ Pulni oldim" bosgan payt —
    # BU MOLIYAVIY HISOBGA TA'SIR QILMAYDI (balans allaqachon approve
    # paytida yakunlangan), faqat shaffoflik/audit uchun yoziladi.
    recipient_confirmed_at = Column(DateTime, nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    partner = relationship("PartnerProfile", foreign_keys=[partner_id])
    card = relationship("Card")
    processed_by = relationship("User", foreign_keys=[processed_by_id])


class Card(Base):
    """Kuryer/hamkorning pul yechish uchun qo'shgan plastik kartasi.

    XAVFSIZLIK: `encrypted_card_number` HECH QACHON ochiq raqam emas —
    qarang card_security.py (Fernet shifrlash). Bu yerda faqat shifrlangan
    matn saqlanadi; ekranga chiqarishda maskalanadi, faqat OWNER/operator
    P2P o'tkazma paytida to'liq (deshifrlangan) holda ko'radi."""
    __tablename__ = "cards"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    encrypted_card_number = Column(Text, nullable=False)
    card_holder_name = Column(String, nullable=False)
    expire_month = Column(Integer, nullable=False)
    expire_year = Column(Integer, nullable=False)  # to'rt xonali, masalan 2027

    # BIN (birinchi 6 raqam) orqali avtomatik aniqlanadi — qarang
    # card_security.detect_card_bin(). Har safar qayta hisoblash shart
    # bo'lmasligi uchun bazada ham saqlanadi.
    bank_name = Column(String, nullable=True)

    # Faqat OWNER'ning shaxsiy kartalari uchun ma'noga ega: mijozlar P2P
    # to'lov qilganda aynan SHU karta raqami ko'rsatiladi. Bir vaqtning
    # o'zida faqat BITTA karta faol bo'ladi (yangisini faollashtirsangiz,
    # eskisi avtomatik o'chadi — qarang _activate_p2p_card).
    is_p2p_active = Column(Boolean, default=False)
    card_type = Column(String, nullable=True)  # uzcard / humo / visa / mastercard / mir / unknown

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")


class OperatorPermission(Base):
    """OWNER tomonidan boshqariladigan, admin panelidagi har bir bo'lim
    (data-page) operatorga (ADMIN roli) ko'rinadimi-yo'qmi degan global
    kalit-qiymat ro'yxati. `section_key` — admin.html'dagi data-page bilan
    bir xil (masalan 'moliya', 'operatorlar', 'banner'...).

    DIQQAT: bu PER-OPERATOR emas, BARCHA operatorlar uchun BIR XIL
    (foydalanuvchining so'zi bilan "barcha qismlarda operatorga ko'rinishi/
    ko'rinmasligini admin o'zi hal qiladi"). Jadvalda qator bo'lmagan
    bo'lim — standart (xavfsiz) qiymatni oladi, qarang
    DEFAULT_OPERATOR_SECTION_VISIBILITY (main.py).
    """
    __tablename__ = "operator_permissions"

    section_key = Column(String, primary_key=True)
    enabled = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Banner(Base):
    """Mini App tepasidagi reklama bannerlari — bir nechtasi bo'lishi
    mumkin (masalan, aylanadigan/slayder ko'rinishida), har biri rasmli
    yoki faqat matnli bo'lishi mumkin. Faqat OWNER boshqaradi."""
    __tablename__ = "banners"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=True)
    text_content = Column(Text, nullable=True)  # faqat matnli banner uchun (rasmsiz)

    # Rasm ham bazaning o'ziga saqlanadi (disk emas — Render'da fayllar
    # qayta ishga tushganda yo'qolib qolardi, qarang: Product.image_data)
    image_data = Column(LargeBinary, nullable=True)
    image_mime = Column(String, nullable=True)

    link_url = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    display_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class PromoCode(Base):
    """Chegirma kodlari — mijoz buyurtma berayotganda kiritadi."""
    __tablename__ = "promo_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, nullable=False, unique=True, index=True)
    discount_percent = Column(Float, nullable=True)  # masalan 10.0 = 10%
    discount_amount = Column(Float, nullable=True)  # yoki qat'iy summa (so'mda)
    max_uses = Column(Integer, nullable=True)  # bo'sh = cheksiz
    used_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PromoCodeUsage(Base):
    """Kim, qachon, qaysi buyurtmada qaysi promo-koddan foydalangani —
    bitta mijoz bitta kodni bir necha marta ishlatolmasligini nazorat
    qilish uchun ham kerak."""
    __tablename__ = "promo_code_usages"

    id = Column(Integer, primary_key=True, index=True)
    promo_code_id = Column(Integer, ForeignKey("promo_codes.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    used_at = Column(DateTime, default=datetime.utcnow)

    promo_code = relationship("PromoCode")


class FavoriteProduct(Base):
    """Mijozning 'sevimli' deb belgilagan mahsulotlari."""
    __tablename__ = "favorite_products"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    product = relationship("Product")