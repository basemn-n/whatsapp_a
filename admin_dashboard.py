import requests
import os
import hmac
import logging
import hashlib
import secrets
import base64
import json
import time
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

security = HTTPBasic(auto_error=False)
logger = logging.getLogger(__name__)

MERCHANT_SESSION_SECONDS = 8 * 60 * 60


class MerchantLogin(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class MerchantAccountCreate(BaseModel):
    business_id: str
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=12, max_length=256)


def _merchant_session_secret():
    return os.getenv("MERCHANT_SESSION_SECRET", "")


def _password_hash(password: str, salt: bytes = None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    return (
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
        + "$"
        + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    )


def _password_matches(password: str, encoded: str):
    try:
        salt_part, digest_part = encoded.split("$", 1)
        salt = base64.urlsafe_b64decode(salt_part + "=" * (-len(salt_part) % 4))
        expected = base64.urlsafe_b64decode(digest_part + "=" * (-len(digest_part) % 4))
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _issue_merchant_token(email: str, business_id: str):
    secret = _merchant_session_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="إعداد MERCHANT_SESSION_SECRET غير مكتمل في Render",
        )
    payload = {
        "email": email,
        "business_id": business_id,
        "exp": int(time.time()) + MERCHANT_SESSION_SECONDS,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    signed = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return encoded + "." + signed


def _read_merchant_token(token: str):
    secret = _merchant_session_secret()
    if not secret or not token or "." not in token:
        return None
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = hmac.new(
            secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        supplied = base64.urlsafe_b64decode(
            supplied_signature + "=" * (-len(supplied_signature) % 4)
        )
        if not hmac.compare_digest(expected_signature, supplied):
            return None
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw.decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        if not payload.get("email") or not payload.get("business_id"):
            return None
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


STATUSES = {
    "pending": "قيد المراجعة",
    "approved": "تمت الموافقة",
    "processing": "قيد التجهيز",
    "shipped": "قيد التوصيل",
    "delivered": "تم التسليم",
    "cancelled": "ملغي",
}


class StatusUpdate(BaseModel):
    status: str


class ProductCreate(BaseModel):
    business_id: str
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    price: Decimal = Field(ge=0)
    currency: str = Field(default="YER", min_length=1, max_length=10)
    is_available: bool = True


class ProductUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    price: Decimal = Field(ge=0)
    currency: str = Field(default="YER", min_length=1, max_length=10)
    is_available: bool


def send_telegram_status_notification(chat_id, order):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")

    if not token or not chat_id:
        logger.warning(
            "Telegram notification skipped: token or chat_id is missing."
        )
        return False

    status = order.get("status", "")
    status_label = STATUSES.get(status, status)

    order_number = order.get("order_number") or order.get("id")
    product_name = order.get("product_name") or "غير محدد"

    message = (
        "🔔 تحديث حالة طلبك\n\n"
        f"رقم الطلب: {order_number}\n"
        f"المنتج: {product_name}\n"
        f"الحالة الجديدة: {status_label}\n\n"
        "شكرًا لثقتك بنا."
    )

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": str(chat_id),
                "text": message,
            },
            timeout=10,
        )
        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            logger.warning("Telegram notification was not accepted.")
            return False

        logger.info(
            "Telegram order status notification sent successfully."
        )
        return True

    except Exception:
        logger.exception("Failed to send Telegram order notification.")
        return False


def create_admin_router(supabase):
    router = APIRouter()

    def check_admin(
        request: Request,
        credentials: HTTPBasicCredentials = Depends(security),
    ):
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            payload = _read_merchant_token(authorization[7:].strip())
            if not payload:
                raise HTTPException(
                    status_code=401,
                    detail="انتهت جلسة التاجر أو أنها غير صالحة. سجّل الدخول مجددًا.",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            try:
                account = (
                    supabase.table("merchant_accounts")
                    .select("email, business_id, is_active")
                    .eq("email", payload["email"].lower())
                    .eq("business_id", payload["business_id"])
                    .eq("is_active", True)
                    .limit(1)
                    .execute()
                )
            except Exception:
                logger.exception("تعذر التحقق من حساب التاجر.")
                raise HTTPException(status_code=503, detail="تعذر التحقق من حساب التاجر")
            if not account.data:
                raise HTTPException(status_code=401, detail="حساب التاجر غير نشط")
            return {
                "role": "merchant",
                "email": payload["email"].lower(),
                "business_id": str(payload["business_id"]),
            }

        username = os.getenv("ADMIN_USERNAME", "")
        password = os.getenv("ADMIN_PASSWORD", "")
        if not username or not password:
            raise HTTPException(
                status_code=503,
                detail="إعداد بيانات دخول المدير غير مكتمل",
            )
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="يلزم تسجيل الدخول",
                headers={"WWW-Authenticate": "Basic"},
            )
        valid_user = hmac.compare_digest(credentials.username, username)
        valid_password = hmac.compare_digest(credentials.password, password)
        if not (valid_user and valid_password):
            raise HTTPException(
                status_code=401,
                detail="بيانات الدخول غير صحيحة",
                headers={"WWW-Authenticate": "Basic"},
            )
        return {"role": "admin", "business_id": None}

    def ensure_business_access(business_id, actor):
        if actor.get("role") == "admin":
            return
        if str(actor.get("business_id") or "") != str(business_id or ""):
            raise HTTPException(
                status_code=403,
                detail="لا تملك صلاحية الوصول إلى بيانات هذا النشاط التجاري",
            )

    @router.post("/api/merchant/login")
    def merchant_login(payload: MerchantLogin):
        if not _merchant_session_secret():
            raise HTTPException(
                status_code=503,
                detail="يلزم إعداد MERCHANT_SESSION_SECRET في Render قبل تفعيل حسابات التجار",
            )
        email = payload.email.strip().lower()
        if "@" not in email:
            raise HTTPException(status_code=401, detail="البريد الإلكتروني أو كلمة المرور غير صحيحة")
        try:
            result = (
                supabase.table("merchant_accounts")
                .select("email, business_id, password_hash, is_active")
                .eq("email", email)
                .eq("is_active", True)
                .limit(1)
                .execute()
            )
        except Exception:
            logger.exception("تعذر البحث عن حساب التاجر.")
            raise HTTPException(
                status_code=503,
                detail="جدول حسابات التجار غير جاهز. نفّذ ملف SQL الخاص بحسابات التجار أولًا.",
            )
        account = result.data[0] if result.data else None
        if not account or not _password_matches(payload.password, account.get("password_hash", "")):
            raise HTTPException(status_code=401, detail="البريد الإلكتروني أو كلمة المرور غير صحيحة")
        token = _issue_merchant_token(email, str(account["business_id"]))
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": MERCHANT_SESSION_SECONDS,
            "business_id": str(account["business_id"]),
        }

    @router.post("/api/admin/merchant-accounts")
    def create_merchant_account(
        payload: MerchantAccountCreate,
        actor=Depends(check_admin),
    ):
        if actor.get("role") != "admin":
            raise HTTPException(status_code=403, detail="إنشاء حسابات التجار متاح للمدير العام فقط")
        if not _merchant_session_secret():
            raise HTTPException(
                status_code=503,
                detail="أضف MERCHANT_SESSION_SECRET إلى Render قبل إنشاء حسابات التجار",
            )
        get_business_or_404(payload.business_id)
        email = payload.email.strip().lower()
        if "@" not in email:
            raise HTTPException(status_code=400, detail="أدخل بريدًا إلكترونيًا صحيحًا")
        try:
            result = (
                supabase.table("merchant_accounts")
                .insert({
                    "business_id": payload.business_id,
                    "email": email,
                    "password_hash": _password_hash(payload.password),
                    "is_active": True,
                })
                .select("id, business_id, email, is_active, created_at")
                .execute()
            )
            if not result.data:
                raise HTTPException(status_code=500, detail="لم يُنشأ حساب التاجر")
            return {"success": True, "account": result.data[0]}
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر إنشاء حساب التاجر.")
            raise HTTPException(
                status_code=409,
                detail="تعذر إنشاء الحساب. قد يكون البريد مستخدمًا مسبقًا أو أن جدول حسابات التجار غير مُعد.",
            )

    def get_business_or_404(business_id):
        try:
            result = (
                supabase.table("businesses")
                .select("id, name")
                .eq("id", business_id)
                .limit(1)
                .execute()
            )
        except Exception:
            logger.exception("تعذر التحقق من النشاط التجاري")
            raise HTTPException(
                status_code=500,
                detail="تعذر التحقق من النشاط التجاري",
            )

        if not result.data:
            raise HTTPException(
                status_code=404,
                detail="النشاط التجاري غير موجود",
            )

        return result.data[0]

    @router.get("/admin", response_class=HTMLResponse)
    def dashboard():
        return HTMLResponse(r"""
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>لوحة إدارة المتجر</title>
<style>
*{box-sizing:border-box}
body{font-family:Arial,sans-serif;background:#f2f5fa;margin:0;padding:18px;color:#182238}
main{max-width:1450px;margin:auto}
h2{margin-bottom:8px}
button,select,input,textarea{font:inherit;padding:10px;border:1px solid #cbd5e1;border-radius:7px}
button{cursor:pointer;background:#1559a8;color:white;border:0}
button:disabled{opacity:.5;cursor:wait}
input,textarea{width:100%;background:white}
label{display:block;margin:10px 0 5px}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0}
.tabs button{background:#64748b}
.tabs button.active{background:#1559a8}
.panel{background:white;border-radius:12px;padding:16px;margin-bottom:18px}
.toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:12px 0}
.toolbar select{max-width:100%;background:white}
.toolbar label{margin:0}
.wrap{overflow-x:auto;border:1px solid #e2e8f0;border-radius:8px}
table{width:100%;border-collapse:collapse;min-width:850px}
th,td{padding:10px;border-bottom:1px solid #e2e8f0;text-align:right;vertical-align:middle}
th{background:#eaf0f8}
td select{max-width:180px;background:white}
#msg{margin:12px 0;white-space:pre-wrap}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.full{grid-column:1/-1}
.form-actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
.secondary{background:#64748b}
.danger{background:#b42318}
.status-on{color:#087443;font-weight:bold}
.status-off{color:#b42318;font-weight:bold}
.hidden{display:none!important}
.stats{
  display:grid;
  grid-template-columns:repeat(4,minmax(0,1fr));
  gap:12px;
  margin:16px 0;
}
.stat-card{
  background:#fff;
  border:1px solid #e2e8f0;
  border-radius:10px;
  padding:16px;
}
.stat-card span{
  display:block;
  color:#64748b;
  font-size:14px;
  margin-bottom:10px;
}
.stat-card strong{
  display:block;
  font-size:26px;
  color:#1559a8;
}
.search-box{min-width:250px;flex:1}
#orderSearch{width:100%}
@media(max-width:800px){
  .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:650px){
  body{padding:9px}
  .grid{grid-template-columns:1fr}
  .full{grid-column:auto}
  .panel{padding:11px}
}
</style>
</head>
<body>
<main>
<h2>لوحة إدارة المتجر</h2>
<p>إدارة الطلبات والمنتجات المحفوظة في قاعدة البيانات.</p>

<div class="toolbar" id="businessToolbar" style="margin-top:16px">
<label for="businessSelect">النشاط التجاري الذي تعمل عليه اللوحة:</label>
<select id="businessSelect"></select>
<button id="createMerchantAccountBtn" class="secondary hidden" onclick="createMerchantAccount()">إنشاء حساب تاجر</button>
</div>

<div class="tabs">
<button id="ordersTab" class="active"
onclick="showTab('orders')">الطلبات</button>
<button id="productsTab"
onclick="showTab('products')">المنتجات</button>
<button id="reportsTab"
onclick="showTab('reports')">تقارير المبيعات</button>
</div>

<div id="msg" role="status">جارٍ الاتصال...</div>

<section id="ordersPanel" class="panel">
<h3>إدارة طلبات العملاء</h3>

<div class="stats">
  <div class="stat-card">
    <span>إجمالي الطلبات المحمّلة</span>
    <strong id="totalOrders">0</strong>
  </div>
  <div class="stat-card">
    <span>قيد المراجعة</span>
    <strong id="pendingOrders">0</strong>
  </div>
  <div class="stat-card">
    <span>قيد التجهيز</span>
    <strong id="processingOrders">0</strong>
  </div>
  <div class="stat-card">
    <span>تم التسليم</span>
    <strong id="deliveredOrders">0</strong>
  </div>
</div>

<div class="toolbar">
  <div class="search-box">
    <input
      id="orderSearch"
      type="search"
      placeholder="ابحث برقم الطلب أو اسم العميل أو الهاتف أو المنتج"
      aria-label="البحث في الطلبات"
    >
  </div>

  <label for="filter">حالة الطلب:</label>
  <select id="filter">
    <option value="">كل الطلبات</option>
    <option value="pending">قيد المراجعة</option>
    <option value="approved">تمت الموافقة</option>
    <option value="processing">قيد التجهيز</option>
    <option value="shipped">قيد التوصيل</option>
    <option value="delivered">تم التسليم</option>
    <option value="cancelled">ملغي</option>
  </select>

  <button id="refreshOrdersBtn" onclick="loadOrders()">
    تحديث الطلبات
  </button>
</div>

<div class="wrap">
<table>
<thead>
<tr>
<th>رقم الطلب</th>
<th>المنتج</th>
<th>الكمية</th>
<th>العميل</th>
<th>الهاتف</th>
<th>العنوان</th>
<th>الدفع</th>
<th>الحالة</th>
<th>حفظ</th>
</tr>
</thead>
<tbody id="orderRows"></tbody>
</table>
</div>
</section>

<section id="productsPanel" class="panel hidden">
<h3>إدارة المنتجات</h3>
<p>اختر النشاط التجاري الذي تريد إدارة منتجاته.</p>

<div class="toolbar"><button onclick="loadProducts()">تحديث المنتجات</button></div>
<div class="panel">
  <strong>رابط العملاء لفتح مساعد المتجر في Telegram</strong>
  <p>انسخ هذا الرابط وضعه في وصف قناة المنتجات أو في منشور مثبت. يجب أن يضغط العميل عليه ثم يضغط Start في البوت.</p>
  <p id="telegramCustomerLink">اختر النشاط التجاري لعرض الرابط.</p>
  <button type="button" class="secondary" onclick="copyTelegramLink()">نسخ رابط العملاء</button>
</div>

<div class="panel">
<h3 id="formTitle">إضافة منتج جديد</h3>

<form id="productForm">
<input type="hidden" id="productId">

<div class="grid">
<div>
<label for="productName">اسم المنتج *</label>
<input id="productName" maxlength="200" required>
</div>

<div>
<label for="productPrice">السعر *</label>
<input id="productPrice" type="number" min="0" step="0.01" required>
</div>

<div>
<label for="productCurrency">العملة *</label>
<input id="productCurrency" value="YER" maxlength="10" required>
</div>

<div>
<label for="productAvailable">التوفر</label>
<select id="productAvailable">
<option value="true">متاح للطلب</option>
<option value="false">غير متاح</option>
</select>
</div>

<div class="full">
<label for="productDescription">وصف المنتج</label>
<textarea id="productDescription" rows="3" maxlength="2000"></textarea>
</div>
</div>

<div class="form-actions">
<button id="saveProductBtn" type="submit">حفظ المنتج</button>
<button type="button" class="secondary"
onclick="resetProductForm()">إلغاء التعديل</button>
</div>
</form>
</div>

<div class="wrap">
<table>
<thead>
<tr>
<th>المنتج</th>
<th>الوصف</th>
<th>السعر</th>
<th>العملة</th>
<th>التوفر</th>
<th>إجراء</th>
</tr>
</thead>
<tbody id="productRows"></tbody>
</table>
</div>
</section>

<section id="reportsPanel" class="panel hidden">
<h3>تقارير المبيعات</h3>
<p>تعرض التقارير الطلبات المحمّلة (حتى 1000 طلب حديث). المبيعات الدقيقة متاحة للطلبات التي حُفظ سعرها وقت الطلب؛ أما الطلبات القديمة فتظهر كتقدير حسب السعر الحالي للمنتج.</p>
<div class="toolbar">
<button id="refreshReportsBtn" onclick="loadReports()">تحديث التقرير</button>
</div>
<div class="stats">
  <div class="stat-card"><span>إجمالي الطلبات</span><strong id="reportTotalOrders">—</strong></div>
  <div class="stat-card"><span>قيد المراجعة</span><strong id="reportPendingOrders">—</strong></div>
  <div class="stat-card"><span>قيد التجهيز</span><strong id="reportProcessingOrders">—</strong></div>
  <div class="stat-card"><span>تم التسليم</span><strong id="reportDeliveredOrders">—</strong></div>
</div>
<div class="panel">
<h4>قيمة الطلبات التي تم تسليمها</h4>
<p id="exactRevenue">المبيعات الدقيقة: —</p>
<p id="estimatedRevenue">المبيعات التقديرية للطلبات القديمة: —</p>
<p id="missingRevenue">طلبات لا يمكن تقدير قيمتها: —</p>
</div>
</section>
</main>

<script>
const labels = {
  pending: "قيد المراجعة",
  approved: "تمت الموافقة",
  processing: "قيد التجهيز",
  shipped: "قيد التوصيل",
  delivered: "تم التسليم",
  cancelled: "ملغي"
};

let auth = "";
let isMerchantSession = false;
let businesses = [];
let currentProducts = [];
let allOrders = [];

const msg = document.getElementById("msg");

function setMessage(text) {
  msg.textContent = text;
}

function cell(value) {
  const td = document.createElement("td");
  td.textContent =
    value === null || value === undefined || value === ""
      ? "—"
      : String(value);
  return td;
}

async function api(url, options = {}) {
  const headers = {
    ...(options.headers || {}),
    "Authorization": auth
  };

  if (options.body) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(url, {
    ...options,
    headers
  });

  if (response.status === 401) {
    auth = "";
    throw Error(
      "انتهت صلاحية تسجيل الدخول أو بيانات الدخول غير صحيحة. حدّث الصفحة وسجّل الدخول مجددًا."
    );
  }

  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw Error(
      data.detail || "حدث خطأ أثناء الاتصال بالخادم."
    );
  }

  return data;
}

async function login() {
  const username = prompt("للمدير: اسم المستخدم الإداري. للتاجر: البريد الإلكتروني المسجل له.");
  if (username === null) return false;
  const password = prompt("كلمة المرور");
  if (password === null) return false;

  try {
    if (username.includes("@")) {
      const response = await fetch("/api/merchant/login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({email: username.trim(), password})
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw Error(data.detail || "تعذر تسجيل الدخول.");
      auth = "Bearer " + data.access_token;
      isMerchantSession = true;
    } else {
      auth = "Basic " + btoa(
        unescape(encodeURIComponent(username + ":" + password))
      );
      isMerchantSession = false;
    }
    await api("/api/admin/businesses");
    return true;
  } catch (error) {
    auth = "";
    setMessage(error.message);
    return false;
  }
}

async function ensureLogin() {
  if (auth) return true;
  return await login();
}

function showTab(tab) {
  document.getElementById("ordersPanel")
    .classList.toggle("hidden", tab !== "orders");

  document.getElementById("productsPanel")
    .classList.toggle("hidden", tab !== "products");

  document.getElementById("reportsPanel")
    .classList.toggle("hidden", tab !== "reports");

  document.getElementById("ordersTab")
    .classList.toggle("active", tab === "orders");

  document.getElementById("productsTab")
    .classList.toggle("active", tab === "products");

  document.getElementById("reportsTab")
    .classList.toggle("active", tab === "reports");

  if (tab === "products") {
    loadBusinesses();
  } else if (tab === "reports") {
    loadReports();
  } else {
    loadOrders();
  }
}

/* تحميل الطلبات */
async function loadOrders() {
  const button = document.getElementById("refreshOrdersBtn");
  button.disabled = true;

  try {
    if (!(await ensureLogin())) return;

    setMessage("جارٍ تحميل الطلبات...");

    if (!businesses.length) await loadBusinesses();
    const businessId = document.getElementById("businessSelect").value;
    if (!businessId) { setMessage("أضف نشاطًا تجاريًا أو اختر نشاطًا أولًا."); return; }
    const data = await api("/api/admin/orders?business_id=" + encodeURIComponent(businessId));
    allOrders = Array.isArray(data.orders) ? data.orders : [];

    renderOrders();

    setMessage(
      "تم تحميل " + allOrders.length +
      " طلب. يمكنك البحث أو التصفية حسب الحالة."
    );
  } catch (error) {
    setMessage(error.message);
  } finally {
    button.disabled = false;
  }
}

/* إحصائيات الطلبات والبحث والتصفية */
function renderOrders() {
  const statusFilter = document.getElementById("filter").value;
  const searchText = document.getElementById("orderSearch")
    .value.trim().toLowerCase();

  document.getElementById("totalOrders").textContent =
    allOrders.length;

  document.getElementById("pendingOrders").textContent =
    allOrders.filter(order => order.status === "pending").length;

  document.getElementById("processingOrders").textContent =
    allOrders.filter(order => order.status === "processing").length;

  document.getElementById("deliveredOrders").textContent =
    allOrders.filter(order => order.status === "delivered").length;

  const visibleOrders = allOrders.filter(order => {
    const matchesStatus =
      !statusFilter || order.status === statusFilter;

    const searchableText = [
      order.order_number,
      order.id,
      order.product_name,
      order.customer_name,
      order.customer_phone,
      order.customer_address
    ]
      .map(value => String(value ?? "").toLowerCase())
      .join(" ");

    return matchesStatus &&
      searchableText.includes(searchText);
  });

  const rows = document.getElementById("orderRows");
  rows.replaceChildren();

  if (visibleOrders.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 9;
    td.textContent = "لا توجد طلبات تطابق البحث أو التصفية.";
    tr.appendChild(td);
    rows.appendChild(tr);
  }

  for (const order of visibleOrders) {
    const tr = document.createElement("tr");

    [
      order.order_number ?? order.id,
      order.product_name,
      order.quantity,
      order.customer_name,
      order.customer_phone,
      order.customer_address,
      order.payment_method
    ].forEach(value => tr.appendChild(cell(value)));

    const statusCell = document.createElement("td");
    const select = document.createElement("select");

    for (const [value, label] of Object.entries(labels)) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      option.selected = value === order.status;
      select.appendChild(option);
    }

    statusCell.appendChild(select);
    tr.appendChild(statusCell);

    const actionCell = document.createElement("td");
    const button = document.createElement("button");
    button.textContent = "حفظ";

    button.onclick = async () => {
      button.disabled = true;

      try {
        const result = await api(
          "/api/admin/orders/" +
          encodeURIComponent(order.id) +
          "/status?business_id=" + encodeURIComponent(document.getElementById("businessSelect").value),
          {
            method: "PATCH",
            body: JSON.stringify({
              status: select.value
            })
          }
        );

        if (result.notification_sent) {
          setMessage(
            "تم حفظ حالة الطلب وإرسال إشعار Telegram للعميل."
          );
        } else {
          setMessage(
            "تم حفظ حالة الطلب. لم يُؤكَّد إرسال إشعار Telegram؛ تحقق من ربط محادثة العميل وإعدادات البوت."
          );
        }

        await loadOrders();
      } catch (error) {
        setMessage(error.message);
      } finally {
        button.disabled = false;
      }
    };

    actionCell.appendChild(button);
    tr.appendChild(actionCell);
    rows.appendChild(tr);
  }

  setMessage(
    "إجمالي الطلبات المحمّلة: " + allOrders.length +
    " | نتائج البحث والتصفية: " + visibleOrders.length
  );
}

/* تقرير المبيعات */
function formatMoneyGroups(groups) {
  if (!groups || Object.keys(groups).length === 0) return "لا توجد بيانات";
  return Object.entries(groups)
    .map(([currency, amount]) =>
      Number(amount).toLocaleString("ar", {maximumFractionDigits: 2}) + " " + currency
    )
    .join("، ");
}

async function loadReports() {
  const button = document.getElementById("refreshReportsBtn");
  button.disabled = true;
  try {
    if (!(await ensureLogin())) return;
    setMessage("جارٍ تحميل تقرير المبيعات...");
    if (!businesses.length) await loadBusinesses();
    const businessId = document.getElementById("businessSelect").value;
    if (!businessId) { setMessage("أضف نشاطًا تجاريًا أو اختر نشاطًا أولًا."); return; }
    const data = await api("/api/admin/reports/sales?business_id=" + encodeURIComponent(businessId));
    document.getElementById("reportTotalOrders").textContent = data.total_orders ?? 0;
    document.getElementById("reportPendingOrders").textContent = data.pending_orders ?? 0;
    document.getElementById("reportProcessingOrders").textContent = data.processing_orders ?? 0;
    document.getElementById("reportDeliveredOrders").textContent = data.delivered_orders ?? 0;
    document.getElementById("exactRevenue").textContent =
      "المبيعات الدقيقة للطلبات المسلّمة: " + formatMoneyGroups(data.exact_revenue);
    document.getElementById("estimatedRevenue").textContent =
      "تقدير الطلبات القديمة حسب السعر الحالي: " + formatMoneyGroups(data.estimated_revenue);
    document.getElementById("missingRevenue").textContent =
      "طلبات مسلّمة بلا سعر يمكن حسابه: " + (data.missing_price_orders ?? 0);
    setMessage("تم تحديث تقرير المبيعات.");
  } catch (error) {
    setMessage(error.message);
  } finally {
    button.disabled = false;
  }
}

const telegramBotUsername = "SmartCustomerHelp2026Bot";

function updateTelegramCustomerLink() {
  const businessId = document.getElementById("businessSelect").value;
  const container = document.getElementById("telegramCustomerLink");
  container.replaceChildren();
  if (!businessId) {
    container.textContent = "اختر النشاط التجاري لعرض الرابط.";
    return;
  }
  const url = "https://t.me/" + telegramBotUsername +
    "?start=" + encodeURIComponent(businessId);
  const link = document.createElement("a");
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = url;
  container.appendChild(link);
}

async function copyTelegramLink() {
  const businessId = document.getElementById("businessSelect").value;
  if (!businessId) {
    setMessage("اختر النشاط التجاري أولًا.");
    return;
  }
  const url = "https://t.me/" + telegramBotUsername +
    "?start=" + encodeURIComponent(businessId);
  try {
    await navigator.clipboard.writeText(url);
    setMessage("تم نسخ رابط العملاء.");
  } catch (error) {
    setMessage("تعذر النسخ التلقائي. انسخ الرابط الظاهر يدويًا.");
  }
}

/* الأنشطة التجارية */
async function loadBusinesses() {
  try {
    if (!(await ensureLogin())) return;

    setMessage("جارٍ تحميل الأنشطة التجارية...");

    const data = await api("/api/admin/businesses");
    businesses = data.businesses || [];

    const select = document.getElementById("businessSelect");
    document.getElementById("businessToolbar").classList.toggle("hidden", isMerchantSession);
    document.getElementById("createMerchantAccountBtn").classList.toggle("hidden", isMerchantSession);
    const previous = select.value;

    select.replaceChildren();

    for (const business of businesses) {
      const option = document.createElement("option");
      option.value = business.id;
      option.textContent = business.name || business.id;
      select.appendChild(option);
    }

    if (previous && businesses.some(b => b.id === previous)) {
      select.value = previous;
    }

    if (!businesses.length) {
      setMessage("لا توجد أنشطة تجارية متاحة.");
      document.getElementById("productRows").replaceChildren();
      updateTelegramCustomerLink();
      return;
    }

    updateTelegramCustomerLink();
    await loadProducts();
  } catch (error) {
    setMessage(error.message);
  }
}

async function createMerchantAccount() {
  if (isMerchantSession) {
    setMessage("إنشاء حسابات التجار متاح للمدير العام فقط.");
    return;
  }
  const businessId = document.getElementById("businessSelect").value;
  if (!businessId) {
    setMessage("اختر النشاط التجاري الذي تريد إنشاء حساب له.");
    return;
  }
  const email = prompt("أدخل البريد الإلكتروني الذي سيستخدمه التاجر لتسجيل الدخول:");
  if (email === null) return;
  const password = prompt("أنشئ كلمة مرور قوية للتاجر (12 حرفًا على الأقل). سلّمها له بطريقة آمنة:");
  if (password === null) return;
  if (password.length < 12) {
    setMessage("يجب أن تكون كلمة المرور 12 حرفًا على الأقل.");
    return;
  }
  try {
    const result = await api("/api/admin/merchant-accounts", {
      method: "POST",
      body: JSON.stringify({business_id: businessId, email: email.trim(), password})
    });
    setMessage("تم إنشاء حساب التاجر: " + result.account.email + ". سلّم بيانات الدخول للتاجر بطريقة آمنة.");
  } catch (error) {
    setMessage(error.message);
  }
}

/* المنتجات */
async function loadProducts() {
  try {
    if (!(await ensureLogin())) return;

    const businessId = document.getElementById("businessSelect").value;

    if (!businessId) {
      setMessage("اختر نشاطًا تجاريًا أولًا.");
      return;
    }

    setMessage("جارٍ تحميل المنتجات...");

    const data = await api(
      "/api/admin/products?business_id=" +
      encodeURIComponent(businessId)
    );

    currentProducts = data.products || [];
    renderProducts();

    setMessage("عدد المنتجات: " + currentProducts.length);
  } catch (error) {
    setMessage(error.message);
  }
}

function renderProducts() {
  const rows = document.getElementById("productRows");
  rows.replaceChildren();

  if (!currentProducts.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.textContent = "لا توجد منتجات لهذا النشاط التجاري.";
    tr.appendChild(td);
    rows.appendChild(tr);
    return;
  }

  for (const product of currentProducts) {
    const tr = document.createElement("tr");

    tr.appendChild(cell(product.name));
    tr.appendChild(cell(product.description));
    tr.appendChild(cell(product.price));
    tr.appendChild(cell(product.currency));

    const availability = cell(
      product.is_available ? "متاح للطلب" : "غير متاح"
    );

    availability.className = product.is_available
      ? "status-on"
      : "status-off";

    tr.appendChild(availability);

    const action = document.createElement("td");
    const edit = document.createElement("button");
    edit.textContent = "تعديل";
    edit.onclick = () => editProduct(product);

    action.appendChild(edit);
    tr.appendChild(action);
    rows.appendChild(tr);
  }
}

function editProduct(product) {
  document.getElementById("productId").value = product.id;
  document.getElementById("productName").value = product.name || "";
  document.getElementById("productDescription").value =
    product.description || "";
  document.getElementById("productPrice").value =
    product.price ?? 0;
  document.getElementById("productCurrency").value =
    product.currency || "YER";
  document.getElementById("productAvailable").value =
    String(Boolean(product.is_available));

  document.getElementById("formTitle").textContent = "تعديل المنتج";
  document.getElementById("saveProductBtn").textContent =
    "حفظ التعديلات";

  document.getElementById("productName").focus();
  setMessage("أنت تعدّل المنتج: " + product.name);
}

function resetProductForm() {
  document.getElementById("productForm").reset();
  document.getElementById("productId").value = "";
  document.getElementById("productCurrency").value = "YER";
  document.getElementById("productAvailable").value = "true";
  document.getElementById("formTitle").textContent =
    "إضافة منتج جديد";
  document.getElementById("saveProductBtn").textContent =
    "حفظ المنتج";
}

/* حفظ منتج جديد أو تعديل منتج */
document.getElementById("productForm")
  .addEventListener("submit", async event => {
    event.preventDefault();

    const button = document.getElementById("saveProductBtn");
    const productId = document.getElementById("productId").value;
    const businessId =
      document.getElementById("businessSelect").value;

    if (!businessId) {
      setMessage("اختر النشاط التجاري أولًا.");
      return;
    }

    const payload = {
      business_id: businessId,
      name: document.getElementById("productName").value.trim(),
      description: document.getElementById("productDescription")
        .value.trim(),
      price: document.getElementById("productPrice").value,
      currency: document.getElementById("productCurrency")
        .value.trim(),
      is_available:
        document.getElementById("productAvailable").value === "true"
    };

    if (!payload.name || !payload.currency || payload.price === "") {
      setMessage("يرجى تعبئة اسم المنتج والسعر والعملة.");
      return;
    }

    button.disabled = true;

    try {
      if (productId) {
        delete payload.business_id;

        await api(
          "/api/admin/products/" +
          encodeURIComponent(productId) +
          "?business_id=" + encodeURIComponent(businessId),
          {
            method: "PATCH",
            body: JSON.stringify(payload)
          }
        );

        setMessage("تم تحديث المنتج بنجاح.");
      } else {
        await api("/api/admin/products", {
          method: "POST",
          body: JSON.stringify(payload)
        });

        setMessage("تمت إضافة المنتج بنجاح.");
      }

      resetProductForm();
      await loadProducts();
    } catch (error) {
      setMessage(error.message);
    } finally {
      button.disabled = false;
    }
  });

document.getElementById("businessSelect")
  .addEventListener("change", () => {
    resetProductForm();
    updateTelegramCustomerLink();
    loadProducts();
    loadOrders();
    loadReports();
  });

document.getElementById("filter")
  .addEventListener("change", renderOrders);

document.getElementById("orderSearch")
  .addEventListener("input", renderOrders);

loadOrders();
</script>
</body>
</html>
""")

    @router.get("/api/admin/businesses")
    def list_businesses(
        _admin=Depends(check_admin),
    ):
        try:
            if _admin.get("role") == "merchant":
                result = (
                    supabase.table("businesses")
                    .select("id, name")
                    .eq("id", _admin["business_id"])
                    .limit(1)
                    .execute()
                )
            else:
                result = (
                    supabase.table("businesses")
                    .select("id, name")
                    .order("name")
                    .limit(500)
                    .execute()
                )
            return {"businesses": result.data or []}
        except Exception:
            logger.exception("تعذر تحميل الأنشطة التجارية")
            raise HTTPException(
                status_code=500,
                detail="تعذر تحميل الأنشطة التجارية",
            )

    @router.get("/api/admin/products")
    def list_products(
        business_id: str,
        _admin=Depends(check_admin),
    ):
        ensure_business_access(business_id, _admin)
        get_business_or_404(business_id)

        try:
            result = (
                supabase.table("products")
                .select(
                    "id, business_id, name, description, "
                    "price, currency, is_available"
                )
                .eq("business_id", business_id)
                .order("name")
                .limit(1000)
                .execute()
            )
            return {"products": result.data or []}
        except Exception:
            logger.exception("تعذر تحميل المنتجات")
            raise HTTPException(
                status_code=500,
                detail="تعذر تحميل المنتجات",
            )

    @router.post("/api/admin/products")
    def create_product(
        payload: ProductCreate,
        _admin=Depends(check_admin),
    ):
        ensure_business_access(payload.business_id, _admin)
        get_business_or_404(payload.business_id)

        name = payload.name.strip()
        currency = payload.currency.strip().upper()

        if not name or not currency:
            raise HTTPException(
                status_code=400,
                detail="اسم المنتج والعملة مطلوبان",
            )

        try:
            result = (
                supabase.table("products")
                .insert({
                    "business_id": payload.business_id,
                    "name": name,
                    "description": payload.description.strip(),
                    "price": float(payload.price),
                    "currency": currency,
                    "is_available": payload.is_available,
                })
                .select(
                    "id, business_id, name, description, "
                    "price, currency, is_available"
                )
                .execute()
            )

            if not result.data:
                raise HTTPException(
                    status_code=500,
                    detail="لم تُرجع قاعدة البيانات المنتج الجديد",
                )

            return {"success": True, "product": result.data[0]}

        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذرت إضافة المنتج")
            raise HTTPException(
                status_code=500,
                detail="تعذرت إضافة المنتج. تحقق من أعمدة جدول products.",
            )

    @router.patch("/api/admin/products/{product_id}")
    def update_product(
        product_id: str,
        payload: ProductUpdate,
        business_id: str,
        _admin=Depends(check_admin),
    ):
        ensure_business_access(business_id, _admin)
        get_business_or_404(business_id)

        name = payload.name.strip()
        currency = payload.currency.strip().upper()

        if not name or not currency:
            raise HTTPException(
                status_code=400,
                detail="اسم المنتج والعملة مطلوبان",
            )

        try:
            result = (
                supabase.table("products")
                .update({
                    "name": name,
                    "description": payload.description.strip(),
                    "price": float(payload.price),
                    "currency": currency,
                    "is_available": payload.is_available,
                })
                .eq("id", product_id)
                .eq("business_id", business_id)
                .execute()
            )

            if not result.data:
                raise HTTPException(
                    status_code=404,
                    detail="المنتج غير موجود في هذا النشاط التجاري",
                )

            return {"success": True, "product": result.data[0]}

        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر تحديث المنتج")
            raise HTTPException(
                status_code=500,
                detail="تعذر تحديث المنتج. تحقق من أعمدة جدول products.",
            )

    @router.get("/api/admin/orders")
    def list_orders(
        business_id: str,
        _admin=Depends(check_admin),
        status: str = "",
    ):
        ensure_business_access(business_id, _admin)
        get_business_or_404(business_id)
        if status and status not in STATUSES:
            raise HTTPException(
                status_code=400,
                detail="حالة غير صحيحة",
            )

        query = (
            supabase.table("orders")
            .select("*")
            .eq("business_id", business_id)
            .order("created_at", desc=True)
            .limit(500)
        )

        if status:
            query = query.eq("status", status)

        try:
            result = query.execute()
            return {"orders": result.data or []}
        except Exception:
            logger.exception("تعذر تحميل الطلبات")
            raise HTTPException(
                status_code=500,
                detail="تعذر تحميل الطلبات",
            )

    @router.get("/api/admin/reports/sales")
    def sales_report(
        business_id: str,
        _admin=Depends(check_admin),
    ):
        ensure_business_access(business_id, _admin)
        get_business_or_404(business_id)
        """Summarize recent orders and distinguish saved revenue from estimates."""
        try:
            order_result = (
                supabase.table("orders")
                .select("*")
                .eq("business_id", business_id)
                .order("created_at", desc=True)
                .limit(1000)
                .execute()
            )
            orders = order_result.data or []

            product_result = (
                supabase.table("products")
                .select("business_id, name, price, currency")
                .eq("business_id", business_id)
                .limit(5000)
                .execute()
            )
            product_prices = {}
            for product in product_result.data or []:
                key = (
                    str(product.get("business_id") or ""),
                    str(product.get("name") or "").strip().casefold(),
                )
                product_prices[key] = {
                    "price": product.get("price"),
                    "currency": product.get("currency") or "YER",
                }

            counts = {
                "pending_orders": 0,
                "processing_orders": 0,
                "delivered_orders": 0,
                "cancelled_orders": 0,
            }
            exact_revenue = {}
            estimated_revenue = {}
            missing_price_orders = 0

            for order in orders:
                status = order.get("status")
                if status == "pending":
                    counts["pending_orders"] += 1
                elif status == "processing":
                    counts["processing_orders"] += 1
                elif status == "delivered":
                    counts["delivered_orders"] += 1
                elif status == "cancelled":
                    counts["cancelled_orders"] += 1

                if status != "delivered":
                    continue

                try:
                    quantity = float(order.get("quantity") or 0)
                    if quantity <= 0:
                        raise ValueError("invalid quantity")
                    if order.get("total_price") is not None:
                        amount = float(order["total_price"])
                        currency = order.get("currency") or "YER"
                        target = exact_revenue
                    else:
                        key = (
                            str(order.get("business_id") or ""),
                            str(order.get("product_name") or "").strip().casefold(),
                        )
                        product = product_prices.get(key)
                        if not product or product.get("price") is None:
                            missing_price_orders += 1
                            continue
                        amount = float(product["price"]) * quantity
                        currency = product.get("currency") or "YER"
                        target = estimated_revenue
                    target[currency] = round(target.get(currency, 0) + amount, 2)
                except (TypeError, ValueError):
                    missing_price_orders += 1

            return {
                "total_orders": len(orders),
                **counts,
                "exact_revenue": exact_revenue,
                "estimated_revenue": estimated_revenue,
                "missing_price_orders": missing_price_orders,
                "limit": 1000,
            }
        except Exception:
            logger.exception("تعذر إنشاء تقرير المبيعات")
            raise HTTPException(
                status_code=500,
                detail="تعذر إنشاء تقرير المبيعات. تحقق من جداول الطلبات والمنتجات.",
            )

    @router.patch("/api/admin/orders/{order_id}/status")
    def update_status(
        order_id: str,
        payload: StatusUpdate,
        business_id: str,
        _admin=Depends(check_admin),
    ):
        ensure_business_access(business_id, _admin)
        get_business_or_404(business_id)
        if payload.status not in STATUSES:
            raise HTTPException(
                status_code=400,
                detail="حالة غير مسموح بها",
            )

        try:
            # قراءة الطلب وحالته السابقة ووجهة إشعار Telegram.
            existing_result = (
                supabase.table("orders")
                .select(
                    "id, order_number, customer_name, product_name, "
                    "status, telegram_chat_id"
                )
                .eq("id", order_id)
                .eq("business_id", business_id)
                .limit(1)
                .execute()
            )

            if not existing_result.data:
                raise HTTPException(
                    status_code=404,
                    detail="الطلب غير موجود",
                )

            existing = existing_result.data[0]
            old_status = existing.get("status")

            # تحديث حالة الطلب في قاعدة البيانات أولًا.
            updated_result = (
                supabase.table("orders")
                .update({"status": payload.status})
                .eq("id", order_id)
                .eq("business_id", business_id)
                .select("*")
                .execute()
            )

            if not updated_result.data:
                raise HTTPException(
                    status_code=404,
                    detail="تعذر تحديث الطلب أو أن الطلب غير موجود",
                )

            updated = updated_result.data[0]
            notification_sent = False

            # لا نرسل إشعارًا إذا لم تتغير الحالة فعليًا.
            if old_status != payload.status:
                notification_sent = send_telegram_status_notification(
                    existing.get("telegram_chat_id"),
                    updated,
                )

            return {
                "success": True,
                "order": updated,
                "notification_sent": notification_sent,
            }

        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر تحديث حالة الطلب")
            raise HTTPException(
                status_code=500,
                detail=(
                    "تعذر تحديث حالة الطلب. تحقق من إعدادات قاعدة البيانات "
                    "ووجود عمود telegram_chat_id في جدول orders."
                ),
            )

    return router
