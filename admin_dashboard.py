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
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class MerchantAccountCreate(BaseModel):
    business_id: str
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=256)


class MerchantSignup(BaseModel):
    store_name: str = Field(min_length=2, max_length=160)
    industry: str = Field(default="", max_length=100)
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=256)


class SignupReview(BaseModel):
    action: str
    reason: str = Field(default="", max_length=500)


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


def _issue_merchant_token(username: str, business_id: str):
    secret = _merchant_session_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="إعداد MERCHANT_SESSION_SECRET غير مكتمل في Render",
        )
    payload = {
        "username": username,
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
        if not payload.get("username") or not payload.get("business_id"):
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
                    .select("username, business_id, is_active")
                    .eq("username", payload["username"].lower())
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
                "username": payload["username"].lower(),
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
        username = payload.username.strip().lower()
        if not username:
            raise HTTPException(status_code=401, detail="اسم المستخدم أو كلمة المرور غير صحيحة")
        try:
            result = (
                supabase.table("merchant_accounts")
                .select("username, business_id, password_hash, is_active")
                .eq("username", username)
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
            raise HTTPException(status_code=401, detail="اسم المستخدم أو كلمة المرور غير صحيحة")
        token = _issue_merchant_token(username, str(account["business_id"]))
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": MERCHANT_SESSION_SECONDS,
            "business_id": str(account["business_id"]),
        }

    @router.post("/api/merchant/signup")
    def submit_merchant_signup(payload: MerchantSignup):
        store_name = payload.store_name.strip()
        username = payload.username.strip().lower()
        industry = payload.industry.strip()
        if len(store_name) < 2:
            raise HTTPException(status_code=400, detail="أدخل اسم متجر صحيحًا.")
        if (
            not username
            or any(ch.isspace() for ch in username)
            or not username.replace("_", "").replace("-", "").replace(".", "").isalnum()
        ):
            raise HTTPException(
                status_code=400,
                detail="اسم المستخدم يجب أن يحتوي على أحرف أو أرقام أو _ أو - أو . دون مسافات.",
            )
        try:
            existing = (
                supabase.table("merchant_accounts")
                .select("id")
                .eq("username", username)
                .limit(1)
                .execute()
            )
            pending = (
                supabase.table("merchant_signup_requests")
                .select("id")
                .eq("username", username)
                .eq("status", "pending")
                .limit(1)
                .execute()
            )
            if existing.data or pending.data:
                raise HTTPException(
                    status_code=409,
                    detail="اسم المستخدم مستخدم بالفعل أو لديه طلب قيد المراجعة. اختر اسمًا آخر.",
                )
            result = (
                supabase.table("merchant_signup_requests")
                .insert({
                    "store_name": store_name,
                    "industry": industry,
                    "username": username,
                    "password_hash": _password_hash(payload.password),
                    "status": "pending",
                })
                .select("id, store_name, username, status, created_at")
                .execute()
            )
            if not result.data:
                raise HTTPException(status_code=500, detail="لم نتمكن من حفظ طلب التسجيل.")
            return {
                "success": True,
                "request": result.data[0],
                "message": "تم استلام طلبك. لن تتمكن من الدخول حتى يراجع المدير العام الطلب ويوافق عليه.",
            }
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر حفظ طلب تسجيل تاجر.")
            raise HTTPException(
                status_code=503,
                detail="تعذر إرسال الطلب. تأكد من تنفيذ ترحيل merchant signup في Supabase ثم حاول مجددًا.",
            )

    @router.get("/api/admin/merchant-signup-requests")
    def list_merchant_signup_requests(actor=Depends(check_admin)):
        if actor.get("role") != "admin":
            raise HTTPException(status_code=403, detail="هذه الصفحة متاحة للمدير العام فقط.")
        try:
            result = (
                supabase.table("merchant_signup_requests")
                .select("id, store_name, industry, username, status, rejection_reason, business_id, created_at, reviewed_at")
                .order("created_at", desc=True)
                .limit(300)
                .execute()
            )
            return {"requests": result.data or []}
        except Exception:
            logger.exception("تعذر تحميل طلبات تسجيل التجار.")
            raise HTTPException(
                status_code=503,
                detail="تعذر تحميل طلبات التسجيل. تحقق من تنفيذ ملف SQL الخاص بطلبات التجار.",
            )

    @router.patch("/api/admin/merchant-signup-requests/{request_id}")
    def review_merchant_signup_request(
        request_id: str,
        payload: SignupReview,
        actor=Depends(check_admin),
    ):
        if actor.get("role") != "admin":
            raise HTTPException(status_code=403, detail="مراجعة طلبات التجار متاحة للمدير العام فقط.")
        if payload.action not in ("approve", "reject"):
            raise HTTPException(status_code=400, detail="الإجراء يجب أن يكون approve أو reject.")
        try:
            request_result = (
                supabase.table("merchant_signup_requests")
                .select("id, store_name, industry, username, password_hash, status")
                .eq("id", request_id)
                .limit(1)
                .execute()
            )
            if not request_result.data:
                raise HTTPException(status_code=404, detail="طلب التسجيل غير موجود.")
            signup = request_result.data[0]
            if signup.get("status") != "pending":
                raise HTTPException(status_code=409, detail="تمت مراجعة هذا الطلب مسبقًا.")
            if payload.action == "reject":
                supabase.table("merchant_signup_requests").update({
                    "status": "rejected",
                    "rejection_reason": payload.reason.strip(),
                    "reviewed_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                }).eq("id", request_id).eq("status", "pending").execute()
                return {"success": True, "status": "rejected"}

            if not _merchant_session_secret():
                raise HTTPException(
                    status_code=503,
                    detail="أضف MERCHANT_SESSION_SECRET إلى Render قبل الموافقة على حسابات التجار.",
                )
            business_id = None
            account_id = None
            try:
                business_result = (
                    supabase.table("businesses")
                    .insert({
                        "name": signup["store_name"],
                        "industry": signup.get("industry") or "",
                        "description": "",
                    })
                    .select("id")
                    .execute()
                )
                if not business_result.data:
                    raise RuntimeError("Business insert returned no row")
                business_id = str(business_result.data[0]["id"])
                account_result = (
                    supabase.table("merchant_accounts")
                    .insert({
                        "business_id": business_id,
                        "username": signup["username"],
                        "password_hash": signup["password_hash"],
                        "is_active": True,
                    })
                    .select("id")
                    .execute()
                )
                if not account_result.data:
                    raise RuntimeError("Merchant account insert returned no row")
                account_id = account_result.data[0]["id"]
                updated_request = (
                    supabase.table("merchant_signup_requests")
                    .update({
                        "status": "approved",
                        "business_id": business_id,
                        "reviewed_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                        "rejection_reason": "",
                    })
                    .eq("id", request_id)
                    .eq("status", "pending")
                    .execute()
                )
                if not updated_request.data:
                    raise RuntimeError("Signup request status could not be updated")
            except Exception:
                logger.exception("تعذر إكمال الموافقة على طلب التاجر؛ جارٍ التراجع عن السجلات المنشأة.")
                try:
                    if account_id:
                        supabase.table("merchant_accounts").delete().eq("id", account_id).execute()
                    if business_id:
                        supabase.table("businesses").delete().eq("id", business_id).execute()
                except Exception:
                    logger.exception("تعذر التراجع الكامل عن سجل الموافقة.")
                raise HTTPException(
                    status_code=500,
                    detail="لم تكتمل الموافقة ولم يتم تأكيد تفعيل الحساب. تحقق من مخطط جدول businesses وmerchant_accounts ثم أعد المحاولة.",
                )
            return {
                "success": True,
                "status": "approved",
                "business_id": business_id,
                "username": signup["username"],
            }
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر مراجعة طلب تسجيل التاجر.")
            raise HTTPException(status_code=500, detail="تعذر تحديث حالة طلب التسجيل.")

    @router.get("/signup", response_class=HTMLResponse)
    def merchant_signup_page():
        return HTMLResponse(r"""
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>طلب إنشاء متجر جديد</title>
<style>
:root{font-family:"Segoe UI",Tahoma,Arial,sans-serif;color:#182338;background:#f4f7fc}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:18px;background:radial-gradient(circle at top right,#e4eaff,transparent 40%),#f4f7fc}
.card{width:min(100%,520px);padding:30px;background:#fff;border:1px solid #e2e8f4;border-radius:24px;box-shadow:0 18px 55px #203b7912}
.mark{display:grid;place-items:center;width:56px;height:56px;background:#edf1ff;color:#3157d5;border-radius:18px;font-size:26px;margin-bottom:16px}
h1{font-size:27px;margin:0 0 8px}p{color:#68758c;line-height:1.8;margin:0 0 20px}
label{display:block;font-weight:650;font-size:14px;margin:13px 0 6px}
input{width:100%;padding:12px 14px;border:1px solid #d5deed;border-radius:11px;font:inherit}
input:focus{outline:3px solid #3157d51a;border-color:#3157d5}
button{width:100%;padding:13px;border:0;border-radius:11px;background:#3157d5;color:#fff;font:inherit;font-weight:700;cursor:pointer;margin-top:18px}
button:disabled{opacity:.6;cursor:wait}
.note{font-size:13px;background:#f5f7fc;border:1px solid #e6ebf5;padding:12px;border-radius:11px;margin-top:16px}
#result{margin-top:15px;padding:12px;border-radius:10px;white-space:pre-wrap;display:none}
#result.ok{display:block;background:#eaf8f1;color:#176744}#result.error{display:block;background:#fff0ef;color:#a12620}
a{color:#3157d5}
</style>
</head>
<body>
<main class="card">
<div class="mark" aria-hidden="true">▦</div>
<h1>اطلب إنشاء متجرك</h1>
<p>أنشئ طلبًا بحسابك وبيانات متجرك. سيقوم المدير العام بمراجعة الطلب قبل تفعيل الدخول إلى لوحة الإدارة.</p>
<form id="signupForm">
<label for="storeName">اسم المتجر</label><input id="storeName" required minlength="2" maxlength="160" autocomplete="organization" placeholder="مثال: متجر التقنية الذكية">
<label for="industry">مجال النشاط (اختياري)</label><input id="industry" maxlength="100" placeholder="ملابس، إلكترونيات، أدوات منزلية...">
<label for="username">اسم المستخدم</label><input id="username" required minlength="3" maxlength="80" pattern="[A-Za-z0-9_.-]+" autocomplete="username" placeholder="shop_ahmed">
<label for="password">كلمة المرور</label><input id="password" type="password" required minlength="12" maxlength="256" autocomplete="new-password" placeholder="12 حرفًا على الأقل">
<label for="confirmPassword">تأكيد كلمة المرور</label><input id="confirmPassword" type="password" required minlength="12" maxlength="256" autocomplete="new-password" placeholder="أعد كتابة كلمة المرور">
<button id="submitBtn" type="submit">إرسال طلب التسجيل</button>
</form>
<div id="result" role="status"></div>
<div class="note">بعد الإرسال، انتظر موافقة المدير العام. لا تشارك كلمة المرور مع أي شخص. <a href="/admin">العودة إلى تسجيل الدخول</a></div>
</main>
<script>
const form=document.getElementById("signupForm");
form.addEventListener("submit",async event=>{
 event.preventDefault();
 const result=document.getElementById("result");
 const button=document.getElementById("submitBtn");
 const password=document.getElementById("password").value;
 const confirm=document.getElementById("confirmPassword").value;
 result.className="";result.style.display="none";
 if(password!==confirm){result.textContent="كلمتا المرور غير متطابقتين.";result.className="error";return;}
 button.disabled=true;button.textContent="جارٍ إرسال الطلب...";
 try{
  const response=await fetch("/api/merchant/signup",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({store_name:document.getElementById("storeName").value.trim(),industry:document.getElementById("industry").value.trim(),username:document.getElementById("username").value.trim(),password})});
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(data.detail||"تعذر إرسال الطلب.");
  result.textContent="تم استلام طلبك بنجاح. رقم الطلب: "+data.request.id+"\nحالة الطلب: قيد المراجعة. لن يعمل تسجيل الدخول حتى يوافق المدير العام.";
  result.className="ok";form.reset();
 }catch(error){result.textContent=error.message||"حدث خطأ أثناء إرسال الطلب.";result.className="error";}
 finally{button.disabled=false;button.textContent="إرسال طلب التسجيل";}
});
</script>
</body>
</html>
""")

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
        username = payload.username.strip().lower()
        if not username or any(ch.isspace() for ch in username):
            raise HTTPException(status_code=400, detail="اسم المستخدم مطلوب ولا يجوز أن يحتوي على مسافات")
        try:
            result = (
                supabase.table("merchant_accounts")
                .insert({
                    "business_id": payload.business_id,
                    "username": username,
                    "password_hash": _password_hash(payload.password),
                    "is_active": True,
                })
                .select("id, business_id, username, is_active, created_at")
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
                detail="تعذر إنشاء الحساب. قد يكون اسم المستخدم مستخدمًا مسبقًا أو أن جدول حسابات التجار غير مُعد.",
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
:root{--ink:#182338;--muted:#66758b;--brand:#3157d5;--brand-dark:#243fa8;--line:#e4eaf3;--surface:#fff;--canvas:#f5f7fc;--success:#0d8057;--danger:#b42318}
*{box-sizing:border-box}
body{font-family:"Segoe UI",Tahoma,Arial,sans-serif;background:linear-gradient(180deg,#edf2ff 0,#f7f9fd 260px,var(--canvas) 100%);margin:0;padding:24px;color:var(--ink);line-height:1.65}
main{max-width:1440px;margin:auto}
h2{margin:0 0 6px;font-size:clamp(24px,3vw,34px);letter-spacing:-.5px}
h3{margin:0 0 12px;font-size:21px}
h4{margin:0 0 10px}
p{color:var(--muted);margin:7px 0 14px}
button,select,input,textarea{font:inherit;padding:11px 13px;border:1px solid #d4ddec;border-radius:10px}
button{cursor:pointer;background:var(--brand);color:#fff;border:1px solid transparent;font-weight:650;transition:background .18s,transform .18s}
button:hover{background:var(--brand-dark);transform:translateY(-1px)}
button:disabled{opacity:.5;cursor:wait;transform:none}
input,textarea,select{width:100%;background:#fff;color:var(--ink);outline:none}
input:focus,textarea:focus,select:focus{border-color:#7b94f2;box-shadow:0 0 0 3px #3157d51c}
label{display:block;margin:10px 0 5px;font-size:14px;font-weight:650;color:#34425a}
.page-head{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:22px 24px;background:linear-gradient(120deg,#202f78,#3157d5 65%,#6685ef);border-radius:22px;color:#fff;box-shadow:0 14px 34px #2945a51c}
.page-head p{color:#e2e9ff;margin:4px 0 0}
.brand-mark{display:flex;align-items:center;gap:14px}
.brand-icon{display:grid;place-items:center;width:54px;height:54px;border-radius:17px;background:#ffffff22;border:1px solid #ffffff35;font-size:26px}
.head-actions{display:flex;gap:9px;align-items:center}
.head-actions button{background:#ffffff1c;border-color:#ffffff55}
.head-actions button:hover{background:#ffffff30}
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:14px 0}
.toolbar select{max-width:100%;width:auto;min-width:210px}
.toolbar label{margin:0}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0 14px;padding:7px;background:#e9eef9;border:1px solid #e1e8f5;border-radius:14px}
.tabs button{background:transparent;color:#53627c;border:0;padding:12px 18px}
.tabs button:hover{background:#fff;color:var(--brand);transform:none}
.tabs button.active{background:#fff;color:var(--brand);box-shadow:0 3px 12px #1b347512}
.panel{background:var(--surface);border:1px solid var(--line);box-shadow:0 5px 20px #1f355708;border-radius:18px;padding:22px;margin-bottom:18px}
.toolbar .secondary,.secondary{background:#eef2ff;color:#2e49b3;border:1px solid #dce4ff}
.toolbar .secondary:hover,.secondary:hover{background:#dfe7ff}
.danger{background:#b42318}
.status-on{color:var(--success);font-weight:700}
.status-off{color:var(--danger);font-weight:700}
.hidden{display:none!important}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:18px 0}
.stat-card{background:linear-gradient(145deg,#fff,#f9fbff);border:1px solid var(--line);border-radius:16px;padding:19px;min-width:0}
.stat-card span{display:block;color:var(--muted);font-size:14px;margin-bottom:8px}
.stat-card strong{display:block;font-size:clamp(24px,3vw,32px);color:var(--brand);line-height:1.2}
.search-box{min-width:250px;flex:1}
#orderSearch{width:100%}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:13px;background:#fff}
table{width:100%;border-collapse:collapse;min-width:850px}
th,td{padding:13px 12px;border-bottom:1px solid #edf0f6;text-align:right;vertical-align:middle}
th{background:#f4f7fc;color:#4b5b75;font-size:13px;font-weight:750;white-space:nowrap}
tbody tr:hover{background:#fafbff}
td select{max-width:180px;background:white}
#msg{margin:14px 0;padding:12px 15px;background:#eef4ff;color:#314c9c;border:1px solid #dce7ff;border-radius:12px;white-space:pre-wrap}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.full{grid-column:1/-1}
.form-actions{display:flex;gap:9px;margin-top:16px;flex-wrap:wrap}
#telegramCustomerLink a{color:var(--brand);overflow-wrap:anywhere}
#telegramCustomerLink{font-size:14px}
.sub-panel{background:#f8faff;border:1px solid #e6ecf8;border-radius:15px;padding:18px;margin:16px 0}
#productForm{margin-top:10px}
.login-overlay{position:fixed;inset:0;z-index:1000;display:grid;place-items:center;padding:18px;background:#111b34a8;backdrop-filter:blur(7px)}
.login-card{width:min(100%,440px);background:#fff;border:1px solid #e5eaf4;border-radius:24px;padding:28px;box-shadow:0 24px 80px #0b173d40}
.login-logo{width:58px;height:58px;border-radius:18px;display:grid;place-items:center;background:#edf1ff;color:var(--brand);font-size:28px;margin-bottom:14px}
.login-card h2{font-size:25px}
.login-card p{margin-bottom:18px}
.login-card button{width:100%;margin-top:18px;padding:13px}
.login-error{color:var(--danger);font-size:14px;margin-top:12px;min-height:20px}
.login-foot{font-size:12px;color:#7b879b;text-align:center;margin-top:15px}
@media(max-width:800px){body{padding:12px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.page-head{padding:18px;align-items:flex-start}.head-actions{flex-direction:column}}
@media(max-width:560px){body{padding:8px}.page-head{border-radius:16px;flex-direction:column}.brand-icon{width:45px;height:45px}.tabs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px}.tabs button{padding:10px 5px;font-size:13px}.grid{grid-template-columns:1fr}.full{grid-column:auto}.panel{padding:14px;border-radius:14px}.stats{gap:8px}.stat-card{padding:13px}.toolbar select{width:100%}.search-box{min-width:100%}.login-card{padding:22px}}
</style>
</head>
<body>
<main>
<header class="page-head">
  <div class="brand-mark">
    <div class="brand-icon" aria-hidden="true">▦</div>
    <div><h2>إدارة المتجر</h2><p>مساحة عملك لإدارة الطلبات والمنتجات والمبيعات من مكان واحد.</p></div>
  </div>
  <div class="head-actions"><button type="button" onclick="openLogin()" id="accountBtn">تسجيل الدخول</button><button type="button" onclick="logout()" id="logoutBtn" class="hidden">تسجيل الخروج</button></div>
</header>

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
<button id="requestsTab" class="hidden"
onclick="showTab('requests')">طلبات التجار</button>
</div>

<div id="msg" role="status">أهلًا بك. سجّل الدخول لعرض بيانات متجرك.</div>

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
<div class="sub-panel">
  <strong>رابط العملاء لفتح مساعد المتجر في Telegram</strong>
  <p>انسخ هذا الرابط وضعه في وصف قناة المنتجات أو في منشور مثبت. يجب أن يضغط العميل عليه ثم يضغط Start في البوت.</p>
  <p id="telegramCustomerLink">اختر النشاط التجاري لعرض الرابط.</p>
  <button type="button" class="secondary" onclick="copyTelegramLink()">نسخ رابط العملاء</button>
</div>

<div class="sub-panel">
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

<section id="requestsPanel" class="panel hidden">
  <h3>طلبات إنشاء المتاجر</h3>
  <p>راجع معلومات النشاط واسم المستخدم قبل تفعيل حساب التاجر.</p>
  <div class="toolbar"><button id="refreshRequestsBtn" onclick="loadSignupRequests()">تحديث الطلبات</button></div>
  <div class="wrap"><table style="min-width:760px">
    <thead><tr><th>المتجر</th><th>المجال</th><th>اسم المستخدم</th><th>تاريخ الطلب</th><th>الحالة</th><th>الإجراء</th></tr></thead>
    <tbody id="signupRequestRows"></tbody>
  </table></div>
</section>
</main>

<div id="loginOverlay" class="login-overlay hidden" role="dialog" aria-modal="true" aria-labelledby="loginTitle">
  <form id="loginForm" class="login-card">
    <div class="login-logo" aria-hidden="true">▦</div>
    <h2 id="loginTitle">مرحبًا بعودتك</h2>
    <p>سجّل الدخول للانتقال إلى لوحة إدارة متجرك.</p>
    <label for="loginType">نوع الحساب</label>
    <select id="loginType">
      <option value="admin">المدير العام</option>
      <option value="merchant">التاجر</option>
    </select>
    <label for="loginUsername">اسم المستخدم</label>
    <input id="loginUsername" autocomplete="username" required minlength="3" placeholder="أدخل اسم المستخدم">
    <label for="loginPassword">كلمة المرور</label>
    <input id="loginPassword" type="password" autocomplete="current-password" required placeholder="أدخل كلمة المرور">
    <div id="loginError" class="login-error" role="alert"></div>
    <button id="loginSubmit" type="submit">دخول إلى لوحة الإدارة</button>
    <div class="login-foot">يتم عرض بيانات المتجر بحسب صلاحية الحساب.<br><a href="/signup" target="_blank" rel="noopener noreferrer">طلب إنشاء متجر جديد</a></div>
  </form>
</div>

<script>
const labels = {
  pending: "قيد المراجعة",
  approved: "تمت الموافقة",
  processing: "قيد التجهيز",
  shipped: "قيد التوصيل",
  delivered: "تم التسليم",
  cancelled: "ملغي"
};

let auth = sessionStorage.getItem("store_admin_auth") || "";
let isMerchantSession = sessionStorage.getItem("store_admin_role") === "merchant";
let loginResolver = null;
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
    isMerchantSession = false;
    sessionStorage.removeItem("store_admin_auth");
    sessionStorage.removeItem("store_admin_role");
    document.getElementById("logoutBtn").classList.add("hidden");
    document.getElementById("accountBtn").classList.remove("hidden");
    openLogin();
    throw Error("انتهت الجلسة أو رُفضت بيانات الدخول. سجّل الدخول مجددًا.");
  }

  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw Error(
      data.detail || "حدث خطأ أثناء الاتصال بالخادم."
    );
  }

  return data;
}

function openLogin() {
  document.getElementById("loginError").textContent = "";
  document.getElementById("loginOverlay").classList.remove("hidden");
  document.getElementById("loginUsername").focus();
}

function closeLogin() {
  document.getElementById("loginOverlay").classList.add("hidden");
}

function login() {
  openLogin();
  return new Promise(resolve => { loginResolver = resolve; });
}

document.getElementById("loginForm").addEventListener("submit", async event => {
  event.preventDefault();
  const submit = document.getElementById("loginSubmit");
  const errorBox = document.getElementById("loginError");
  const type = document.getElementById("loginType").value;
  const username = document.getElementById("loginUsername").value.trim();
  const password = document.getElementById("loginPassword").value;
  errorBox.textContent = "";
  submit.disabled = true;
  submit.textContent = "جارٍ التحقق...";
  try {
    if (type === "merchant") {
      const response = await fetch("/api/merchant/login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({username, password})
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw Error(data.detail || "تعذر تسجيل الدخول.");
      auth = "Bearer " + data.access_token;
      isMerchantSession = true;
    } else {
      auth = "Basic " + btoa(unescape(encodeURIComponent(username + ":" + password)));
      isMerchantSession = false;
      const response = await fetch("/api/admin/businesses", {
        headers: {"Authorization": auth}
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw Error(data.detail || "اسم المستخدم أو كلمة المرور غير صحيحة.");
    }
    sessionStorage.setItem("store_admin_auth", auth);
    sessionStorage.setItem("store_admin_role", isMerchantSession ? "merchant" : "admin");
    document.getElementById("accountBtn").classList.add("hidden");
    document.getElementById("logoutBtn").classList.remove("hidden");
    closeLogin();
    document.getElementById("loginPassword").value = "";
    setMessage("تم تسجيل الدخول بنجاح. جارٍ تحميل لوحة الإدارة...");
    const resolver = loginResolver;
    loginResolver = null;
    if (!businesses.length) await loadBusinesses();
    if (resolver) resolver(true);
    else await loadOrders();
  } catch (error) {
    auth = "";
    sessionStorage.removeItem("store_admin_auth");
    sessionStorage.removeItem("store_admin_role");
    errorBox.textContent = error.message || "تعذر تسجيل الدخول.";
  } finally {
    submit.disabled = false;
    submit.textContent = "دخول إلى لوحة الإدارة";
  }
});

function logout() {
  auth = "";
  isMerchantSession = false;
  businesses = [];
  currentProducts = [];
  allOrders = [];
  sessionStorage.removeItem("store_admin_auth");
  sessionStorage.removeItem("store_admin_role");
  document.getElementById("businessSelect").replaceChildren();
  document.getElementById("productRows").replaceChildren();
  document.getElementById("orderRows").replaceChildren();
  document.getElementById("accountBtn").classList.remove("hidden");
  document.getElementById("logoutBtn").classList.add("hidden");
  setMessage("تم تسجيل الخروج. سجّل الدخول للمتابعة.");
  openLogin();
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
  document.getElementById("requestsPanel")
    .classList.toggle("hidden", tab !== "requests");

  document.getElementById("ordersTab")
    .classList.toggle("active", tab === "orders");

  document.getElementById("productsTab")
    .classList.toggle("active", tab === "products");

  document.getElementById("reportsTab")
    .classList.toggle("active", tab === "reports");
  document.getElementById("requestsTab")
    .classList.toggle("active", tab === "requests");

  if (tab === "requests") {
    loadSignupRequests();
  } else if (tab === "products") {
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
    document.getElementById("requestsTab").classList.toggle("hidden", isMerchantSession);
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

async function loadSignupRequests() {
  if (isMerchantSession) {
    setMessage("طلبات التسجيل متاحة للمدير العام فقط.");
    return;
  }
  const button = document.getElementById("refreshRequestsBtn");
  button.disabled = true;
  const rows = document.getElementById("signupRequestRows");
  rows.replaceChildren();
  try {
    if (!(await ensureLogin())) return;
    const data = await api("/api/admin/merchant-signup-requests");
    const requests = Array.isArray(data.requests) ? data.requests : [];
    if (!requests.length) {
      const tr = document.createElement("tr");
      const td = cell("لا توجد طلبات تسجيل.");
      td.colSpan = 6;
      tr.appendChild(td);
      rows.appendChild(tr);
    }
    for (const request of requests) {
      const tr = document.createElement("tr");
      tr.appendChild(cell(request.store_name));
      tr.appendChild(cell(request.industry));
      tr.appendChild(cell(request.username));
      tr.appendChild(cell(request.created_at ? new Date(request.created_at).toLocaleString("ar") : "—"));
      tr.appendChild(cell(request.status === "pending" ? "قيد المراجعة" : request.status === "approved" ? "تمت الموافقة" : "مرفوض"));
      const actionCell = document.createElement("td");
      if (request.status === "pending") {
        const approve = document.createElement("button");
        approve.textContent = "موافقة وتفعيل";
        approve.addEventListener("click", async () => {
          if (!confirm("سيتم إنشاء متجر وحساب تاجر وتفعيل الدخول. هل تريد المتابعة؟")) return;
          approve.disabled = true;
          try {
            await api("/api/admin/merchant-signup-requests/" + encodeURIComponent(request.id), {
              method: "PATCH",
              body: JSON.stringify({action: "approve"})
            });
            setMessage("تمت الموافقة وإنشاء متجر للتاجر " + request.username + ".");
            await loadSignupRequests();
            await loadBusinesses();
          } catch (error) {
            setMessage(error.message);
            approve.disabled = false;
          }
        });
        actionCell.appendChild(approve);
        const reject = document.createElement("button");
        reject.textContent = "رفض";
        reject.className = "danger";
        reject.style.marginInlineStart = "6px";
        reject.addEventListener("click", async () => {
          const reason = prompt("سبب الرفض (اختياري):") || "";
          if (!confirm("هل تريد رفض طلب هذا المتجر؟")) return;
          reject.disabled = true;
          try {
            await api("/api/admin/merchant-signup-requests/" + encodeURIComponent(request.id), {
              method: "PATCH",
              body: JSON.stringify({action: "reject", reason})
            });
            setMessage("تم رفض طلب " + request.store_name + ".");
            await loadSignupRequests();
          } catch (error) {
            setMessage(error.message);
            reject.disabled = false;
          }
        });
        actionCell.appendChild(reject);
      } else {
        actionCell.textContent = request.status === "approved" ? "تم تفعيل الحساب" : (request.rejection_reason || "لا يوجد إجراء");
      }
      tr.appendChild(actionCell);
      rows.appendChild(tr);
    }
    setMessage("تم تحميل " + requests.length + " طلب تسجيل.");
  } catch (error) {
    setMessage(error.message);
  } finally {
    button.disabled = false;
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
  const username = prompt("أنشئ اسم مستخدم للتاجر بدون مسافات (مثال: shop_ahmed):");
  if (username === null) return;
  const password = prompt("أنشئ كلمة مرور قوية للتاجر (12 حرفًا على الأقل). سلّمها له بطريقة آمنة:");
  if (password === null) return;
  if (password.length < 12) {
    setMessage("يجب أن تكون كلمة المرور 12 حرفًا على الأقل.");
    return;
  }
  try {
    const result = await api("/api/admin/merchant-accounts", {
      method: "POST",
      body: JSON.stringify({business_id: businessId, username: username.trim(), password})
    });
    setMessage("تم إنشاء حساب التاجر: " + result.account.username + ". سلّم بيانات الدخول للتاجر بطريقة آمنة.");
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

if (auth) {
  document.getElementById("accountBtn").classList.add("hidden");
  document.getElementById("logoutBtn").classList.remove("hidden");
  loadOrders();
} else {
  openLogin();
}
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
