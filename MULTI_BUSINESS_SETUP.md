# إعداد النظام متعدد الأنشطة التجارية

هذا المستودع يدعم ربط محادثات العملاء بالنشاط التجاري عبر جدول Supabase الحالي `channel_connections`. لا تضع رموز الوصول في هذا الملف أو في GitHub.

## 1. ربط رقم WhatsApp بمتجر

من Supabase SQL Editor، استخدم رقم النشاط التجاري الموجود في جدول `businesses` ومعرّف رقم WhatsApp الذي يظهر في Meta باسم **Phone Number ID**:

```sql
insert into public.channel_connections (business_id, channel, external_account_id)
values ('BUSINESS_UUID_HERE', 'whatsapp', 'WHATSAPP_PHONE_NUMBER_ID_HERE');
```

لا تستخدم رقم هاتف العميل هنا؛ المطلوب هو Phone Number ID الخاص بحساب النشاط.

## 2. ربط قناة منتجات Telegram بمتجر

أضف ربطًا مستقلًا لقناة المنتجات باستخدام معرّف القناة:

```sql
insert into public.channel_connections (business_id, channel, external_account_id)
values ('BUSINESS_UUID_HERE', 'telegram_products', 'TELEGRAM_SOURCE_CHAT_ID_HERE');
```

ثم في Render > Environment ضع `TELEGRAM_PRODUCTS_CHAT_ID` (معرّف القناة أو اسمها العام) و`TELEGRAM_PRODUCTS_CURRENCY` مثل `YER` أو `SAR`.

لم يعد النظام يختار متجرًا افتراضيًا ثابتًا لاستيراد المنتجات. إذا لم يجد ربط القناة، فسيتوقف الاستيراد ما لم تضبط صراحةً `TELEGRAM_PRODUCTS_BUSINESS_ID` في Render.

## 3. روابط العملاء على Telegram

لكل متجر رابط بدء مستقل، بصيغة:

`https://t.me/TELEGRAM_BOT_USERNAME?start=BUSINESS_UUID`

عندما يضغط العميل Start، تُربط محادثته بهذا النشاط. رسائل خدمة العملاء تتم في المحادثة الخاصة؛ رسائل المجموعات يتم تجاهلها حتى لا يرد البوت على كل ما يُنشر فيها.

## 4. حدود المرحلة الحالية

- ربط WhatsApp يعتمد على `WHATSAPP_TOKEN` الموجود في Render. ربط أرقام متعددة يعمل فقط إذا كان الرمز يملك صلاحية الإرسال من تلك الأرقام. إدارة رمز مستقل لكل تاجر تحتاج تخزين أسرار آمنًا وتدفق إعداد/تفويض منفصل.
- لوحة الإدارة الحالية تستخدم بيانات دخول مدير عامة، وليست بعد لوحة تسجيل ذاتي مع حساب وصلاحيات منفصلة لكل تاجر. لا تشارك بيانات دخول المدير مع التجار.
- اختبر متجرين مختلفين برسائل فعلية للتأكد من عدم اختلاط الكتالوجات.
