# Loyihalar boti

Telegram bot: machine, CTF va ZIP CTF yechimlaringizni (usul, flag, fayl, rasm, izoh) saqlaydi
va faqat siz ruxsat bergan odamlarga ko'rsatadi.

## Ishga tushirish

```powershell
pip install -r requirements.txt
```

`bot` papkasida `.env` fayl yarating:

```
BOT_TOKEN=123456:ABC...        # @BotFather dan olingan token
ADMIN_ID=123456789             # sizning Telegram ID ingiz (@userinfobot orqali bilish mumkin)
```

```powershell
python bot.py
```

Ma'lumotlar `data/bot.db` (SQLite) faylida saqlanadi. Fayl va rasmlarning o'zi Telegram serverida
turadi, bazada faqat ularning `file_id` si bor. Shu sababli botni boshqa token bilan
almashtirsangiz, eski fayllar ochilmaydi.

## Tuzilma

```
Loyiha
 ├─ 🖥 Machine   → machine raqami/nomi → usul → flag → fayllar
 ├─ 🚩 CTF       → CTF nomi            → usul → flag → fayllar
 └─ 🗜 ZIP CTF   → ZIP CTF nomi        → usul → flag → fayllar
```

Har bir ekranda `◀️ Orqaga` tugmasi bor. Ish qo'shish paytida u bir qadam orqaga qaytaradi.

## Admin (ADMIN_ID)

- Botni faqat shu ID admin sifatida taniydi.
- **⚙️ Admin panel → ➕ Loyiha qo'shish**: yangi loyiha nomini yozasiz, bot ichida shu nomda tugma chiqadi.
- Bo'lim ichida **➕ Qo'shish**: nom → usul (skript / oddiy kod / tool) → flag → qiyinlik (Easy/Medium/Hard/Insane)
  → teglar (vergul bilan, masalan `web, sql injection`) → fayl, rasm, izoh (istalgan formatda, ketma-ket) → ✅ Tugatish.
  Qiyinlik va teglarni o'tkazib yuborsa bo'ladi.
- Ishni ochganda **✏️ Tahrirlash**, **➕ Material** va **🗑 O'chirish** bor. Tahrirlashda nom, usul, flag, qiyinlik,
  teglar o'zgaradi, **📎 Materiallar** ichida esa har bir materialning matni/izohini o'zgartirish yoki o'chirish mumkin.
- **➕ Ruxsat berish**: foydalanuvchi ID yoki @username → qaysi bo'limlar → qaysi loyihalar → 💾 Saqlash.
  Keyin **muddat** tanlanadi: cheksiz / 1 kun / 3 kun / 1 hafta / 1 oy. Muddat tugagach ruxsat o'zi yopiladi
  (foydalanuvchiga "muddati tugagan" deb chiqadi), admin uni **👥 Foydalanuvchilar** dan qayta uzaytiradi.
  Username bo'yicha berilsa, u botga birinchi marta `/start` bosganda ruxsat kuchga kiradi.
- **👥 Foydalanuvchilar**: ruxsatlarni o'zgartirish yoki olib tashlash.
- **🗑 O'chirish**: loyiha → bo'lim → o'chiriladigan ishlarni belgilash (yoki butun loyiha), tasdiqlash bilan.
- **📊 Statistika**: faollik grafigi (7 / 30 / 90 kun): har kun uchun ustun, **yashil** = shu kuni ish/loyiha qo'shilgan
  (balandligi soni), **qizil** = nofaol kun. Loyiha qo'shilgan sanalar punktir chiziq bilan belgilanadi. Ostida faol/nofaol
  kunlar soni, joriy va eng uzun seriya. **📋 Matnli statistika**: loyiha/ish/fayl soni, bo'limlar, qiyinlik, usullar,
  top teglar, oxirgi 7 kundagi ko'rishlar.
- **🛡 Kirish tarixi**: ruxsat berilgan foydalanuvchilar qachon qaysi ishni ochgani (oxirgi 90 kun saqlanadi).
- **💾 Backup**: pastdagi bo'limga qarang.

## Boshqa foydalanuvchilar

- Ruxsat berilmagan odam `/start` bossa, faqat **✍️ Adminga yozish** tugmasi chiqadi, boshqa hech narsa ishlamaydi.
- Xabar adminga keladi, u yerda **✅ Ruxsat berish** va **✍️ Javob** tugmalari bor.
- Ruxsat berilgach foydalanuvchi faqat o'ziga ochilgan loyiha va bo'limlarni ko'radi (faqat o'qish).
- **🔍 Qidiruv**: nom, flag, teg, usul yoki qiyinlik bo'yicha, faqat o'ziga ochiq ishlar ichidan qidiradi.
- Bo'lim ichida **🏷 Filtr**: qiyinlik yoki teg bo'yicha saralash.
- Ruxsatni olib tashlasangiz, u darhol yo'qoladi.

## Backup va tiklash

Ma'lumotlar `data/bot.db` faylida turadi, shuning uchun botni o'chirib qayta yoqsangiz hammasi joyida qoladi.
Baza yo'qolsa yoki botni/tokenni almashtirsangiz, backupdan tiklaysiz.

- **⚙️ Admin panel → 💾 Backup → 📦 Backup olish**: baza va **fayllarning o'zi** (rasm, hujjat va boshqalar)
  bitta ZIP ga yig'iladi. ZIP sizga Telegramda yuboriladi va `backups/` papkaga ham saqlanadi.
- **♻️ Tiklash**: ZIP ni botga yuboring (yoki `backups/` dagilardan tanlang) → tasdiqlang.
  Hamma loyiha, ish, fayl va ruxsatlar qaytadi. Fayllar qaytadan yuklanadi, shuning uchun **yangi bot yoki
  yangi token bilan ham ishlaydi** (faqat `.env` da `ADMIN_ID` to'g'ri bo'lsin).
- Tiklashdan oldin hozirgi bazaning nusxasi `backups/pre_restore_*.db` ga saqlanadi.
- **🕒 Avto-backup**: har kuni yoki har hafta (yoki o'chiq), tugma bilan almashtiriladi. Bot ishlab turgan bo'lsa
  o'zi backup olib sizga yuboradi, oxirgi 7 tasi `backups/` da saqlanadi (`backup_auto_*.zip`).

Cheklovlar (Telegram qoidalari):
- Bot **20 MB dan katta** faylni yuklab ola olmaydi. Bunday fayl backupga kirmaydi, tiklaganda o'rniga
  "⚠️ Fayl tiklanmadi" izohi qo'yiladi.
- ZIP **50 MB dan** katta bo'lsa Telegramga yuborilmaydi, faqat `backups/` da qoladi.
- ZIP ni Telegram orqali tiklash uchun u **20 MB dan** kichik bo'lishi kerak. Kattasini `backups/` papkaga
  qo'lda tashlasangiz bo'ladi, ♻️ Tiklash ekranida chiqadi.
