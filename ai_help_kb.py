"""ZooMo AI yordamchi — har bir rol (mijoz, kuryer, hamkor, operator/admin) uchun o'z paneli
bo'yicha bilim bazasi. Tashqi xizmatsiz: kalit so'zlar bo'yicha eng mos mavzuni topadi.
Har mavzu: (sarlavha, [kalit so'z bo'laklari], javob matni)."""

def _n(s: str) -> str:
    s = (s or "").lower()
    for ch in ("’", "‘", "ʻ", "ʼ", "`", "´"):
        s = s.replace(ch, "'")
    return s


KB = {
"client": [
 ("Buyurtma berish", ["buyurtma ber", "qanday buyurtma", "zakaz", "olish", "buyurtma qil", "заказ", "order"],
  "1) Shaharni tanlang. 2) Do'kon yoki kafeni oching. 3) Mahsulotlarni «+» bilan savatga qo'shing. 4) Pastdagi savat panelini bosing. 5) Yetkazish manzili yoki joylashuvni kiriting, to'lov usulini tanlang va «Buyurtma berish» ni bosing. Buyurtma holatini «Buyurtmalarim» bo'limidan kuzatasiz."),
 ("ZooMo AI bilan buyurtma", ["ai", "✨", "ovoz", "mikrofon", "yordamchi", "niyat", "aqlli", "aytib", "ayt"],
  "Tepadagi ✨ tugmani bosing. Yozing yoki 🎤 bilan ayting: «uchta lavash va bitta kola», «tomog'im og'riyapti», «charchadim, shirin yegim keldi». AI sizga ENG YAQIN do'kondan topib yig'adi. Do'kon nomini aytsangiz («Evos dan 2 ta lavash») faqat o'sha do'kondan qidiradi. «Tasdiqlayman» bosgach savat to'lovga o'tadi. AI buyurtmasi karta orqali oldindan to'lov bilan ishlaydi — to'lov tasdiqlangach buyurtma do'konga yuboriladi."),
 ("To'lov usullari", ["to'lov", "tolov", "pul", "karta", "p2p", "naqd", "click", "payme", "chek", "оплат", "payment"],
  "Naqd — kuryerga yetkazilganda to'laysiz. Karta orqali (P2P) — ko'rsatilgan kartaga pul o'tkazib, chek rasmini buyurtmadan keyin yuklaysiz; administrator tasdiqlagach buyurtma do'konga boradi. AI orqali yig'ilgan buyurtmada faqat oldindan karta to'lovi bor."),
 ("Yetkazish narxi", ["narx", "yetkazish", "dostavka", "km", "masofa", "qancha", "доставк", "fee"],
  "Yetkazish narxi do'kondan manzilingizgacha bo'lgan masofaga (km) bog'liq: belgilangan radiusgacha bir xil narx, undan keyin har km uchun qo'shimcha. Aniq summani savatda ko'rasiz. Joylashuvni yuborsangiz narx aniq hisoblanadi."),
 ("Joylashuvni yuborish", ["joylashuv", "lokatsiya", "geolok", "manzil", "gps", "location", "адрес"],
  "Savatda «📍 Joylashuvim» tugmasini bosing va brauzer/Telegram so'raganda ruxsat bering. Joylashuv aniqlangach kuryer manzilingizni xaritada ko'radi va narx masofa bo'yicha hisoblanadi. Ruxsat bermasangiz manzilni matn bilan yozing."),
 ("Buyurtmani kuzatish", ["kuzat", "kuryer qayerda", "xarita", "yo'lda", "tracking", "qayerda", "status", "holat"],
  "«Buyurtmalarim» da buyurtmani oching va «Buyurtmani jonli kuzatish» ni bosing. To'liq ekranli xaritada kuryer harakati, masofa va taxminiy vaqt ko'rinadi. Kuryer buyurtmani olgunga qadar do'kon va manzilingiz ko'rsatiladi."),
 ("Kechikish", ["kech", "uzoq", "sekin", "kutish", "ko'p vaqt", "задерж", "late"],
  "Kuryer kutilganidan ko'proq kechiksa, Telegramda uzr xabari keladi. Shoshilinch bo'lsa buyurtma sahifasidagi kuryer raqamiga qo'ng'iroq qiling."),
 ("Promo-kod va keshbek", ["promo", "kod", "chegirma", "keshbek", "cashback", "bonus", "skidka", "referal"],
  "Savatda «Promo-kod» maydoniga kodni yozing — chegirma hisoblanadi. Keshbek ballari avvalgi buyurtmalardan to'planadi va keyingi buyurtmada ishlatiladi. Do'stlaringizga referal kodingizni ulashsangiz bonus olasiz."),
 ("Sevimlilar", ["sevimli", "yurak", "❤", "saqla", "favorite"],
  "Mahsulotdagi ❤️ ni bosing — u sevimlilarga qo'shiladi. Tepadagi yurakcha tugmasi orqali ro'yxatni ochasiz."),
 ("Shaxsiy kabinet", ["kabinet", "profil", "tug'ilgan", "ism", "telefon", "profile", "cabinet"],
  "Tepadagi shaxs belgisi — shaxsiy kabinet: ism, telefon, tug'ilgan kun (bonus uchun), keshbek balansi, referal kodi va buyurtmalar tarixi shu yerda."),
 ("Til va mavzu", ["til", "mavzu", "tungi", "rejim", "dark", "rang", "language", "theme", "ru", "english", "lang"],
  "🌐 tugmasi — tilni tanlaydi (o'zbek, o'zbek kirill, rus, ingliz, qozoq, tojik, qirg'iz). ☀️/🌙 — kunduzgi va tungi rejim, 🎨 — rang mavzusi. Tanlovingiz eslab qolinadi."),
 ("Bekor qilish va muammo", ["bekor", "xato", "muammo", "shikoyat", "operator", "yordam kerak", "bog'lan", "cancel"],
  "Buyurtmani bekor qilish yoki muammo bo'lsa, buyurtma sahifasidagi aloqa raqamlari orqali operator yoki kuryer bilan bog'laning. Hamkor tayyorlashni boshlagan bo'lsa, bekor qilish cheklanishi mumkin."),
],
"courier": [
 ("Ishni boshlash (onlayn)", ["onlayn", "online", "ish boshla", "ishga", "yoqish", "start", "faol", "offlayn"],
  "Yuqoridagi «Ish boshladim» tugmasi bilan onlayn bo'lasiz. Faqat onlayn kuryerlarga yangi buyurtma avtomatik biriktiriladi va admin xaritasida ko'rinasiz. Telefon joylashuviga (GPS) ruxsat bering va sahifani ochiq qoldiring."),
 ("Buyurtma qabul qilish", ["qabul", "buyurtma ol", "yangi buyurtma", "accept", "naqd ushlab", "hold"],
  "«Yangi buyurtmalar» da buyurtma kartasida daromadingiz (ulush + km haqi) ko'rinadi. «Qabul qilish» ni bosing. Naqd buyurtmada platforma ulushi balansingizdan ushlab turiladi — balans yetmasa avval to'ldiring."),
 ("Navigator va yo'l", ["navigator", "yo'l", "marshrut", "yo'nalish", "xarita", "bor", "route", "navigat"],
  "Buyurtma qabul qilgach navigator ochiladi: 1-bosqich — do'konga borish, «Buyurtmani oldim», 2-bosqich — mijozga borish, «Yetkazdim». Tashqi navigatorlar (Google, Yandex, 2GIS) tugmalari ham bor. AI sahifasidagi 🔊 ovozli ko'rsatmani yoqsangiz burilishlarni ovoz bilan aytadi."),
 ("Daromad qanday hisoblanadi", ["daromad", "pul", "haq", "ulush", "qancha", "bonus", "km", "earn", "income"],
  "Daromad = yetkazish narxidagi sizning ulushingiz + mijozgacha bo'lgan masofa uchun km haqi + buyurtmani olishga borish masofasi uchun haq. Qiymatlarni admin belgilaydi; har buyurtma kartasida tafsiloti ko'rinadi."),
 ("Balansni to'ldirish", ["balans", "to'ldir", "karta", "chek", "topup", "hisob", "to'lov"],
  "«Balansni to'ldirish» da ko'rsatilgan admin kartasiga pul o'tkazing va chek rasmini yuklang. Admin tasdiqlagach balans ko'payadi. Naqd buyurtmalarni olish uchun balans yetarli bo'lishi kerak."),
 ("Pul yechish", ["yechish", "yechib", "pul ol", "withdraw", "karta qo'sh", "kartalarim"],
  "Avval «Kartalarim» ga o'z kartangizni qo'shing, so'ng «Pul yechish so'rovi» da summani kiriting. Admin tasdiqlab o'tkazadi. Minimal va maksimal summa sahifada ko'rsatilgan."),
 ("Yetkazdim — tugatish", ["yetkazdim", "tugat", "yakun", "mijoz bergan", "deliver", "naqd pul"],
  "Mijozga yetgach «Yetkazdim» ni bosing. Naqd bo'lsa mijozdan olingan pul sizda qoladi, platforma ulushi balansdan hisoblanadi; daromadingiz tarixda ko'rinadi."),
 ("Muammolar", ["muammo", "xato", "ishlamayapti", "gps", "joylashuv", "topilmayapti", "bug"],
  "GPS ishlamasa — brauzer sozlamasida joylashuvga ruxsat bering va sahifani yangilang. Buyurtma ko'rinmasa — onlayn ekanligingizni va balansingizni tekshiring. Boshqa muammoda operatorga murojaat qiling."),
 ("LED ekran (sumka reklamasi)", ["led", "reklama", "ekran", "sumka"],
  "AI kartasidagi «Sumka LED ekranini ochish» sahifani to'liq ekranda ochadi — yaqin hamkorlar reklamasi navbat bilan chiqadi. Reklamani yoqish/o'chirish va tarkibni faqat admin boshqaradi."),
 ("AI maslahatchi", ["ai", "maslahat", "copilot", "batching", "birlashtir", "yordamchi"],
  "AI kartasi eng foydali va eng yaqin buyurtmani ko'rsatadi, bir yo'nalishdagi buyurtmalarni birlashtirishni maslahat beradi, ob-havo va band soatlar haqida ogohlantiradi, ovozli yo'l ko'rsatma beradi."),
 ("Til va mavzu", ["til", "mavzu", "tungi", "rejim", "signal", "ovoz", "language", "theme"],
  "🌐 — til, ☀️/🌙 — rejim, 🎨 — rang mavzusi. «Bildirishnoma ovozi» bo'limida yangi buyurtma signalini tanlaysiz."),
],
"partner": [
 ("Buyurtmalarni qabul qilish", ["qabul", "buyurtma", "yangi", "tayyorla", "accept", "order", "zakaz"],
  "«Buyurtmalar» bo'limida yangi buyurtma signal bilan keladi. «Qabul qilish» ni bosing — chek avtomatik chop etiladi, keyin «Tayyorlanmoqda» va «Tayyor» holatlariga o'tkazasiz. Tayyor bo'lgach kuryer olib ketadi. Karta (P2P) bilan to'langan buyurtma admin to'lovni tasdiqlagach ko'rinadi."),
 ("Chek printer", ["chek", "printer", "bluetooth", "chop", "print", "airprint", "rawbt"],
  "Sozlamalardagi «Chek printer» bo'limidan printerni ulang: kompyuter/Android — Bluetooth yoki USB, iPhone — Bluefy brauzeri yoki AirPrint («Tizim»). Qabul qilganda chek avtomatik chiqadi."),
 ("Menyu boshqarish", ["menyu", "mahsulot", "taom", "qo'shish", "narx", "rasm", "tahrir", "menu", "product"],
  "«Menyu» bo'limida mahsulot qo'shasiz (nom, narx, kategoriya, rasm), narxni o'zgartirasiz, vaqtincha o'chirib-yoqasiz yoki o'chirasiz. O'chirilgan (yashirilgan) mahsulot mijozga ko'rinmaydi."),
 ("Avto-menyu (AI)", ["tuga", "avto", "yashir", "qolmadi", "ai", "zaxira", "tugab"],
  "AI bo'limidagi «Avto-menyu» ga «Tovuq tugadi» deb yozing yoki 🎤 bilan ayting — tovuqli taomlar menyudan yashiriladi va muqobillar ko'rsatiladi. «Tovuq keldi» desangiz qaytadi."),
 ("Talab bashorati (AI)", ["bashorat", "talab", "kutilmoqda", "tahlil", "forecast", "soat", "ko'p buyurtma"],
  "AI bo'limi oxirgi 8 haftadagi buyurtmalar, hafta kuni va ob-havo asosida bugun nechta buyurtma kutilishi va qaysi soatlar band bo'lishini ko'rsatadi, zaxirani oshirishni maslahat beradi."),
 ("LED reklama joylashtirish", ["led", "reklama", "ekran", "radius", "sumka"],
  "AI bo'limida reklama so'rovi yuboring: sarlavha, matn, rang va radius (kuryer shu masofada o'tganda ko'rsatiladi). Adminga to'lovni amalga oshiring — admin tasdiqlagach reklama kuryer sumkasi LED ekranida AI navbati bilan chiqadi. Tasdiqlangan reklamani faqat admin boshqaradi."),
 ("Balans va pul yechish", ["balans", "pul", "yechish", "yechib", "komissiya", "daromad", "karta", "withdraw"],
  "«Balans» bo'limida komissiya chegirilgan sof daromadingiz ko'rinadi. Kartangizni qo'shing va «Pul yechish» so'rovini yuboring — admin tasdiqlab o'tkazadi. Komissiya foizini faqat egasi belgilaydi."),
 ("Do'kon sozlamalari", ["sozlama", "manzil", "joylashuv", "ish vaqti", "ochiq", "yopiq", "signal", "settings"],
  "«Sozlamalar» da nom, manzil, xaritadagi joylashuv va yangi buyurtma signalini o'zgartirasiz. Ish vaqti, komissiya va shahar faqat admin tomonidan o'zgartiriladi. Do'konni vaqtincha yopish uchun ochiq/yopiq holatidan foydalaning."),
 ("Buyurtma tarixi", ["tarix", "o'tgan", "oldingi", "history"],
  "«Tarix» bo'limida oldingi kunlardagi buyurtmalar saqlanadi."),
 ("Kuryerni kuzatish", ["kuzat", "kuryer", "qayerda", "xarita", "track"],
  "Buyurtma kartasidagi kuzatish tugmasi kuryerning jonli joylashuvini to'liq ekranli xaritada ko'rsatadi."),
 ("Til va mavzu", ["til", "mavzu", "tungi", "rejim", "language", "theme"],
  "🌐 — til, ☀️/🌙 — rejim, 🎨 — rang mavzusi. Panel telefon, planshet va kompyuterga moslashadi."),
],
"admin": [
 ("Bosh sahifa va statistika", ["bosh", "statistika", "dashboard", "hisobot", "bugun", "stat"],
  "«Bosh sahifa» da bugungi buyurtmalar, daromad, faol kuryerlar va do'konlar jonli yangilanadi — sahifani qayta yuklash shart emas. Admin xaritasida kuryerlar, hamkorlar va mijoz manzillari real vaqtda ko'rinadi."),
 ("Buyurtmalar va kuryer biriktirish", ["buyurtma", "kuryer biriktir", "assign", "holat", "bekor", "order"],
  "«Buyurtmalar» da barcha buyurtmalar holati bilan ko'rinadi. Kuryerni qo'lda biriktirish, holatni o'zgartirish yoki bekor qilish mumkin. Avtomatik biriktirish eng yaqin onlayn kuryerni tanlaydi."),
 ("P2P to'lov chekini tasdiqlash", ["p2p", "chek", "to'lov", "tasdiq", "karta", "receipt"],
  "Mijoz karta orqali to'lagan buyurtmaning cheki «Moliya/Cheklar» da (va botda) ko'rinadi. To'lovni tasdiqlasangiz buyurtma hamkorga o'tadi; rad etsangiz bekor bo'ladi. AI orqali yig'ilgan buyurtmalar ham shunday tasdiqlanadi."),
 ("Tizim sozlamalari va km narxlash", ["sozlama", "komissiya", "km", "narx", "radius", "ob-havo", "ulush", "settings"],
  "«Tizim sozlamalari» (faqat egasi): yetkazish narxi, ob-havo koeffitsienti, komissiya, kuryer ulushi, kartalar soni. Km bo'yicha narx: radiusgacha belgilangan narx, keyin har km uchun. Kuryerga alohida: olib borish va yetkazish km haqi."),
 ("Kuryerlar va arizalar", ["kuryer", "ariza", "tasdiq", "bloklash", "balans", "courier"],
  "«Kuryerlar» da kuryerlar, balans, qarz chegarasi, bloklash va arizalarni tasdiqlash. Onlayn kuryerlarni xaritada kuzatasiz."),
 ("Do'konlar (hamkorlar)", ["do'kon", "hamkor", "partner", "komissiya", "menyu", "ariza"],
  "«Do'konlar» da hamkorlarni qo'shish/tahrirlash, komissiya, ish vaqti, shahar va arizalarni tasdiqlash."),
 ("Moliya", ["moliya", "pul yechish", "withdrawal", "balans", "tranzaksiya", "finance", "yechish"],
  "«Moliya» da bugungi/oylik hisobotlar, kuryer va hamkorlarning pul yechish so'rovlari, balans to'ldirish cheklari. So'rovni tasdiqlab kartaga o'tkazasiz."),
 ("Mijozlar, tug'ilgan kun, referal, keshbek", ["mijoz", "tug'ilgan", "referal", "keshbek", "cashback", "bonus", "promo"],
  "Mijozlar ro'yxati, tug'ilgan kun bonusi, referal va keshbek dasturlari hamda promo-kodlar shu bo'limlarda boshqariladi."),
 ("Banner va shartlar", ["banner", "shart", "reklama", "terms"],
  "«Banner» — mijoz ilovasi tepasidagi bannerlar. «Shartlar» — foydalanish shartlari matni."),
 ("Operatorlar", ["operator", "ruxsat", "permission", "huquq"],
  "«Operatorlar» (faqat egasi): operatorlarni qo'shish, shaharga biriktirish va har bir bo'limga ruxsat berish."),
 ("ZooMo AI boshqaruvi", ["ai", "yoqish", "o'chirish", "dispetcher", "led", "yordamchi", "boshqar"],
  "«Tizim sozlamalari → ZooMo AI» da AI ning har bir qismini alohida yoqasiz/o'chirasiz: mijoz, kuryer, hamkor AI, yordamchi, dispetcher (kechikish xabari), LED reklama, AI xabarlari. Kechikishda promo-kod berilishi ham shu yerda yoqiladi."),
 ("AI xabarlari (bildirishnoma)", ["xabar", "bildirishnoma", "kirmagan", "sog'ind", "kampaniya", "yuborish", "notification"],
  "AI xabarlari bo'limida matn yozasiz, kimga (mijoz/kuryer/hamkor) va shartni tanlaysiz — masalan «72 soat kirmaganlarga». Xabar Telegram orqali bildirishnoma bo'lib boradi. Takror yuborish oralig'ini belgilaysiz. «Hozir yuborish» butun auditoriyaga bir martalik jo'natadi."),
 ("LED reklamalar", ["led", "reklama", "to'lov", "tasdiqla", "efir"],
  "Hamkorlar reklama so'raydi. To'lov kelgach «Tasdiqlash»: necha kun va qancha to'langani kiritiladi. AI navbatni tuzadi (yaqin va ko'p to'lagan reklama ko'proq efir oladi). Radius, yoqish/o'chirish, kuryer bo'yicha LED — faqat admin."),
 ("Jonli xarita", ["xarita", "map", "jonli", "to'liq ekran", "kuzatuv"],
  "Admin xaritasida kuryerlar, hamkorlar va mijoz manzillari jonli. «⛶ To'liq ekran» katta ko'rinish beradi, ✕ yoki Esc bilan yopasiz."),
],
}

# ---- Qo'shimcha mavzular (kengaytirilgan qamrov) ----
KB["client"] += [
 ("Parolni / PIN ni unutdim", ["unutdim", "parol", "pin", "kira olmay", "kirolmay", "забыл", "forgot", "login"],
  "Botda «🔑 Ilova uchun PIN» tugmasini bosib yangi 4 xonali PIN o'rnating, so'ng telefon raqamingiz va shu PIN bilan kiring. Raqamingiz Telegram orqali tasdiqlanadi."),
 ("Do'kon yopiq / mahsulot yo'q", ["yopiq", "ochilmayapti", "ko'rinmayapti", "topilmayapti", "tugagan", "mahsulot yo'q", "closed"],
  "Do'kon ish vaqtidan tashqarida yoki vaqtincha yopiq bo'lsa, buyurtma qabul qilinmaydi. Boshqa ochiq do'konni tanlang yoki shaharni tekshiring. Mahsulot ko'rinmasa — u hozircha tugagan bo'lishi mumkin."),
 ("Buyurtma tarixi / qayta buyurtma", ["tarix", "oldingi", "qayta buyurtma", "takror", "history", "reorder"],
  "«Buyurtmalarim» da oldingi buyurtmalar bor. Birini ochib, shu tarkibni qayta savatga qo'shishingiz mumkin."),
 ("Yordam chati va e'lonlar", ["chat", "e'lon", "elon", "xabar", "yozish", "support", "operatorga yoz"],
  "Operator bilan yozishish uchun ilovadagi yordam (chat) bo'limidan foydalaning — javob shu yerda va Telegramda keladi. Yangiliklar va e'lonlar ilovaning e'lonlar bo'limida chiqadi."),
 ("Pul qaytarish", ["qaytar", "qaytarish", "refund", "pulim", "noto'g'ri olib keldi", "buzuq", "sifat"],
  "Buyurtma noto'g'ri yoki sifatsiz kelsa, buyurtma raqami bilan operatorga yozing — holat tekshirilib, kerak bo'lsa qaytarish/kompensatsiya qilinadi."),
]
KB["courier"] += [
 ("Maxfiy javob (2-qadam himoya)", ["maxfiy", "sevimli", "himoya", "secret", "xavfsizlik", "tasdiqlash so'rayapti", "javob"],
  "Pul yechish, karta va rekvizitlar kabi nozik amallar oldidan maxfiy javobingiz so'raladi. Bir marta kiritsangiz, faol ishlaganda 30 daqiqa qayta so'ralmaydi. Javobni unutsangiz — Telegramga kelgan kod orqali o'zgartirasiz."),
 ("Reyting va bonus", ["reyting", "baho", "yulduz", "rating", "bonus", "mukofot"],
  "Mijozlar bergan baholar reytingingizni belgilaydi. Yuqori reyting qo'shimcha bonus beradi; tez yetkazish va xushmuomalalik reytingni oshiradi."),
 ("Bloklangan / qarz", ["blok", "bloklandi", "qarz", "chegara", "ruxsat yo'q", "kira olmay"],
  "Balans qarz chegarasidan oshsa yangi naqd buyurtmalar yopiladi. Balansni to'ldirsangiz qayta ochiladi. Hisob bloklangan bo'lsa operatorga murojaat qiling."),
 ("Yuridik rekvizit / o'zini band qilgan", ["yuridik", "pinfl", "o'zini band", "self", "rekvizit", "stir", "yatt"],
  "«Yuridik» bo'limida o'zini band qilgan shaxs (PINFL) yoki yuridik shaxs rekvizitlarini kiritasiz. Ular sukut bo'yicha yashirin — 👁 ni bosib maxfiy javob bilan ochasiz."),
 ("Yordam chati", ["chat", "operator", "yozish", "support", "e'lon", "elon"],
  "Yordam bo'limida operatorga yozing — javob shu yerda va Telegramda keladi. Umumiy e'lonlar ham shu yerda chiqadi."),
]
KB["partner"] += [
 ("Yuridik manzil / o'zini band qilgan", ["yuridik", "yuridik manzil", "pinfl", "o'zini band", "self", "rekvizit", "stir", "direktor", "yatt"],
  "«Sozlamalar → Yuridik» da o'zini band qilgan shaxs (PINFL) yoki tashkilot (STIR, direktor) ma'lumotini kiritasiz. Ko'rinishi sukut bo'yicha yashirin: 👁 ni bosib maxfiy javobingiz bilan ochasiz — faqat egasi ko'ra oladi."),
 ("Maxfiy javob (himoya)", ["maxfiy", "sevimli", "himoya", "secret", "javob", "xavfsizlik"],
  "Pul yechish, karta, yuridik ma'lumot va sozlamalar o'zgartirilganda maxfiy javob so'raladi. Bir marta kiritsangiz 30 daqiqa (faol ishlaganda uzayadi) qayta so'ralmaydi. Unutilsa — Telegram kodi bilan tiklanadi."),
 ("Reyting va sharhlar", ["reyting", "baho", "sharh", "yulduz", "rating", "review"],
  "Mijozlar bergan baholar do'koningiz reytingini belgilaydi. Reyting yuqori bo'lsa do'kon ro'yxatda oldinroq chiqadi. Tez tayyorlash va sifat reytingni oshiradi."),
 ("Do'konni yopish / tanaffus", ["yopish", "tanaffus", "dam", "vaqtincha", "ochiq emas", "pauza", "close"],
  "Sozlamalarda do'konni vaqtincha yopiq qiling — yangi buyurtma kelmaydi. Ish vaqti jadvalini admin o'rnatadi; o'zgartirish uchun operatorga yozing."),
 ("Yordam chati", ["chat", "operator", "yozish", "support", "e'lon", "elon"],
  "Yordam bo'limida operatorga yozing — javob shu yerda va Telegramda keladi. Umumiy e'lonlar ham shu yerda chiqadi."),
]
KB["admin"] += [
 ("Yordam chati va e'lonlar", ["chat", "e'lon", "elon", "yordam chat", "support", "xabarlar", "javob ber"],
  "«Xabarlar» bo'limida mijoz, kuryer va hamkorlarning murojaatlari ko'rinadi — javob yozsangiz u foydalanuvchi panelida va Telegramda chiqadi. E'lon: auditoriyani (hamma/mijoz/kuryer/hamkor) tanlab yuboriladi. Operator o'z shahri doirasida ishlaydi."),
 ("Maxfiy javob va PIN", ["maxfiy", "pin", "sevimli", "himoya", "secret", "qayta so'ray", "parol"],
  "Operator panelga kirishda maxfiy javob so'raladi va u sessiya davomida saqlanadi. Pul yechish/karta kabi amallarda 30 daqiqa qayta so'ralmaydi. Parol/PIN ni almashtirish: Operatorlar bo'limi."),
 ("Operator ruxsatlari", ["ruxsat", "ko'rinmayapti", "bo'lim", "ochilmayapti", "permission", "huquq", "shahar"],
  "Operatorga ko'rinadigan bo'limlarni egasi «Operatorlar» da belgilaydi. Bo'lim ko'rinmasa — ruxsat berilmagan yoki operator shahriga biriktirilmagan. Egasi tegishli katakni yoqsin."),
 ("O'z-o'zini band qilgan hamkor/kuryer", ["pinfl", "o'zini band", "yuridik", "stir", "rekvizit", "self"],
  "Hamkor va kuryer yuridik ma'lumotlarini o'zi kiritadi (o'zini band qilgan — PINFL, tashkilot — STIR). Ma'lumotlar faqat egasiga ko'rinadi va maxfiy javob bilan ochiladi."),
 ("Xavfsizlik tavsiyalari", ["xavfsiz", "kuchli parol", "zaxira", "backup", "env", "kalit", "secret key"],
  "Kuchli maxfiy javoblar va noyob parollardan foydalaning, SESSION_SECRET_KEY va CARD_ENCRYPTION_KEY ni .env da saqlang, bazadan muntazam zaxira nusxa oling va .env ni hech kimga bermang."),
]

KB["client"] += [
 ("Taxi chaqirish", ["taxi", "taksi", "mashina", "haydovchi", "такси"],
  "Pastdagi chap «🚕 Taxi» tugmasini bosing (admin yoqqan bo'lsa). Xaritada A (qayerdan) va ixtiyoriy B (qayerga) ni belgilang — narx darhol ko'rinadi. «Taxi chaqirish» ni bosing; haydovchi qabul qilgach ismi, mashinasi va raqami chiqadi. To'lov — haydovchiga naqd."),
 ("Shaxsiy buyum yuborish", ["jo'natma", "jonatma", "posilka", "buyum yubor", "yuborish", "kuryer orqali", "посылка"],
  "«📦 Jo'natma» tugmasi: xaritada olish (A) va yetkazish (B) nuqtalarini belgilang, qabul qiluvchi ismi/telefoni va nima yuborayotganingizni yozing, taqiqlangan narsa emasligini tasdiqlang. Kuryer olib boradi. Qabul qiluvchi kuryerga 4 xonali kodni aytganda jo'natma topshirilgan hisoblanadi. To'lov — kuryerga naqd."),
]
KB["courier"] += [
 ("Shaxsiy jo'natmalar", ["jo'natma", "jonatma", "posilka", "buyum", "kod", "topshir"],
  "«📦 Jo'natmalar» tugmasida yangi jo'natmalar ro'yxati. Qabul qiling → «Buyumni oldim» → qabul qiluvchidan 4 xonali kodni so'rab «Topshirdim» ga kiriting. Naqd pulni mijozdan olasiz; platforma ulushi balansingizdan hisoblanadi."),
 ("Taxi haydovchi bo'lish", ["taxi", "taksi", "haydovchi", "mashina"],
  "«🚕 Taxi haydovchi» sahifasida mashina ma'lumotini kiritib ariza bering. Admin tasdiqlagach onlayn bo'lib buyurtma qabul qilasiz. Komissiya balansdan ushlanadi — balans yetarli bo'lishi kerak."),
]
KB["admin"] += [
 ("Taxi va posilka (yoqish)", ["taxi", "taksi", "posilka", "jo'natma", "yoqish", "xizmat", "haydovchi"],
  "«🧩 Taxi va posilka» bo'limida (egasi) ikkita tugma: posilka va taxi — bosilganda xizmat ilovada paydo bo'ladi/yo'qoladi. Shu yerda narxlar, komissiya, haydovchi arizalarini tasdiqlash va balansini to'ldirish, so'nggi jo'natma va safarlar ro'yxati bor."),
]

OVERVIEW_WORDS = ("qo'llanma", "qollanma", "hammasi", "to'liq", "toliq", "nima qila", "nimalar", "yordam", "help", "guide", "boshla", "tushuntir", "start")
TITLE = {"client": "Mijoz paneli", "courier": "Kuryer paneli", "partner": "Hamkor paneli", "admin": "Admin/operator paneli"}


def topics(role: str):
    return [t[0] for t in KB.get(role, [])]


def answer(role: str, q: str) -> dict:
    items = KB.get(role) or KB["client"]
    qn = _n(q).strip()
    if not qn:
        return {"title": TITLE.get(role, ""), "text": overview(role), "topics": topics(role), "matched": False}
    best, best_sc = None, 0
    for title, keys, text in items:
        sc = 0
        for k in keys:
            if k in qn:
                sc += 2 if len(k) >= 4 else 1
        if _n(title) in qn:
            sc += 3
        if sc > best_sc:
            best, best_sc = (title, text), sc
    if best_sc == 0 and any(w in qn for w in OVERVIEW_WORDS):
        return {"title": TITLE.get(role, ""), "text": overview(role), "topics": topics(role), "matched": True}
    if best is None or best_sc == 0:
        return {"title": "", "text": "Bu savolga aniq javob topa olmadim. Quyidagi mavzulardan birini tanlang yoki savolni boshqacha yozing. Murakkab muammo bo'lsa operatorga murojaat qiling.",
                "topics": topics(role), "matched": False}
    return {"title": best[0], "text": best[1], "topics": [t for t in topics(role) if t != best[0]], "matched": True}


def overview(role: str) -> str:
    items = KB.get(role) or []
    lines = [f"{TITLE.get(role, 'Panel')} bo'yicha asosiy mavzular:"]
    for title, _k, text in items:
        first = text.split(". ")[0].rstrip(".")
        lines.append(f"• {title} — {first}.")
    lines.append("Istalgan mavzuni nomi bilan so'rang, men batafsil tushuntiraman.")
    return "\n".join(lines)
