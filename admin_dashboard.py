
import os
import hmac
import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

security = HTTPBasic()
logger = logging.getLogger(__name__)

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


def create_admin_router(supabase):
    router = APIRouter()

    def check_admin(
        credentials: HTTPBasicCredentials = Depends(security),
    ):
        username = os.getenv("ADMIN_USERNAME", "")
        password = os.getenv("ADMIN_PASSWORD", "")

        if not username or not password:
            raise HTTPException(
                status_code=503,
                detail="إعداد بيانات دخول المدير غير مكتمل",
            )

        valid_user = hmac.compare_digest(
            credentials.username, username
        )
        valid_password = hmac.compare_digest(
            credentials.password, password
        )

        if not (valid_user and valid_password):
            raise HTTPException(
                status_code=401,
                detail="بيانات الدخول غير صحيحة",
                headers={"WWW-Authenticate": "Basic"},
            )

        return True

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

<div class="tabs">
<button id="ordersTab" class="active"
onclick="showTab('orders')">الطلبات</button>
<button id="productsTab"
onclick="showTab('products')">المنتجات</button>
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

<div class="toolbar">
<label for="businessSelect">النشاط التجاري:</label>
<select id="businessSelect"></select>
<button onclick="loadProducts()">تحديث المنتجات</button>
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
  const username = prompt("اسم المستخدم الإداري");
  if (username === null) return false;

  const password = prompt("كلمة المرور الإدارية");
  if (password === null) return false;

  auth = "Basic " + btoa(
    unescape(encodeURIComponent(username + ":" + password))
  );

  try {
    await api("/api/admin/orders");
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

  document.getElementById("ordersTab")
    .classList.toggle("active", tab === "orders");

  document.getElementById("productsTab")
    .classList.toggle("active", tab === "products");

  if (tab === "products") {
    loadBusinesses();
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

    // نحمل الطلبات مرة واحدة حتى تبقى الإحصائيات مستقلة عن التصفية.
    const data = await api("/api/admin/orders");
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
        await api(
          "/api/admin/orders/" +
          encodeURIComponent(order.id) +
          "/status",
          {
            method: "PATCH",
            body: JSON.stringify({
              status: select.value
            })
          }
        );

        setMessage("تم حفظ حالة الطلب بنجاح.");
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

/* الأنشطة التجارية */
async function loadBusinesses() {
  try {
    if (!(await ensureLogin())) return;

    setMessage("جارٍ تحميل الأنشطة التجارية...");

    const data = await api("/api/admin/businesses");
    businesses = data.businesses || [];

    const select = document.getElementById("businessSelect");
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
      return;
    }

    await loadProducts();
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
    loadProducts();
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
        _admin: bool = Depends(check_admin),
    ):
        try:
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
        _admin: bool = Depends(check_admin),
    ):
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
        _admin: bool = Depends(check_admin),
    ):
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
        _admin: bool = Depends(check_admin),
    ):
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
        _admin: bool = Depends(check_admin),
        status: str = "",
    ):
        if status and status not in STATUSES:
            raise HTTPException(
                status_code=400,
                detail="حالة غير صحيحة",
            )

        query = (
            supabase.table("orders")
            .select("*")
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

    @router.patch("/api/admin/orders/{order_id}/status")
    def update_status(
        order_id: str,
        payload: StatusUpdate,
        _admin: bool = Depends(check_admin),
    ):
        if payload.status not in STATUSES:
            raise HTTPException(
                status_code=400,
                detail="حالة غير مسموح بها",
            )

        try:
            result = (
                supabase.table("orders")
                .update({"status": payload.status})
                .eq("id", order_id)
                .execute()
            )

            if not result.data:
                raise HTTPException(
                    status_code=404,
                    detail="الطلب غير موجود",
                )

            return {"success": True, "order": result.data[0]}

        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر تحديث حالة الطلب")
            raise HTTPException(
                status_code=500,
                detail="تعذر تحديث حالة الطلب",
            )

    return router
