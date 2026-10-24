import requests
import os
import hmac
import logging
import hashlib
import secrets
import base64
import json
import time
from datetime import datetime, timedelta, timezone
import csv
import io
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, Response
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


class StoreGrowthSettingsUpdate(BaseModel):
    business_id: str
    shopping_advisor_enabled: bool = True
    followups_enabled: bool = True
    followup_delay_hours: int = Field(default=24, ge=1, le=168)
    loyalty_enabled: bool = True
    points_per_1000: float = Field(default=1, ge=0, le=100000)
    point_value_yer: float = Field(default=1, gt=0, le=100000)
    minimum_redeem_points: int = Field(default=20, ge=1, le=100000000)
    repeat_purchase_threshold: int = Field(default=3, ge=2, le=100)
    repeat_purchase_bonus_points: int = Field(default=10, ge=0, le=100000000)
    coupon_expiry_days: int = Field(default=30, ge=1, le=365)


class StoreCouponCreate(BaseModel):
    business_id: str
    code: str = Field(min_length=3, max_length=40)
    title: str = Field(default="مكافأة الولاء", min_length=2, max_length=120)
    points_cost: int = Field(gt=0, le=100000000)
    discount_amount: float = Field(gt=0, le=100000000)
    currency: str = Field(default="YER", min_length=3, max_length=3)
    expiry_days: int = Field(default=30, ge=1, le=365)
    max_redemptions: int | None = Field(default=None, ge=1, le=100000000)


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
        # Only accept a JSON object with the exact identity fields needed by
        # authorization checks. Treat malformed, incomplete, or expired tokens
        # as unauthenticated rather than letting them reach route handlers.
        if not isinstance(payload, dict):
            return None
        expires_at = payload.get("exp")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int):
            return None
        if expires_at <= int(time.time()):
            return None
        username = payload.get("username")
        business_id = payload.get("business_id")
        if not isinstance(username, str) or not username.strip():
            return None
        if not isinstance(business_id, str) or not business_id.strip():
            return None
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def ensure_business_access(business_id, actor):
    """Allow admins all businesses and merchants only their assigned business."""
    if actor.get("role") == "admin":
        return
    if str(actor.get("business_id") or "") != str(business_id or ""):
        raise HTTPException(
            status_code=403,
            detail="لا تملك صلاحية الوصول إلى بيانات هذا النشاط التجاري",
        )


def products_without_recent_deliveries(products, delivered_orders):
    """Return available products with no recent delivered order, using product IDs first.

    Legacy orders without product_id are matched by name only when that name is
    unique within the current store. Ambiguous legacy matches are omitted from
    the warning list instead of incorrectly claiming those products had no sales.
    """
    products_by_name = {}
    for product in products:
        name_key = str(product.get("name") or "").strip().casefold()
        product_id = str(product.get("id") or "").strip()
        if name_key and product_id:
            products_by_name.setdefault(name_key, []).append(product_id)

    delivered_product_ids = set()
    ambiguous_legacy_names = set()
    for order in delivered_orders:
        product_id = str(order.get("product_id") or "").strip()
        if product_id:
            delivered_product_ids.add(product_id)
            continue

        name_key = str(order.get("product_name") or "").strip().casefold()
        if not name_key:
            continue
        matching_ids = products_by_name.get(name_key, [])
        if len(matching_ids) == 1:
            delivered_product_ids.add(matching_ids[0])
        elif len(matching_ids) > 1:
            ambiguous_legacy_names.add(name_key)

    result = []
    for product in products:
        if not product.get("is_available"):
            continue
        product_id = str(product.get("id") or "").strip()
        name_key = str(product.get("name") or "").strip().casefold()
        if product_id in delivered_product_ids or name_key in ambiguous_legacy_names:
            continue
        result.append(product.get("name") or "منتج دون اسم")
        if len(result) >= 10:
            break
    return result


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


class BusinessProfileUpdate(BaseModel):
    business_id: str
    name: str = Field(min_length=2, max_length=160)
    industry: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=2000)

class BusinessAssistantSettingsUpdate(BaseModel):
    business_id: str
    business_description: str = Field(default="", max_length=3000)
    business_policies: str = Field(default="", max_length=5000)
    business_hours: str = Field(default="", max_length=500)
    currency: str = Field(default="YER", min_length=3, max_length=10)


class ProductCreate(BaseModel):
    business_id: str
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    price: Decimal = Field(ge=0)
    currency: str = Field(default="YER", min_length=1, max_length=10)
    is_available: bool = True
    model_number: str = Field(default="", max_length=120)
    category: str = Field(default="", max_length=120)
    image_url: str = Field(default="", max_length=2000)
    stock_quantity: int = Field(default=0, ge=0, le=100000000)

class ProductCsvImport(BaseModel):
    business_id: str
    csv_text: str = Field(min_length=1, max_length=2_000_000)


class ProductUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    price: Decimal = Field(ge=0)
    currency: str = Field(default="YER", min_length=1, max_length=10)
    is_available: bool
    model_number: str = Field(default="", max_length=120)
    category: str = Field(default="", max_length=120)
    image_url: str = Field(default="", max_length=2000)
    stock_quantity: int = Field(default=0, ge=0, le=100000000)


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
                rejected = (
                    supabase.table("merchant_signup_requests").update({
                        "status": "rejected",
                        "rejection_reason": payload.reason.strip(),
                        "reviewed_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                    }).eq("id", request_id).eq("status", "pending").select("id").execute()
                )
                if not rejected.data:
                    raise HTTPException(status_code=409, detail="تمت مراجعة هذا الطلب من جلسة أخرى.")
                return {"success": True, "status": "rejected"}

            if not _merchant_session_secret():
                raise HTTPException(
                    status_code=503,
                    detail="أضف MERCHANT_SESSION_SECRET إلى Render قبل الموافقة على حسابات التجار.",
                )
            business_id = None
            account_id = None
            auth_user_id = None
            try:
                # businesses.owner_id is a required FK to auth.users. Create a
                # dedicated internal Auth identity for this merchant request;
                # the merchant continues to sign in with merchant_accounts.
                # The synthetic, unique address is never used for email delivery.
                internal_email = f"merchant-{request_id}@example.com"
                auth_response = supabase.auth.admin.create_user({
                    "email": internal_email,
                    "password": secrets.token_urlsafe(48),
                    "email_confirm": True,
                    "user_metadata": {
                        "internal_merchant_identity": True,
                        "merchant_username": signup["username"],
                        "signup_request_id": request_id,
                    },
                })
                auth_user = getattr(auth_response, "user", None)
                if auth_user is None and isinstance(auth_response, dict):
                    auth_user = auth_response.get("user")
                auth_user_id = (
                    auth_user.get("id") if isinstance(auth_user, dict)
                    else getattr(auth_user, "id", None)
                )
                if not auth_user_id:
                    raise RuntimeError("Supabase Auth did not return the internal user id")

                business_result = (
                    supabase.table("businesses")
                    .insert({
                        "owner_id": str(auth_user_id),
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
                    .select("id")
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
                    if auth_user_id:
                        supabase.auth.admin.delete_user(str(auth_user_id))
                except Exception:
                    logger.exception("تعذر التراجع الكامل عن سجلات وهوية الموافقة.")
                raise HTTPException(
                    status_code=500,
                    detail="لم تكتمل الموافقة. تحقق من إعداد مفتاح Supabase Service Role وصلاحيات Auth وجداول businesses وmerchant_accounts ثم أعد المحاولة.",
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

    @router.get("/store/{business_id}", response_class=HTMLResponse)
    def public_store(business_id: str):
        """Public read-only storefront; exposes only public business fields and available products."""
        import html
        import re

        if not re.fullmatch(r"[0-9a-fA-F-]{36}", business_id):
            raise HTTPException(status_code=404, detail="المتجر غير موجود")

        try:
            business_result = (
                supabase.table("businesses")
                 .select("id, name, industry, description")
                .eq("id", business_id)
                .limit(1)
                .execute()
            )
            if not business_result.data:
                raise HTTPException(status_code=404, detail="المتجر غير موجود")
            business = business_result.data[0]

            products_result = (
                supabase.table("products")
                .select("name, description, price, currency, model_number, category, image_url, stock_quantity, is_available")
                .eq("business_id", business_id)
                .eq("is_available", True)
                .order("name")
                .limit(100)
                .execute()
            )
            products = [
                product for product in (products_result.data or [])
                if product.get("stock_quantity") is None or product.get("stock_quantity", 0) > 0
            ]
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر تحميل صفحة المتجر العامة.")
            raise HTTPException(status_code=503, detail="تعذر تحميل المتجر حاليًا")

        esc = lambda value: html.escape(str(value if value is not None else ""), quote=True)
        store_name = esc(business.get("name") or "المتجر")
        industry = esc(business.get("industry") or "")
        description = esc(business.get("description") or "")
        telegram_url = "https://t.me/SmartCustomerHelp2026Bot?start=" + business_id
        cards = []
        for product in products:
            name = esc(product.get("name") or "منتج")
            detail = esc(product.get("description") or "")
            category = esc(product.get("category") or "")
            model = esc(product.get("model_number") or "")
            image_url = esc(product.get("image_url") or "")
            searchable = esc(" ".join(str(product.get(key) or "") for key in ("name", "description", "category", "model_number")))
            price = esc(product.get("price") if product.get("price") is not None else "السعر غير محدد")
            currency = esc(product.get("currency") or "YER")
            meta = []
            if category:
                meta.append(f'<span class="tag">{category}</span>')
            if model:
                meta.append(f'<span class="tag">الموديل: {model}</span>')
            image_html = (
                f'<img class="product-image" src="{image_url}" alt="{name}" loading="lazy" referrerpolicy="no-referrer">'
                if image_url.startswith(("https://", "http://")) else ""
            )
            cards.append(
                f'<article class="product" data-search="{searchable}">'
                f'{image_html}'
                f'<div class="product-top"><span class="dot"></span><span class="available">متاح للطلب</span></div>'
                f'<h2>{name}</h2>'
                f'<p class="description">{detail or "تواصل معنا لمعرفة تفاصيل المنتج."}</p>'
                f'<div class="tags">{"".join(meta)}</div>'
                f'<div class="price">{price} <small>{currency}</small></div>'
                f'<a class="order" href="{esc(telegram_url)}" target="_blank" rel="noopener noreferrer">اسأل أو اطلب عبر Telegram</a>'
                '</article>'
            )
        product_html = "".join(cards) if cards else (
            '<div class="empty"><h2>لا توجد منتجات متاحة حاليًا</h2>'
            '<p>يمكنك العودة لاحقًا للاطلاع على المنتجات الجديدة.</p></div>'
        )
        page = f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{description or store_name}">
<title>{store_name} | المتجر</title>
<style>
:root{{color-scheme:light;--ink:#172033;--muted:#64748b;--line:#e2e8f0;--accent:#0f766e;--soft:#f1f5f9}}
*{{box-sizing:border-box}}body{{margin:0;background:#f8fafc;color:var(--ink);font-family:Tahoma,Arial,sans-serif;line-height:1.7}}
header{{background:linear-gradient(135deg,#0f766e,#115e59);color:white;padding:42px 20px 52px}}
.wrap{{width:min(1080px,100% - 32px);margin:auto}}.eyebrow{{opacity:.85;font-size:14px}}h1{{font-size:clamp(28px,5vw,42px);margin:8px 0}}.intro{{max-width:680px;color:#e2e8f0;margin:0}}
main{{padding:28px 0 54px}}.section-title{{display:flex;align-items:end;justify-content:space-between;gap:12px;margin-bottom:18px}}
.section-title h2{{font-size:22px;margin:0}}.count{{color:var(--muted);font-size:14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,250px),1fr));gap:16px}}
.product,.empty{{background:white;border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:0 4px 18px #0f172a08}}
.product{{display:flex;flex-direction:column;min-height:260px;overflow:hidden}}.product-image{{width:100%;height:210px;object-fit:cover;border-radius:12px;background:#f1f5f9;margin-bottom:14px}}.product-top{{display:flex;align-items:center;gap:7px;color:var(--accent);font-size:13px}}
.dot{{width:8px;height:8px;background:#16a34a;border-radius:50%}}.product h2{{font-size:19px;margin:12px 0 5px}}.description{{color:var(--muted);font-size:14px;white-space:pre-wrap;margin:0 0 12px}}
.tags{{display:flex;flex-wrap:wrap;gap:6px}}.tag{{background:var(--soft);border-radius:7px;padding:3px 8px;color:#475569;font-size:12px}}
.price{{font-size:23px;font-weight:700;margin:18px 0 14px}}.price small{{font-size:13px;color:var(--muted);font-weight:400}}
.order{{margin-top:auto;display:block;text-align:center;text-decoration:none;background:var(--accent);color:white;border-radius:10px;padding:11px 12px;font-weight:700}}
.order:hover{{background:#115e59}}footer{{border-top:1px solid var(--line);padding:20px;text-align:center;color:var(--muted);font-size:13px}}
.empty{{grid-column:1/-1;text-align:center;padding:42px 20px}}.empty p{{color:var(--muted)}}.store-search{{width:100%;max-width:520px;padding:13px 15px;border:1px solid var(--line);border-radius:12px;margin:0 0 18px;font:inherit}}.store-search:focus{{outline:3px solid #0f766e22;border-color:#0f766e}}.no-match{{display:none;grid-column:1/-1}}
</style></head>
<body>
<header><div class="wrap"><div class="eyebrow">{industry or "المتجر الرسمي"}</div><h1>{store_name}</h1><p class="intro">{description or "تصفح المنتجات المتاحة وتواصل معنا للاستفسار أو تقديم طلب."}</p></div></header>
<main class="wrap"><div class="section-title"><h2>المنتجات المتاحة</h2><span class="count" id="productCount">{len(products)} منتج</span></div><input class="store-search" id="storeSearch" type="search" placeholder="ابحث باسم المنتج أو الموديل أو التصنيف..." aria-label="البحث في منتجات المتجر"><section class="grid" id="storeProductGrid">{product_html}<div class="empty no-match" id="noMatch"><h2>لا توجد نتائج مطابقة</h2><p>جرّب كلمة بحث أخرى أو رقم الموديل.</p></div></section></main>
<footer>للاستفسار والطلب، استخدم رابط التواصل الموجود في بطاقة المنتج.</footer>
<script>
const searchInput=document.getElementById("storeSearch");
const cards=[...document.querySelectorAll("#storeProductGrid .product")];
const count=document.getElementById("productCount");
const noMatch=document.getElementById("noMatch");
searchInput.addEventListener("input",()=>{{
  const query=searchInput.value.trim().toLocaleLowerCase();
  let visible=0;
  for(const card of cards){{
    const match=(card.dataset.search||"").toLocaleLowerCase().includes(query);
    card.hidden=!match;
    if(match) visible++;
  }}
  noMatch.style.display=visible===0?"block":"none";
  count.textContent=visible.toLocaleString("ar")+" منتج";
}});
</script>
</body></html>"""
        return HTMLResponse(content=page, headers={"Cache-Control": "no-store"})

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
.overview-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin:18px 0}
.overview-card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:0 5px 20px #1f355708;min-width:0}
.overview-card h3{font-size:17px;margin-bottom:6px}
.overview-number{font-size:30px;font-weight:800;color:var(--brand);line-height:1.3}
.overview-sub{font-size:13px;color:var(--muted);margin-top:4px}
.quick-actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px}
.quick-actions button{flex:1;min-width:135px}
.stock-list{display:grid;gap:8px;margin-top:12px}
.stock-item{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 12px;background:#f8faff;border:1px solid #e8edf7;border-radius:11px}
.stock-item strong{overflow-wrap:anywhere}
.stock-badge{white-space:nowrap;border-radius:999px;padding:3px 9px;font-size:12px;font-weight:750;background:#fff1f0;color:#b42318}
.stock-badge.ok{background:#e8f7ef;color:#087443}
.overview-table{min-width:620px}
.empty-note{padding:18px;border:1px dashed #d7e0ef;border-radius:12px;color:var(--muted);text-align:center;background:#fafcff}
@media(max-width:800px){.overview-grid{grid-template-columns:1fr}}
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
td select{max-width:220px;min-width:170px;background:white} td button{white-space:nowrap;padding:9px 13px}
#msg{margin:14px 0;padding:12px 15px;background:#eef4ff;color:#314c9c;border:1px solid #dce7ff;border-radius:12px;white-space:pre-wrap}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.full{grid-column:1/-1}
.form-actions{display:flex;gap:9px;margin-top:16px;flex-wrap:wrap}
#telegramCustomerLink a{color:var(--brand);overflow-wrap:anywhere}
#telegramCustomerLink{font-size:14px}
.sub-panel{background:#f8faff;border:1px solid #e6ecf8;border-radius:15px;padding:18px;margin:16px 0}

.product-workspace{display:grid;grid-template-columns:245px minmax(0,1fr);gap:18px;align-items:start;margin-top:18px}
.product-tree{background:#f8faff;border:1px solid #e1e8f5;border-radius:16px;padding:14px;position:sticky;top:14px}
.tree-heading{font-size:16px;font-weight:800;padding:8px 10px 13px;border-bottom:1px solid #e4eaf5;margin-bottom:10px;color:var(--ink)}
.tree-root{font-weight:750;color:#40516e;padding:10px 9px;background:#edf2ff;border-radius:9px;margin-bottom:6px}
.product-tree-item{display:flex;align-items:center;gap:9px;width:100%;margin:3px 0;padding:11px 10px;text-align:right;background:transparent;color:#53627c;border:1px solid transparent;font-weight:650}
.product-tree-item span{color:#8391aa;font-family:monospace}
.product-tree-item:hover{background:#eef2ff;color:var(--brand);transform:none}
.product-tree-item.active{background:#fff;color:var(--brand);border-color:#dce5ff;box-shadow:0 3px 12px #1b34750c}
.tree-help{font-size:12px;line-height:1.8;color:var(--muted);padding:12px 8px 4px;margin-top:8px;border-top:1px solid #e4eaf5}
.product-content{min-width:0;background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:0 5px 20px #1f355708}
.product-subsection{min-width:0}
.section-heading{display:flex;justify-content:space-between;align-items:start;gap:12px;margin-bottom:12px}
.section-heading h3{margin:0 0 5px;font-size:19px}
.section-heading p{margin:0;font-size:13px}
.product-subsection>.toolbar:first-of-type{margin-top:10px}
.product-subsection>.sub-panel{margin-top:10px}
@media(max-width:800px){.product-workspace{grid-template-columns:1fr}.product-tree{position:static}.product-tree{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px}.tree-heading,.tree-root,.tree-help{grid-column:1/-1}.product-tree-item{margin:0}.product-content{padding:14px}}
@media(max-width:460px){.product-tree{grid-template-columns:1fr}.product-tree-item{padding:10px}}

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

.dashboard-layout{display:grid;grid-template-columns:250px minmax(0,1fr);gap:18px;align-items:start;margin-top:18px}
.dashboard-tree{background:#fff;border:1px solid var(--line);border-radius:18px;padding:14px;position:sticky;top:14px;box-shadow:0 6px 24px #1f355708;min-width:0}
.dashboard-tree-title{font-size:17px;font-weight:800;padding:8px 10px 14px;border-bottom:1px solid var(--line);margin-bottom:10px}
.dashboard-tree-group{font-size:12px;font-weight:800;color:#71809a;padding:10px 9px 5px}
.dashboard-tree-item{display:flex;align-items:center;gap:10px;width:100%;text-align:right;background:transparent;color:#52617b;border:1px solid transparent;padding:11px 10px;margin:3px 0;border-radius:10px;font-weight:650;box-shadow:none}
.dashboard-tree-item span{width:19px;flex:0 0 19px;text-align:center;color:#8290a8;font-family:monospace}
.dashboard-tree-item:hover{background:#f2f5ff;color:var(--brand);transform:none}
.dashboard-tree-item.active{background:#edf1ff;border-color:#dce5ff;color:var(--brand);box-shadow:none}
.dashboard-tree-branch{border-right:1px solid #dce4f2;margin:2px 13px 8px 0;padding-right:9px}
.dashboard-tree-note{font-size:12px;line-height:1.8;color:var(--muted);border-top:1px solid var(--line);margin-top:14px;padding:12px 8px 3px}
.dashboard-main{min-width:0}
.dashboard-main>#msg{margin:0 0 14px}
.dashboard-main>#msg:empty{display:none}
.dashboard-main>.panel,.dashboard-main>section:not(#msg){min-width:0}
.dashboard-main .wrap{max-width:100%}
.dashboard-main table{width:100%}
.dashboard-tree-item span{font-family:"Segoe UI",Tahoma,Arial,sans-serif;font-size:16px}
.overview-heading{display:flex;justify-content:space-between;align-items:center;gap:16px;margin:0 0 18px;padding:5px 2px}
.overview-heading h3{font-size:25px;margin:0 0 4px}
.overview-heading p{margin:0;max-width:650px}
.overview-kicker{font-size:12px;font-weight:800;color:var(--brand);margin-bottom:4px}
.overview-section-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:24px 0 10px}
.overview-section-title h3{font-size:18px;margin:0}
.overview-section-title p{font-size:13px;margin:2px 0 0}
.overview-grid{margin:0 0 14px;gap:14px}
.overview-card{padding:19px}
.overview-card p{margin:5px 0 12px}
.overview-card .quick-actions{margin-top:12px}
.overview-number{font-size:32px}
.overview-card#merchantSetupCard{margin:0 0 20px;border-color:#d9e3ff;background:linear-gradient(135deg,#fff 0%,#f6f8ff 100%)}
#merchantSetupCard h3,#managerAssistantCard h3{font-size:19px}
#merchantSetupSteps .stock-item{background:#fff}
#managerAssistantCard{margin-top:18px}
#managerQuestionForm button{white-space:nowrap}
.dashboard-main .empty-note{line-height:1.8}
.quick-actions button{min-height:42px}
@media(max-width:900px){.dashboard-layout{grid-template-columns:210px minmax(0,1fr);gap:12px}.dashboard-tree{padding:10px}}
@media(max-width:700px){.dashboard-layout{grid-template-columns:1fr}.dashboard-tree{position:static;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px}.dashboard-tree-title,.dashboard-tree-group,.dashboard-tree-note{grid-column:1/-1}.dashboard-tree-item{margin:0;padding:10px 8px;font-size:13px}.dashboard-tree-branch{display:contents}.dashboard-main{min-width:0}.overview-heading{align-items:flex-start;flex-direction:column}.overview-heading h3{font-size:22px}}

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

<div class="dashboard-layout">
  <aside class="dashboard-tree" aria-label="القائمة الرئيسية">
    <div class="dashboard-tree-title">مساحة العمل</div>
    <div class="dashboard-tree-group">▾ إدارة المتجر</div>
    <button id="overviewTab" class="dashboard-tree-item active" onclick="showTab('overview')"><span>⌂</span> نظرة عامة</button>
    <div class="dashboard-tree-branch">
      <button id="ordersTab" class="dashboard-tree-item" onclick="showTab('orders')"><span>📦</span> الطلبات</button>
      <button id="productsTab" class="dashboard-tree-item" onclick="showTab('products')"><span>🛍️</span> المنتجات</button>
      <button id="reportsTab" class="dashboard-tree-item" onclick="showTab('reports')"><span>📈</span> تقارير المبيعات</button>
    </div>
    <div class="dashboard-tree-group">▾ إعدادات وخدمات</div>
    <button id="settingsTab" class="dashboard-tree-item" onclick="showTab('settings')"><span>⚙</span> إعدادات المتجر والمساعد</button>
    <button id="growthTab" class="dashboard-tree-item" onclick="showTab('growth')"><span>🎁</span> مستشار الشراء والولاء</button>
    <button id="requestsTab" class="dashboard-tree-item hidden" onclick="showTab('requests')"><span>👥</span> طلبات التجار</button>
    <button id="aiUsageTab" class="dashboard-tree-item hidden" onclick="showTab('ai-usage')"><span>🤖</span> استخدام الذكاء الاصطناعي</button>
    <div class="dashboard-tree-note">اختر قسمًا من القائمة لعرض بياناته وأدواته فقط.</div>
  </aside>
  <div class="dashboard-main">
    <div id="msg" role="status">أهلًا بك. سجّل الدخول لعرض بيانات متجرك.</div>

<section id="overviewPanel">
  <div class="overview-heading">
    <div><div class="overview-kicker">مساحة العمل</div><h3>نظرة عامة على متجرك</h3><p>ملخص واضح للطلبات والمنتجات والمخزون، مع أدوات مباشرة لإدارة يومك.</p></div>
    <button type="button" class="secondary" onclick="loadOverview()">↻ تحديث البيانات</button>
  </div>

  <section class="overview-card" id="merchantSetupCard">
    <h3>ابدأ متجرك خلال دقائق</h3>
    <p>أكمل بيانات المتجر وسياسات البيع وأضف منتجاتك، ثم شارك رابط المساعد مع العملاء.</p>
    <div id="merchantSetupSteps" class="stock-list"><div class="empty-note">جارٍ فحص جاهزية متجرك...</div></div>
  </section>

  <div class="overview-section-title"><div><h3>ملخص الأداء</h3><p>الأرقام تخص المتجر المحدد فقط.</p></div></div>
  <div class="overview-grid">
    <article class="overview-card"><h3>إجمالي الطلبات</h3><div class="overview-number" id="overviewOrders">—</div><div class="overview-sub">جميع الطلبات المسجلة</div><div class="quick-actions"><button type="button" onclick="showTab('orders')">عرض الطلبات</button></div></article>
    <article class="overview-card"><h3>المنتجات</h3><div class="overview-number" id="overviewProducts">—</div><div class="overview-sub" id="overviewProductSummary">المنتجات المسجلة في متجرك</div><div class="quick-actions"><button type="button" onclick="showTab('products')">إدارة المنتجات</button></div></article>
    <article class="overview-card"><h3>تحتاج متابعة</h3><div class="overview-number" id="overviewAttention">—</div><div class="overview-sub">طلبات قيد المراجعة أو التجهيز</div><div class="quick-actions"><button type="button" class="secondary" onclick="showTab('orders')">متابعة الطلبات</button></div></article>
    <article class="overview-card"><h3>تنبيهات المخزون</h3><div class="overview-number" id="overviewLowStockCount">—</div><div class="overview-sub">منتجات نفدت أو قاربت على النفاد</div><div class="quick-actions"><button type="button" class="secondary" onclick="showTab('products')">مراجعة المخزون</button></div></article>
  </div>

  <div class="overview-section-title"><div><h3>متابعة المتجر</h3><p>راجع ما يحتاج إجراءً الآن وآخر الطلبات الواردة.</p></div></div>
  <div class="overview-grid">
    <section class="overview-card"><h3>مخزون يحتاج انتباهًا</h3><p>المنتجات غير المتاحة أو التي بقي منها 3 قطع أو أقل.</p><div id="overviewStockList" class="stock-list"><div class="empty-note">جارٍ تحميل بيانات المخزون...</div></div></section>
    <section class="overview-card"><h3>أحدث الطلبات</h3><p>آخر الطلبات المسجلة لهذا المتجر.</p><div class="wrap"><table class="overview-table"><thead><tr><th>رقم الطلب</th><th>المنتج</th><th>الحالة</th><th>الكمية</th></tr></thead><tbody id="overviewRecentOrders"><tr><td colspan="4">جارٍ تحميل أحدث الطلبات...</td></tr></tbody></table></div></section>
  </div>

  <section class="overview-card" id="managerAssistantCard">
    <h3>مدير المتجر الذكي</h3>
    <p>تنبيهات وتوصيات مبنية على بيانات متجرك الفعلية. المبيعات ليست صافي ربح؛ حساب الربح يحتاج تكلفة البضاعة والمصاريف.</p>
    <div id="managerInsights" class="stock-list"><div class="empty-note">جارٍ تحميل تحليلات المتجر...</div></div>
    <form id="managerQuestionForm" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
      <input id="managerQuestion" type="text" maxlength="1000" placeholder="مثال: كم مبيعات الطلبات المسلّمة هذا الأسبوع؟" style="flex:1;min-width:220px">
      <button type="submit">اسأل مدير المتجر</button>
    </form>
    <div id="managerAnswer" class="empty-note" role="status" style="margin-top:12px;white-space:pre-wrap">اكتب سؤالك عن المبيعات أو الطلبات أو المخزون.</div>
  </section>

  <section class="overview-card">
    <h3>إجراءات سريعة</h3><p>اختصارات للمهام اليومية الأكثر استخدامًا.</p>
    <div class="quick-actions"><button type="button" onclick="showTab('products')">＋ إضافة منتج</button><button type="button" class="secondary" onclick="showTab('reports')">عرض تقارير المبيعات</button><button type="button" class="secondary" onclick="showTab('settings')">إعدادات المتجر</button></div>
  </section>
</section>

<section id="ordersPanel" class="panel hidden">
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
<p id="productsIntro">إدارة منتجات النشاط التجاري المحدد.</p>


<div class="product-workspace">
  <aside class="product-tree" aria-label="أقسام المنتجات">
    <div class="tree-heading">إدارة المنتجات</div>
    <div class="tree-root">▸ كتالوج المتجر</div>
    <button type="button" class="product-tree-item active" data-product-section="catalog" onclick="showProductSection('catalog')"><span>├</span> المنتجات المسجلة</button>
    <button type="button" class="product-tree-item" data-product-section="editor" onclick="resetProductForm();showProductSection('editor')"><span>├</span> إضافة منتج جديد</button>
    <button type="button" class="product-tree-item" data-product-section="import" onclick="showProductSection('import')"><span>├</span> استيراد الكتالوج</button>
    <button type="button" class="product-tree-item" data-product-section="telegram" onclick="showProductSection('telegram')"><span>└</span> رابط العملاء</button>
    <div class="tree-help">اختر قسمًا لعرض أدواته فقط. ستبقى بيانات المتجر وأقسامه منفصلة ومنظمة.</div>
  </aside>
  <div class="product-content">
    <section id="productCatalogSection" class="product-subsection">
      <div class="section-heading"><div><h3>المنتجات المسجلة</h3><p>البحث والمراجعة وتعديل بيانات المنتجات الموجودة.</p></div></div>
      <div class="toolbar">
  <div class="search-box">
    <input id="productSearch" type="search" placeholder="ابحث باسم المنتج أو الموديل أو التصنيف" aria-label="البحث في المنتجات">
  </div>
  <button onclick="loadProducts()">تحديث المنتجات</button>
</div>
      <div class="wrap">
<table>
<thead>
<tr>
<th>المنتج</th>
<th>التصنيف / الوصف</th>
<th>المخزون</th>
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
    <section id="productEditorSection" class="product-subsection hidden">
      <div class="section-heading"><div><h3>بيانات المنتج</h3><p>أدخل بيانات المنتج أو عدّل منتجًا موجودًا.</p></div></div>
      <div class="sub-panel">
<h3 id="formTitle">إضافة منتج جديد</h3>

<form id="productForm">
<input type="hidden" id="productId">

<div class="grid">
<div><label for="productName">اسم المنتج *</label><input id="productName" maxlength="200" required></div>
<div><label for="productPrice">السعر *</label><input id="productPrice" type="number" min="0" step="0.01" required></div>
<div><label for="productCurrency">العملة *</label><input id="productCurrency" value="YER" maxlength="10" required></div>
<div><label for="productModelNumber">رقم الموديل / SKU</label><input id="productModelNumber" maxlength="120" placeholder="مثال: 4830"></div>
<div><label for="productCategory">التصنيف</label><input id="productCategory" maxlength="120" placeholder="ملابس، إلكترونيات..."></div>
<div><label for="productStock">الكمية في المخزون</label><input id="productStock" type="number" min="0" max="100000000" step="1" value="0" required></div>
<div class="full"><label for="productImageFile">رفع صورة المنتج من جهازك</label><input id="productImageFile" type="file" accept="image/jpeg,image/png,image/webp"><small>ستُرفع الصورة إلى تخزين المتجر وتظهر بجانب اسم المنتج وسعره.</small><div id="productImagePreview"></div></div><div><label for="productImageUrl">أو رابط صورة المنتج</label><input id="productImageUrl" type="url" maxlength="2000" placeholder="https://..."></div>
<div><label for="productAvailable">التوفر</label><select id="productAvailable"><option value="true">متاح للطلب</option><option value="false">غير متاح</option></select></div>
<div class="full"><label for="productDescription">وصف المنتج</label><textarea id="productDescription" rows="3" maxlength="2000"></textarea></div>
</div>

<div class="form-actions">
<button id="saveProductBtn" type="submit">حفظ المنتج</button>
<button type="button" class="secondary"
onclick="resetProductForm()">إلغاء التعديل</button>
</div>
</form>
</div>
    </section>
    <section id="productImportSection" class="product-subsection hidden">
      <div class="section-heading"><div><h3>استيراد الكتالوج</h3><p>إضافة مجموعة منتجات من جدول دفعة واحدة.</p></div></div>
      <div class="sub-panel">
  <h3>استيراد كتالوج المنتجات دفعة واحدة</h3>
  <p>الصق بيانات CSV المنسوخة من Excel أو Google Sheets. الأعمدة المطلوبة: <code>name</code> و<code>price</code>. الأعمدة الاختيارية: <code>currency, model_number, category, stock_quantity, image_url, description, is_available</code>.</p>
  <p>يمكنك لصق CSV من Excel، أو رفع PDF/صورة ليحاول الذكاء الاصطناعي استخراج أسماء المنتجات والموديلات والأسعار. راجع البيانات وعدّلها قبل الحفظ. لا تُحفظ المنتجات تلقائيًا.</p>
  <div class="sub-panel">
    <label for="catalogFile">رفع كتالوج PDF أو صورة (JPG / PNG / WEBP)</label>
    <input id="catalogFile" type="file" accept=".pdf,.png,.jpg,.jpeg,.webp,application/pdf,image/png,image/jpeg,image/webp">
    <div class="form-actions"><button id="extractCatalogBtn" type="button" onclick="extractCatalogFile()">تحليل الملف ومعاينة المنتجات</button></div>
    <p id="catalogExtractResult" role="status" aria-live="polite"></p>
    <p class="muted">للحفاظ على الدقة، ارفع PDF لا يتجاوز 20 ميجابايت وبحد أقصى 40 صفحة في العملية. الصور المستخرجة من الكتالوج لا تُربط تلقائيًا بكل منتج؛ يمكنك رفع صورة مستقلة لكل منتج من نموذج المنتج.</p>
  </div>
  <p><strong>مراجعة البيانات قبل الحفظ</strong></p>
  <p>الأعمدة: <code>name,price,currency,model_number,category,stock_quantity,image_url,description,is_available</code>. صحّح الاسم والسعر والموديل والمخزون يدويًا إذا لزم.</p>
  <textarea id="productCsvText" rows="9" placeholder="name,price,currency,model_number,category,stock_quantity,image_url,description,is_available&#10;عباية صافي,1400,YER,4830,عبايات,5,,عباية سوداء,true"></textarea>
  <div class="form-actions"><button id="importProductsBtn" type="button" onclick="importProductsCsv()">حفظ المنتجات بعد المراجعة</button><button id="undoLastImportBtn" type="button" class="danger" onclick="undoLastImport()" disabled>تراجع عن آخر استيراد</button></div>
  <p id="productImportResult" role="status" aria-live="polite"></p>
</div>
    </section>
    <section id="productTelegramSection" class="product-subsection hidden">
      <div class="section-heading"><div><h3>رابط العملاء</h3><p>رابط مباشر لفتح مساعد المتجر في Telegram.</p></div></div>
      <div class="sub-panel">
  <strong>رابط العملاء لفتح مساعد المتجر في Telegram</strong>
  <p>انسخ هذا الرابط وضعه في وصف قناة المنتجات أو في منشور مثبت. يجب أن يضغط العميل عليه ثم يضغط Start في البوت.</p>
  <p id="telegramCustomerLink">اختر النشاط التجاري لعرض الرابط.</p>
  <button type="button" class="secondary" onclick="copyTelegramLink()">نسخ رابط العملاء</button>
</div>
    </section>
  </div>
</div>
</section>

<section id="settingsPanel" class="panel hidden">
<h3>إعدادات المتجر</h3>
<p>حدّث الاسم والنشاط والوصف الذي يظهر في صفحة متجرك العامة. هذه البيانات تخص المتجر المحدد فقط.</p>
<form id="businessProfileForm">
<input type="hidden" id="profileBusinessId">
<div class="grid">
<div><label for="profileStoreName">اسم المتجر *</label><input id="profileStoreName" maxlength="160" required minlength="2"></div>
<div><label for="profileIndustry">نوع النشاط</label><input id="profileIndustry" maxlength="100" placeholder="مثل: عبايات، إلكترونيات، ملابس"></div>
<div class="full"><label for="profileDescription">نبذة عن المتجر</label><textarea id="profileDescription" rows="4" maxlength="2000" placeholder="نبذة عامة تظهر في صفحة المتجر. لا تضع معلومات سرية هنا."></textarea><small>تظهر النبذة في صفحة المتجر العامة؛ لا تضف بيانات خاصة أو معلومات دخول.</small></div>
</div>
<div class="form-actions"><button id="saveBusinessProfileBtn" type="submit">حفظ بيانات المتجر</button><button type="button" class="secondary" onclick="loadBusinessProfile()">إعادة تحميل</button></div>
</form>
<hr>
<h3>تعليمات المساعد الذكي</h3>
<p>هذه المعلومات خاصة بهذا المتجر، ويستخدمها المساعد في ردوده على العملاء. اترك أي معلومة غير معتمدة فارغة؛ لن يخمّن المساعد التفاصيل الناقصة.</p>
<form id="businessAssistantSettingsForm">
<input type="hidden" id="assistantSettingsBusinessId">
<div class="grid">
<div class="full"><label for="assistantBusinessDescription">معلومات الخدمة التي يمكن للمساعد ذكرها</label><textarea id="assistantBusinessDescription" rows="3" maxlength="3000" placeholder="الخدمات، مناطق الخدمة، طريقة الطلب، معلومات مهمة للعميل"></textarea></div>
<div class="full"><label for="assistantBusinessPolicies">سياسات الاستبدال والإرجاع والتوصيل والدفع</label><textarea id="assistantBusinessPolicies" rows="4" maxlength="5000" placeholder="مثال: مدة الاستبدال، رسوم التوصيل، طرق الدفع المتاحة، شروط الإرجاع"></textarea></div>
<div><label for="assistantBusinessHours">ساعات وأيام العمل</label><textarea id="assistantBusinessHours" rows="2" maxlength="500" placeholder="مثال: السبت–الخميس، 9 صباحًا–5 مساءً"></textarea></div>
<div><label for="assistantCurrency">العملة الافتراضية</label><select id="assistantCurrency"><option value="YER">ريال يمني (YER)</option><option value="SAR">ريال سعودي (SAR)</option><option value="USD">دولار أمريكي (USD)</option><option value="AED">درهم إماراتي (AED)</option><option value="EGP">جنيه مصري (EGP)</option></select></div>
</div>
<div class="form-actions"><button id="saveAssistantSettingsBtn" type="submit">حفظ تعليمات المساعد</button><button type="button" class="secondary" onclick="loadBusinessProfile()">إعادة تحميل الإعدادات</button></div>
</form>
</section>

<section id="reportsPanel" class="panel hidden">
<h3>تقارير المبيعات</h3>
<p>تعرض التقارير حتى 1000 طلب حديث. تُحسب المبيعات الدقيقة من إجمالي السعر المحفوظ وقت إنشاء الطلب، لكن للطلبات التي تم تسليمها فقط. إذا ظهرت «لا توجد بيانات»، فهذا يعني أنه لا توجد طلبات مسلّمة ذات إجمالي محفوظ؛ وتظهر الطلبات القديمة التي لا تملك سعرًا محفوظًا كتقدير منفصل حسب السعر الحالي للمنتج. الطلبات قيد المراجعة أو التجهيز أو التوصيل لا تُحتسب مبيعات بعد، والطلبات الملغاة تُعرض منفصلة.</p>
<div class="toolbar">
<button id="refreshReportsBtn" onclick="loadReports()">تحديث التقرير</button>
<button type="button" class="secondary" onclick="downloadSalesCsv()">تصدير الطلبات CSV</button>
</div>
<div class="stats">
  <div class="stat-card"><span>إجمالي الطلبات</span><strong id="reportTotalOrders">—</strong></div>
  <div class="stat-card"><span>قيد المراجعة</span><strong id="reportPendingOrders">—</strong></div>
  <div class="stat-card"><span>قيد التجهيز</span><strong id="reportProcessingOrders">—</strong></div>
  <div class="stat-card"><span>تم التسليم</span><strong id="reportDeliveredOrders">—</strong></div>
  <div class="stat-card"><span>الطلبات الملغاة</span><strong id="reportCancelledOrders">—</strong></div>
</div>
<div class="panel">
<h4>قيمة الطلبات التي تم تسليمها (المبيعات)</h4>
<p><strong>المبيعات المحفوظة للطلبات المسلّمة:</strong> <span id="realizedRevenue">—</span></p>
<p><strong>إجمالي القطع المسلّمة:</strong> <span id="deliveredQuantity">—</span></p>
<p>المبيعات المحفوظة هي المبالغ المسجلة وقت الطلب، ولا تشمل التقديرات أو الطلبات غير المسلّمة.</p>
<p id="exactRevenue">المبيعات الدقيقة: —</p>
<p id="estimatedRevenue">المبيعات التقديرية للطلبات القديمة: —</p>
<p id="missingRevenue">طلبات مسلّمة بلا سعر يمكن حسابه: —</p>
</div>
<div class="panel">
<h4>الطلبات الملغاة (لا تُحتسب مبيعات)</h4>
<p id="cancelledExactValue">قيمة الطلبات الملغاة المحفوظة: —</p>
<p id="cancelledEstimatedValue">تقدير الطلبات الملغاة القديمة: —</p>
<p id="cancelledMissingValue">طلبات ملغاة بلا سعر يمكن حسابه: —</p>
</div>
</section>

<section id="growthPanel" class="panel hidden">
  <h3>مستشار الشراء واستعادة العملاء والولاء</h3>
  <p>كل إعداد هنا يخص المتجر المحدد فقط. التذكيرات لا تُرسل إلا بعد موافقة العميل، ومرة واحدة لكل استفسار غير مكتمل. تُضاف نقاط الولاء عند تسليم الطلب فقط.</p>
  <form id="storeGrowthSettingsForm">
    <div class="grid">
      <div><label><input id="growthAdvisorEnabled" type="checkbox" style="width:auto"> تفعيل مستشار الشراء الذكي</label><small>يقارن المنتجات المسجلة فقط ويطلب توضيحًا عند الحاجة.</small></div>
      <div><label><input id="growthFollowupsEnabled" type="checkbox" style="width:auto"> تفعيل تذكيرات العملاء</label><small>لا يعمل دون موافقة العميل الصريحة.</small></div>
      <div><label for="growthFollowupDelay">مدة الانتظار قبل التذكير (ساعة)</label><input id="growthFollowupDelay" type="number" min="1" max="168" required></div>
      <div><label><input id="growthLoyaltyEnabled" type="checkbox" style="width:auto"> تفعيل برنامج الولاء</label><small>تُحتسب النقاط للطلبات المسلّمة فقط.</small></div>
      <div><label for="growthPointsRate">النقاط لكل 1000 من قيمة الطلب</label><input id="growthPointsRate" type="number" min="0" max="100000" step="0.1" required></div>
      <div><label for="growthPointValue">القيمة الحسابية للنقطة</label><input id="growthPointValue" type="number" min="0.0001" step="0.01" required></div>
      <div><label for="growthMinimumPoints">الحد الأدنى لاستبدال النقاط</label><input id="growthMinimumPoints" type="number" min="1" required></div>
      <div><label for="growthRepeatThreshold">عدد الطلبات المسلّمة للحصول على مكافأة التكرار</label><input id="growthRepeatThreshold" type="number" min="2" max="100" required></div>
      <div><label for="growthRepeatBonus">نقاط إضافية للعميل المتكرر</label><input id="growthRepeatBonus" type="number" min="0" required></div>
      <div><label for="growthCouponExpiry">مدة انتهاء المكافأة الافتراضية (يوم)</label><input id="growthCouponExpiry" type="number" min="1" max="365" required></div>
    </div>
    <div class="form-actions"><button type="submit">حفظ إعدادات الميزات</button><button type="button" class="secondary" onclick="loadStoreGrowth()">إعادة تحميل</button></div>
  </form>
  <hr>
  <h3>إنشاء كوبون مكافأة</h3>
  <p>حدد تكلفة النقاط وقيمة الخصم ومدة الصلاحية. رمز الكوبون فريد داخل هذا المتجر ولا يمكن استخدامه بعد انتهاء صلاحيته أو بلوغ حد الاستخدام.</p>
  <form id="storeCouponForm">
    <div class="grid">
      <div><label for="couponCode">رمز المكافأة</label><input id="couponCode" required minlength="3" maxlength="40" pattern="[A-Za-z0-9_-]+" placeholder="LOYAL100"></div>
      <div><label for="couponTitle">اسم المكافأة</label><input id="couponTitle" required minlength="2" maxlength="120" value="مكافأة الولاء"></div>
      <div><label for="couponPointsCost">النقاط المطلوبة</label><input id="couponPointsCost" type="number" min="1" required></div>
      <div><label for="couponDiscount">قيمة الخصم</label><input id="couponDiscount" type="number" min="0.01" step="0.01" required></div>
      <div><label for="couponCurrency">العملة</label><select id="couponCurrency"><option value="YER">ريال يمني (YER)</option><option value="SAR">ريال سعودي (SAR)</option><option value="USD">دولار أمريكي (USD)</option><option value="AED">درهم إماراتي (AED)</option><option value="EGP">جنيه مصري (EGP)</option></select></div>
      <div><label for="couponExpiryDays">الصلاحية (يوم)</label><input id="couponExpiryDays" type="number" min="1" max="365" required value="30"></div>
      <div><label for="couponMaxRedemptions">أقصى عدد استخدامات (اختياري)</label><input id="couponMaxRedemptions" type="number" min="1" placeholder="اتركه فارغًا بلا حد"></div>
    </div>
    <div class="form-actions"><button type="submit">إنشاء المكافأة</button></div>
  </form>
  <h3 style="margin-top:24px">مكافآت هذا المتجر</h3>
  <div class="toolbar"><button type="button" class="secondary" onclick="loadStoreCoupons()">تحديث المكافآت</button></div>
  <div class="wrap"><table style="min-width:780px"><thead><tr><th>الرمز</th><th>المكافأة</th><th>النقاط</th><th>الخصم</th><th>الاستخدام</th><th>تنتهي</th><th>الحالة</th></tr></thead><tbody id="storeCouponRows"><tr><td colspan="7">سجّل الدخول لعرض المكافآت.</td></tr></tbody></table></div>
</section>

<section id="aiUsagePanel" class="panel hidden">
  <h3>مراقبة استخدام الذكاء الاصطناعي</h3>
  <p>تُحفظ أعداد الرموز فقط، دون رسائل العملاء أو أرقامهم أو محتوى الصور. يعرض الملخص آخر 1000 طلب مسجل.</p>
  <div class="toolbar"><button id="refreshAIUsageBtn" onclick="loadAIUsage()">تحديث الاستخدام</button></div>
  <div class="stats">
    <div class="stat-card"><span>الطلبات المسجلة ضمن العينة</span><strong id="aiUsageSample">—</strong></div>
    <div class="stat-card"><span>إجمالي الرموز</span><strong id="aiUsageTokens">—</strong></div>
    <div class="stat-card"><span>التكلفة التقديرية بالدولار</span><strong id="aiUsageCost">—</strong></div>
    <div class="stat-card"><span>طلبات دون سعر مضبوط</span><strong id="aiUsageUnpriced">—</strong></div>
  </div>
  <h4>أحدث الاستخدامات</h4>
  <div class="wrap"><table style="min-width:720px">
    <thead><tr><th>التاريخ</th><th>النموذج</th><th>النوع</th><th>رموز الإدخال</th><th>رموز الإخراج</th><th>التكلفة التقديرية</th></tr></thead>
    <tbody id="aiUsageRows"></tbody>
  </table></div>
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
  </div>
</div>
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

function clearOverviewForStoreSwitch() {
  for (const id of ["overviewOrders", "overviewProducts", "overviewAttention", "overviewLowStockCount"]) {
    const element = document.getElementById(id);
    if (element) element.textContent = "—";
  }
  const summary = document.getElementById("overviewProductSummary");
  if (summary) summary.textContent = "جارٍ تحميل بيانات المتجر المحدد...";
  const placeholders = [
    ["overviewStockList", "جارٍ تحميل مخزون المتجر المحدد..."],
    ["merchantSetupSteps", "جارٍ فحص إعدادات المتجر المحدد..."],
    ["managerInsights", "جارٍ تحميل تحليلات المتجر المحدد..."]
  ];
  for (const [id, message] of placeholders) {
    const list = document.getElementById(id);
    if (!list) continue;
    list.replaceChildren();
    const note = document.createElement("div");
    note.className = "empty-note";
    note.textContent = message;
    list.appendChild(note);
  }
  const recentRows = document.getElementById("overviewRecentOrders");
  if (recentRows) {
    recentRows.replaceChildren();
    const tr = document.createElement("tr");
    const td = cell("جارٍ تحميل طلبات المتجر المحدد...");
    td.colSpan = 4;
    tr.appendChild(td);
    recentRows.appendChild(tr);
  }
  const managerAnswer = document.getElementById("managerAnswer");
  if (managerAnswer) managerAnswer.textContent = "جارٍ تحميل بيانات المتجر المحدد...";
}

function clearPrivateDashboardData() {
  businesses = [];
  currentProducts = [];
  allOrders = [];
  for (const id of [
    "businessSelect", "productRows", "orderRows", "signupRequestRows"
  ]) {
    const element = document.getElementById(id);
    if (element) element.replaceChildren();
  }
  for (const id of [
    "totalOrders", "pendingOrders", "processingOrders", "deliveredOrders",
    "reportTotalOrders", "reportPendingOrders", "reportProcessingOrders",
    "reportDeliveredOrders", "reportCancelledOrders"
  ]) {
    const element = document.getElementById(id);
    if (element) element.textContent = "0";
  }
  for (const id of ["exactRevenue", "estimatedRevenue", "missingRevenue", "cancelledExactValue", "cancelledEstimatedValue", "cancelledMissingValue"]) {
    const element = document.getElementById(id);
    if (element) element.textContent = "—";
  }
  const link = document.getElementById("telegramCustomerLink");
  if (link) link.textContent = "سجّل الدخول لعرض رابط المتجر.";
  resetProductForm();
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

  if (options.body && !(options.body instanceof FormData)) {
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
    clearPrivateDashboardData();
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
    await loadOverview();
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
  clearPrivateDashboardData();
  sessionStorage.removeItem("store_admin_auth");
  sessionStorage.removeItem("store_admin_role");
  document.getElementById("accountBtn").classList.remove("hidden");
  document.getElementById("logoutBtn").classList.add("hidden");
  setMessage("تم تسجيل الخروج. سجّل الدخول للمتابعة.");
  openLogin();
}

async function ensureLogin() {
  if (auth) return true;
  return await login();
}

function showProductSection(section) {
  const sections = {
    catalog: "productCatalogSection",
    editor: "productEditorSection",
    import: "productImportSection",
    telegram: "productTelegramSection"
  };
  Object.entries(sections).forEach(([key, id]) => {
    const panel = document.getElementById(id);
    if (panel) panel.classList.toggle("hidden", key !== section);
  });
  document.querySelectorAll("[data-product-section]").forEach(button => {
    button.classList.toggle("active", button.dataset.productSection === section);
  });
  if (section === "catalog") loadProducts();
  if (section === "telegram") updateTelegramCustomerLink();
}

function showTab(tab) {
  // Merchant sessions must never navigate to the admin-only signup requests panel.
  if ((tab === "requests" || tab === "ai-usage") && isMerchantSession) {
    tab = "orders";
  }

  document.getElementById("overviewPanel")
    .classList.toggle("hidden", tab !== "overview");
  document.getElementById("ordersPanel")
    .classList.toggle("hidden", tab !== "orders");

  document.getElementById("productsPanel")
    .classList.toggle("hidden", tab !== "products");

  document.getElementById("reportsPanel")
    .classList.toggle("hidden", tab !== "reports");
  document.getElementById("settingsPanel")
    .classList.toggle("hidden", tab !== "settings");
  document.getElementById("growthPanel")
    .classList.toggle("hidden", tab !== "growth");
  document.getElementById("requestsPanel")
    .classList.toggle("hidden", tab !== "requests");
  document.getElementById("aiUsagePanel")
    .classList.toggle("hidden", tab !== "ai-usage");

  document.getElementById("overviewTab")
    .classList.toggle("active", tab === "overview");
  document.getElementById("ordersTab")
    .classList.toggle("active", tab === "orders");

  document.getElementById("productsTab")
    .classList.toggle("active", tab === "products");

  document.getElementById("reportsTab")
    .classList.toggle("active", tab === "reports");
  document.getElementById("settingsTab")
    .classList.toggle("active", tab === "settings");
  document.getElementById("growthTab")
    .classList.toggle("active", tab === "growth");
  document.getElementById("requestsTab")
    .classList.toggle("active", tab === "requests");
  document.getElementById("aiUsageTab")
    .classList.toggle("active", tab === "ai-usage");

  if (tab === "overview") {
    loadOverview();
  } else if (tab === "requests") {
    loadSignupRequests();
  } else if (tab === "ai-usage") {
    loadAIUsage();
  } else if (tab === "products") {
    loadBusinesses();
  } else if (tab === "reports") {
    loadReports();
  } else if (tab === "settings") {
    loadBusinessProfile();
  } else if (tab === "growth") {
    loadStoreGrowth();
    loadStoreCoupons();
  } else {
    loadOrders();
  }
}

async function loadStoreGrowth() {
  const businessId = document.getElementById("businessSelect").value;
  if (!businessId) return;
  try {
    const data = await api("/api/admin/store-growth-settings?business_id=" + encodeURIComponent(businessId));
    const s = data.settings || {};
    document.getElementById("growthAdvisorEnabled").checked = Boolean(s.shopping_advisor_enabled);
    document.getElementById("growthFollowupsEnabled").checked = Boolean(s.followups_enabled);
    document.getElementById("growthFollowupDelay").value = s.followup_delay_hours ?? 24;
    document.getElementById("growthLoyaltyEnabled").checked = Boolean(s.loyalty_enabled);
    document.getElementById("growthPointsRate").value = s.points_per_1000 ?? 1;
    document.getElementById("growthPointValue").value = s.point_value_yer ?? 1;
    document.getElementById("growthMinimumPoints").value = s.minimum_redeem_points ?? 20;
    document.getElementById("growthRepeatThreshold").value = s.repeat_purchase_threshold ?? 3;
    document.getElementById("growthRepeatBonus").value = s.repeat_purchase_bonus_points ?? 10;
    document.getElementById("growthCouponExpiry").value = s.coupon_expiry_days ?? 30;
    document.getElementById("couponExpiryDays").value = s.coupon_expiry_days ?? 30;
  } catch (error) {
    setMessage(error.message || "تعذر تحميل إعدادات الميزات.");
  }
}

document.getElementById("storeGrowthSettingsForm").addEventListener("submit", async event => {
  event.preventDefault();
  const businessId = document.getElementById("businessSelect").value;
  if (!businessId) return;
  const payload = {
    business_id: businessId,
    shopping_advisor_enabled: document.getElementById("growthAdvisorEnabled").checked,
    followups_enabled: document.getElementById("growthFollowupsEnabled").checked,
    followup_delay_hours: Number(document.getElementById("growthFollowupDelay").value),
    loyalty_enabled: document.getElementById("growthLoyaltyEnabled").checked,
    points_per_1000: Number(document.getElementById("growthPointsRate").value),
    point_value_yer: Number(document.getElementById("growthPointValue").value),
    minimum_redeem_points: Number(document.getElementById("growthMinimumPoints").value),
    repeat_purchase_threshold: Number(document.getElementById("growthRepeatThreshold").value),
    repeat_purchase_bonus_points: Number(document.getElementById("growthRepeatBonus").value),
    coupon_expiry_days: Number(document.getElementById("growthCouponExpiry").value)
  };
  try {
    await api("/api/admin/store-growth-settings", {method:"PATCH", body:JSON.stringify(payload)});
    setMessage("تم حفظ إعدادات مستشار الشراء والتذكيرات والولاء لهذا المتجر.");
    await loadStoreGrowth();
  } catch (error) {
    setMessage(error.message || "تعذر حفظ الإعدادات.");
  }
});

async function loadStoreCoupons() {
  const businessId = document.getElementById("businessSelect").value;
  const tbody = document.getElementById("storeCouponRows");
  if (!businessId || !tbody) return;
  tbody.replaceChildren();
  try {
    const data = await api("/api/admin/store-coupons?business_id=" + encodeURIComponent(businessId));
    const coupons = data.coupons || [];
    if (!coupons.length) {
      const row = document.createElement("tr"); const td = cell("لا توجد مكافآت منشأة لهذا المتجر بعد."); td.colSpan = 7; row.appendChild(td); tbody.appendChild(row); return;
    }
    for (const coupon of coupons) {
      const row = document.createElement("tr");
      row.appendChild(cell(coupon.code));
      row.appendChild(cell(coupon.title));
      row.appendChild(cell(coupon.points_cost));
      row.appendChild(cell(String(coupon.discount_amount) + " " + coupon.currency));
      row.appendChild(cell(String(coupon.redemption_count || 0) + (coupon.max_redemptions ? " / " + coupon.max_redemptions : " / بلا حد")));
      row.appendChild(cell(coupon.expires_at ? new Date(coupon.expires_at).toLocaleDateString("ar") : "—"));
      row.appendChild(cell(coupon.is_active && new Date(coupon.expires_at) > new Date() ? "فعالة" : "منتهية/متوقفة"));
      tbody.appendChild(row);
    }
  } catch (error) {
    const row = document.createElement("tr"); const td = cell(error.message || "تعذر تحميل المكافآت."); td.colSpan = 7; row.appendChild(td); tbody.appendChild(row);
  }
}

document.getElementById("storeCouponForm").addEventListener("submit", async event => {
  event.preventDefault();
  const businessId = document.getElementById("businessSelect").value;
  if (!businessId) return;
  const maxRaw = document.getElementById("couponMaxRedemptions").value.trim();
  const payload = {
    business_id: businessId,
    code: document.getElementById("couponCode").value.trim(),
    title: document.getElementById("couponTitle").value.trim(),
    points_cost: Number(document.getElementById("couponPointsCost").value),
    discount_amount: Number(document.getElementById("couponDiscount").value),
    currency: document.getElementById("couponCurrency").value,
    expiry_days: Number(document.getElementById("couponExpiryDays").value),
    max_redemptions: maxRaw ? Number(maxRaw) : null
  };
  try {
    await api("/api/admin/store-coupons", {method:"POST", body:JSON.stringify(payload)});
    setMessage("تم إنشاء مكافأة الولاء لهذا المتجر.");
    document.getElementById("storeCouponForm").reset();
    await loadStoreCoupons();
  } catch (error) {
    setMessage(error.message || "تعذر إنشاء المكافأة.");
  }
});

async function updateMerchantSetupChecklist(businessId, productCount) {
  const list = document.getElementById("merchantSetupSteps");
  if (!list) return;
  list.replaceChildren();
  if (!businessId) {
    const empty = document.createElement("div"); empty.className = "empty-note";
    empty.textContent = "أنشئ متجرًا أو اختر المتجر لبدء الإعداد."; list.appendChild(empty); return;
  }
  let profile = {}, settings = {};
  try {
    const [profileData, settingsData] = await Promise.all([
      api("/api/admin/business-profile?business_id=" + encodeURIComponent(businessId)),
      api("/api/admin/business-assistant-settings?business_id=" + encodeURIComponent(businessId))
    ]);
    if (document.getElementById("businessSelect").value !== businessId) return;
    profile = profileData.business || {};
    settings = settingsData.settings || {};
  } catch (error) {
    const empty = document.createElement("div"); empty.className = "empty-note";
    empty.textContent = "تعذر فحص خطوات الإعداد. افتح إعدادات المتجر وحاول مجددًا.";
    list.appendChild(empty); return;
  }
  const steps = [
    {done: Boolean(String(profile.name || "").trim() && String(profile.industry || "").trim()), title:"بيانات المتجر", detail:"تأكد من اسم المتجر ومجال نشاطه.", action:"إعدادات المتجر", tab:"settings"},
    {done: Boolean(String(settings.business_policies || "").trim() && String(settings.business_hours || "").trim()), title:"سياسات التوصيل والدفع وساعات العمل", detail:"اكتب السياسات وساعات العمل ليجيب المساعد بدقة.", action:"إكمال السياسات", tab:"settings"},
    {done: productCount > 0, title:"إضافة الكتالوج", detail: productCount > 0 ? "تم تسجيل " + productCount.toLocaleString("ar") + " منتج في هذا المتجر." : "أضف المنتجات وأسعارها ومخزونها.", action:"إدارة المنتجات", tab:"products"},
    {done: Boolean(businessId), title:"رابط مساعد العملاء", detail:"الرابط خاص بهذا المتجر ويظهر في قسم المنتجات.", action:"عرض رابط العملاء", tab:"products"}
  ];
  for (const step of steps) {
    const item = document.createElement("div"); item.className = "stock-item";
    const content = document.createElement("div"); content.style.flex = "1";
    const title = document.createElement("strong"); title.textContent = (step.done ? "✓ " : "○ ") + step.title;
    const detail = document.createElement("div"); detail.className = "overview-sub"; detail.textContent = step.detail;
    content.append(title, detail);
    const action = document.createElement("button"); action.type = "button"; action.className = step.done ? "secondary" : "";
    action.textContent = step.done ? "مراجعة" : step.action;
    action.onclick = () => showTab(step.tab);
    item.append(content, action); list.appendChild(item);
  }
}

async function askManagerAssistant(question) {
  try {
    if (!(await ensureLogin())) return;
    const businessId = document.getElementById("businessSelect").value;
    if (!businessId) { setMessage("اختر المتجر أولًا."); return; }
    const answer = document.getElementById("managerAnswer");
    answer.textContent = "جارٍ تحليل بيانات المتجر...";
    const data = await api("/api/admin/manager-assistant", {
      method: "POST",
      body: JSON.stringify({business_id: businessId, question: question || ""})
    });
    if (document.getElementById("businessSelect").value !== businessId) return;
    answer.textContent = data.answer || "لا توجد إجابة متاحة.";
    const list = document.getElementById("managerInsights");
    list.replaceChildren();
    const insights = Array.isArray(data.insights) ? data.insights : [];
    if (!insights.length) {
      const empty = document.createElement("div"); empty.className = "empty-note";
      empty.textContent = "لا توجد تنبيهات حاليًا."; list.appendChild(empty);
    } else {
      for (const insight of insights) {
        const item = document.createElement("div"); item.className = "stock-item";
        const text = document.createElement("span"); text.textContent = insight.text || "";
        const badge = document.createElement("span");
        badge.className = "stock-badge" + (insight.kind === "warning" ? "" : " ok");
        badge.textContent = insight.kind === "warning" ? "يحتاج انتباهًا" : (insight.kind === "good" ? "جيد" : "معلومة");
        item.append(text, badge); list.appendChild(item);
      }
    }
  } catch (error) {
    document.getElementById("managerAnswer").textContent = error.message || "تعذر تحليل بيانات المتجر.";
  }
}

document.getElementById("managerQuestionForm").addEventListener("submit", event => {
  event.preventDefault();
  askManagerAssistant(document.getElementById("managerQuestion").value.trim());
});

async function loadOverview() {
  try {
    if (!(await ensureLogin())) return;
    if (!businesses.length) await loadBusinesses();
    const businessId = document.getElementById("businessSelect").value;
    if (!businessId) {
      for (const id of ["overviewOrders", "overviewProducts", "overviewAttention", "overviewLowStockCount"]) document.getElementById(id).textContent = "0";
      const stockList = document.getElementById("overviewStockList");
      stockList.replaceChildren();
      const note = document.createElement("div"); note.className = "empty-note"; note.textContent = "لا يوجد متجر محدد لعرض بياناته."; stockList.appendChild(note);
      return;
    }
    setMessage("جارٍ تحديث ملخص المتجر والمخزون...");
    const [orderData, productData] = await Promise.all([
      api("/api/admin/orders?business_id=" + encodeURIComponent(businessId)),
      api("/api/admin/products?business_id=" + encodeURIComponent(businessId))
    ]);
    // Ignore late responses if the user has switched stores while requests were in flight.
    if (document.getElementById("businessSelect").value !== businessId) return;
    const nextOrders = Array.isArray(orderData.orders) ? orderData.orders : [];
    const nextProducts = Array.isArray(productData.products) ? productData.products : [];
    if (nextOrders.some(row => String(row.business_id || "") !== businessId) ||
        nextProducts.some(row => String(row.business_id || "") !== businessId)) {
      allOrders = [];
      currentProducts = [];
      renderOrders();
      renderProducts();
      setMessage("تم إيقاف عرض البيانات لأن استجابة الخادم احتوت سجلات من متجر مختلف. لم يتم عرض أي سجل مختلط.");
      return;
    }
    allOrders = nextOrders;
    currentProducts = nextProducts;
    document.getElementById("overviewOrders").textContent = allOrders.length.toLocaleString("ar");
    document.getElementById("overviewProducts").textContent = currentProducts.length.toLocaleString("ar");
    document.getElementById("overviewProductSummary").textContent = currentProducts.filter(p => p.is_available && (p.stock_quantity == null || Number(p.stock_quantity) > 0)).length.toLocaleString("ar") + " منتج متاح للطلب";
    document.getElementById("overviewAttention").textContent = allOrders.filter(o => ["pending", "approved", "processing"].includes(o.status)).length.toLocaleString("ar");
    const lowStock = currentProducts.filter(p => !p.is_available || (p.stock_quantity != null && Number(p.stock_quantity) <= 3));
    document.getElementById("overviewLowStockCount").textContent = lowStock.length.toLocaleString("ar");
    const stockList = document.getElementById("overviewStockList");
    stockList.replaceChildren();
    if (!lowStock.length) {
      const note = document.createElement("div"); note.className = "empty-note"; note.textContent = "ممتاز! لا توجد منتجات نفد مخزونها أو اقترب من النفاد."; stockList.appendChild(note);
    } else {
      for (const product of lowStock.slice(0, 8)) {
        const item = document.createElement("div"); item.className = "stock-item";
        const name = document.createElement("strong"); name.textContent = product.name || "منتج دون اسم";
        const badge = document.createElement("span");
        const available = Boolean(product.is_available) && (product.stock_quantity == null || Number(product.stock_quantity) > 0);
        badge.className = "stock-badge" + (available ? " ok" : "");
        badge.textContent = !product.is_available ? "غير متاح" : (product.stock_quantity == null ? "المخزون غير محدد" : (Number(product.stock_quantity) === 0 ? "نفد المخزون" : "متبقي " + Number(product.stock_quantity).toLocaleString("ar")));
        item.append(name, badge); stockList.appendChild(item);
      }
    }
    const recentRows = document.getElementById("overviewRecentOrders"); recentRows.replaceChildren();
    const recent = [...allOrders].sort((a,b) => new Date(b.created_at || 0) - new Date(a.created_at || 0)).slice(0, 6);
    if (!recent.length) {
      const tr = document.createElement("tr"); const td = cell("لا توجد طلبات مسجلة لهذا المتجر حتى الآن."); td.colSpan = 4; tr.appendChild(td); recentRows.appendChild(tr);
    } else {
      for (const order of recent) {
        const tr = document.createElement("tr");
        tr.append(cell(order.order_number ?? order.id), cell(order.product_name), cell(labels[order.status] || order.status), cell(order.quantity));
        recentRows.appendChild(tr);
      }
    }
    renderOrders();
    renderProducts();
    await updateMerchantSetupChecklist(businessId, currentProducts.length);
    await askManagerAssistant("");
    setMessage("تم تحديث النظرة العامة. تنبيهات المخزون تشمل المنتجات غير المتاحة أو التي بقي منها 3 قطع أو أقل.");
  } catch (error) {
    setMessage(error.message || "تعذر تحميل النظرة العامة.");
  }
}

async function loadBusinessProfile() {
  try {
    if (!(await ensureLogin())) return;
    const businessId = document.getElementById("businessSelect").value;
    if (!businessId) {
      setMessage("اختر متجرًا أولًا.");
      return;
    }
    setMessage("جارٍ تحميل إعدادات المتجر...");
    const data = await api("/api/admin/business-profile?business_id=" + encodeURIComponent(businessId));
    const business = data.business || {};
    document.getElementById("profileBusinessId").value = business.id || businessId;
    document.getElementById("profileStoreName").value = business.name || "";
    document.getElementById("profileIndustry").value = business.industry || "";
    document.getElementById("profileDescription").value = business.description || "";
    document.getElementById("assistantSettingsBusinessId").value = businessId;
    const assistant = await api("/api/admin/business-assistant-settings?business_id=" + encodeURIComponent(businessId));
    const settings = assistant.settings || {};
    document.getElementById("assistantBusinessDescription").value = settings.business_description || "";
    document.getElementById("assistantBusinessPolicies").value = settings.business_policies || "";
    document.getElementById("assistantBusinessHours").value = settings.business_hours || "";
    document.getElementById("assistantCurrency").value = settings.currency || "YER";
    setMessage("تم تحميل بيانات المتجر وتعليمات المساعد.");
  } catch (error) {
    setMessage(error.message || "تعذر تحميل إعدادات المتجر.");
  }
}

document.getElementById("businessProfileForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = document.getElementById("saveBusinessProfileBtn");
  const businessId = document.getElementById("businessSelect").value;
  if (!businessId || businessId !== document.getElementById("profileBusinessId").value) {
    setMessage("تغير المتجر المحدد؛ أعد تحميل إعداداته قبل الحفظ.");
    return;
  }
  button.disabled = true;
  try {
    const result = await api("/api/admin/business-profile", {
      method: "PATCH",
      body: JSON.stringify({
        business_id: businessId,
        name: document.getElementById("profileStoreName").value.trim(),
        industry: document.getElementById("profileIndustry").value.trim(),
        description: document.getElementById("profileDescription").value.trim()
      })
    });
    const updated = result.business || {};
    businesses = businesses.map(b => b.id === businessId ? {...b, name: updated.name || document.getElementById("profileStoreName").value.trim()} : b);
    const option = [...document.getElementById("businessSelect").options].find(o => o.value === businessId);
    if (option) option.textContent = updated.name || document.getElementById("profileStoreName").value.trim();
    setMessage("تم حفظ إعدادات المتجر بنجاح. سيظهر الاسم والنشاط والنبذة في صفحة المتجر العامة.");
  } catch (error) {
    setMessage(error.message || "تعذر حفظ إعدادات المتجر.");
  } finally {
    button.disabled = false;
  }
});

document.getElementById("businessAssistantSettingsForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = document.getElementById("saveAssistantSettingsBtn");
  const businessId = document.getElementById("businessSelect").value;
  if (!businessId || businessId !== document.getElementById("assistantSettingsBusinessId").value) {
    setMessage("تغير المتجر المحدد؛ أعد تحميل إعداداته قبل الحفظ.");
    return;
  }
  button.disabled = true;
  try {
    await api("/api/admin/business-assistant-settings", {
      method: "PATCH",
      body: JSON.stringify({
        business_id: businessId,
        business_description: document.getElementById("assistantBusinessDescription").value.trim(),
        business_policies: document.getElementById("assistantBusinessPolicies").value.trim(),
        business_hours: document.getElementById("assistantBusinessHours").value.trim(),
        currency: document.getElementById("assistantCurrency").value
      })
    });
    setMessage("تم حفظ تعليمات المساعد لهذا المتجر. سيعتمد عليها في ردوده دون خلطها بمتاجر أخرى.");
  } catch (error) {
    setMessage(error.message || "تعذر حفظ تعليمات المساعد.");
  } finally {
    button.disabled = false;
  }
});

async function loadAIUsage() {
  const button = document.getElementById("refreshAIUsageBtn");
  button.disabled = true;
  try {
    if (!(await ensureLogin())) return;
    if (isMerchantSession) throw Error("هذا التقرير متاح للمدير العام فقط.");
    setMessage("جارٍ تحميل مقاييس استخدام الذكاء الاصطناعي...");
    const data = await api("/api/admin/ai-usage");
    document.getElementById("aiUsageSample").textContent = Number(data.sample_size || 0).toLocaleString("en-US");
    document.getElementById("aiUsageTokens").textContent = Number(data.total_tokens || 0).toLocaleString("en-US");
    document.getElementById("aiUsageCost").textContent = data.estimated_cost_usd === null
      ? "غير محددة"
      : "$" + Number(data.estimated_cost_usd).toFixed(6);
    document.getElementById("aiUsageUnpriced").textContent = Number(data.unpriced_requests || 0).toLocaleString("en-US");
    const rows = document.getElementById("aiUsageRows");
    rows.replaceChildren();
    for (const item of (data.recent || [])) {
      const tr = document.createElement("tr");
      let date = "—";
      if (item.created_at) {
        const parsed = new Date(item.created_at);
        if (!Number.isNaN(parsed.getTime())) date = parsed.toLocaleString("ar");
      }
      tr.append(
        cell(date),
        cell(item.model),
        cell(item.operation === "image" ? "صورة" : "نص"),
        cell(Number(item.input_tokens || 0).toLocaleString("en-US")),
        cell(Number(item.output_tokens || 0).toLocaleString("en-US")),
        cell(item.estimated_cost_usd === null ? "غير محددة" : "$" + Number(item.estimated_cost_usd).toFixed(6))
      );
      rows.appendChild(tr);
    }
    setMessage("تم تحديث مقاييس الاستخدام. قد تكون التكلفة غير محددة حتى تضبط أسعار الرموز من مزوّد Groq.");
  } catch (error) {
    setMessage(error.message || "تعذر تحميل مقاييس الاستخدام.");
  } finally {
    button.disabled = false;
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
    if (document.getElementById("businessSelect").value !== businessId) return;
    const nextOrders = Array.isArray(data.orders) ? data.orders : [];
    if (nextOrders.some(row => String(row.business_id || "") !== businessId)) {
      allOrders = [];
      renderOrders();
      setMessage("تم إيقاف عرض الطلبات لأن الاستجابة احتوت طلبًا تابعًا لمتجر آخر.");
      return;
    }
    allOrders = nextOrders;

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
    select.className = "order-status-select";
    select.setAttribute("aria-label", "حالة الطلب رقم " + (order.order_number ?? order.id));

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
    button.textContent = "حفظ الحالة";
    button.setAttribute("aria-label", "حفظ حالة الطلب رقم " + (order.order_number ?? order.id));

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

async function downloadSalesCsv() {
  try {
    if (!(await ensureLogin())) return;
    if (!businesses.length) await loadBusinesses();
    const businessId = document.getElementById("businessSelect").value;
    if (!businessId) {
      setMessage("اختر المتجر أولًا.");
      return;
    }
    setMessage("جارٍ تجهيز ملف الطلبات...");
    const response = await fetch(
      "/api/admin/reports/sales.csv?business_id=" + encodeURIComponent(businessId),
      {headers: {"Authorization": auth}}
    );
    if (response.status === 401) {
      await api("/api/admin/businesses");
      return;
    }
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw Error(error.detail || "تعذر تصدير الطلبات.");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "sales-orders-" + businessId + ".csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setMessage("تم تجهيز ملف CSV للمتجر المحدد.");
  } catch (error) {
    setMessage(error.message);
  }
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
    document.getElementById("reportCancelledOrders").textContent = data.cancelled_orders ?? 0;
    document.getElementById("realizedRevenue").textContent = formatMoneyGroups(data.exact_revenue);
    document.getElementById("deliveredQuantity").textContent = Number(data.delivered_quantity ?? 0).toLocaleString("ar");
    document.getElementById("exactRevenue").textContent =
      "المبيعات الدقيقة للطلبات المسلّمة: " + formatMoneyGroups(data.exact_revenue);
    document.getElementById("estimatedRevenue").textContent =
      "تقدير الطلبات القديمة حسب السعر الحالي: " + formatMoneyGroups(data.estimated_revenue);
    document.getElementById("missingRevenue").textContent =
      "طلبات مسلّمة بلا سعر يمكن حسابه: " + (data.missing_price_orders ?? 0);
    document.getElementById("cancelledExactValue").textContent =
      "قيمة الطلبات الملغاة المحفوظة: " + formatMoneyGroups(data.cancelled_exact_value);
    document.getElementById("cancelledEstimatedValue").textContent =
      "تقدير الطلبات الملغاة القديمة حسب السعر الحالي: " + formatMoneyGroups(data.cancelled_estimated_value);
    document.getElementById("cancelledMissingValue").textContent =
      "طلبات ملغاة بلا سعر يمكن حسابه: " + (data.cancelled_missing_price_orders ?? 0);
    setMessage("");
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
    document.getElementById("productsIntro").textContent = isMerchantSession
      ? "تظهر هنا منتجات متجرك فقط. تتم إدارة المنتجات تلقائيًا حسب حسابك."
      : "اختر النشاط التجاري الذي تريد إدارة منتجاته.";
    document.getElementById("createMerchantAccountBtn").classList.toggle("hidden", isMerchantSession);
    document.getElementById("requestsTab").classList.toggle("hidden", isMerchantSession);
    document.getElementById("aiUsageTab").classList.toggle("hidden", isMerchantSession);
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

    if (document.getElementById("businessSelect").value !== businessId) return;
    const nextProducts = Array.isArray(data.products) ? data.products : [];
    if (nextProducts.some(row => String(row.business_id || "") !== businessId)) {
      currentProducts = [];
      renderProducts();
      setMessage("تم إيقاف عرض المنتجات لأن الاستجابة احتوت منتجًا تابعًا لمتجر آخر.");
      return;
    }
    currentProducts = nextProducts;
    renderProducts();

    setMessage("عدد المنتجات: " + currentProducts.length);
  } catch (error) {
    setMessage(error.message);
  }
}

function csvCell(value) {
  const text = value === null || value === undefined ? "" : String(value);
  return /[",\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
}

async function extractCatalogFile() {
  const businessId = document.getElementById("businessSelect").value;
  const input = document.getElementById("catalogFile");
  const resultBox = document.getElementById("catalogExtractResult");
  const button = document.getElementById("extractCatalogBtn");
  const file = input.files && input.files[0];
  if (!businessId) {
    resultBox.textContent = "اختر المتجر أولًا.";
    return;
  }
  if (!file) {
    resultBox.textContent = "اختر ملف PDF أو صورة أولًا.";
    return;
  }
  if (file.size > 20 * 1024 * 1024) {
    resultBox.textContent = "حجم الملف يتجاوز 20 ميجابايت.";
    return;
  }
  button.disabled = true;
  resultBox.textContent = "جارٍ تحليل الملف. قد يستغرق ذلك وقتًا حسب عدد الصفحات...";
  try {
    const form = new FormData();
    form.append("business_id", businessId);
    form.append("file", file);
    const result = await api("/api/admin/products/extract-catalog", {
      method: "POST",
      body: form
    });
    if (document.getElementById("businessSelect").value !== businessId) return;
    const columns = ["name","price","currency","model_number","category","stock_quantity","image_url","description","is_available"];
    const lines = [columns.join(",")];
    (result.products || []).forEach(product => {
      lines.push(columns.map(key => csvCell(product[key])).join(","));
    });
    document.getElementById("productCsvText").value = lines.join("\n");
    resultBox.textContent = "تم استخراج " + (result.products || []).length +
      " صفًا للمعاينة. راجع البيانات أدناه وصحّحها قبل الضغط على «حفظ المنتجات بعد المراجعة»." +
      (result.warnings && result.warnings.length ? " ملاحظات: " + result.warnings.slice(0, 3).join(" | ") : "");
    document.getElementById("productImportResult").textContent = "";
  } catch (error) {
    resultBox.textContent = error.message || "تعذر تحليل الملف. جرّب صورة أو PDF أوضح.";
  } finally {
    button.disabled = false;
  }
}

async function importProductsCsv() {
  const businessId = document.getElementById("businessSelect").value;
  const csvText = document.getElementById("productCsvText").value;
  const resultBox = document.getElementById("productImportResult");
  const button = document.getElementById("importProductsBtn");
  if (!businessId) {
    resultBox.textContent = "اختر المتجر أولًا.";
    return;
  }
  if (!csvText.trim()) {
    resultBox.textContent = "الصق بيانات CSV أولًا.";
    return;
  }
  if (!confirm("سيتم إضافة المنتجات الجديدة إلى المتجر المحدد فقط. هل تريد المتابعة؟")) return;
  button.disabled = true;
  resultBox.textContent = "جارٍ التحقق من البيانات واستيرادها...";
  try {
    const result = await api("/api/admin/products/import-csv", {
      method: "POST",
      body: JSON.stringify({business_id: businessId, csv_text: csvText})
    });
    if (document.getElementById("businessSelect").value !== businessId) return;
    if (Array.isArray(result.product_ids) && result.product_ids.length) {
      sessionStorage.setItem(getUndoStorageKey(businessId), JSON.stringify({product_ids: result.product_ids, created_at: Date.now()}));
    }
    updateUndoButton();
    resultBox.textContent = "اكتمل الاستيراد. أضيف: " + result.created +
      "، تم تخطيه: " + result.skipped + "، أخطاء: " + result.errors.length +
      (result.errors.length ? " — " + result.errors.slice(0, 5).join(" | ") : "");
    await loadProducts();
    if (result.errors.length) {
      resultBox.textContent += " (عُرضت أول 5 أخطاء فقط).";
    }
  } catch (error) {
    resultBox.textContent = error.message || "تعذر استيراد المنتجات.";
  } finally {
    button.disabled = false;
    updateUndoButton();
  }
}

function renderProducts() {
  const rows = document.getElementById("productRows");
  rows.replaceChildren();

  const searchInput = document.getElementById("productSearch");
  const searchText = searchInput ? searchInput.value.trim().toLocaleLowerCase() : "";
  const visibleProducts = currentProducts.filter(product => {
    const searchable = [
      product.name, product.model_number, product.category, product.description
    ].filter(Boolean).join(" ").toLocaleLowerCase();
    return searchable.includes(searchText);
  });

  if (!visibleProducts.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.textContent = currentProducts.length && searchText
      ? "لا توجد منتجات تطابق عبارة البحث."
      : (isMerchantSession
        ? "لا توجد منتجات في متجرك حتى الآن. أضف أول منتج من نموذج «إضافة منتج جديد» أعلاه؛ منتجات المتاجر الأخرى لا تظهر هنا."
        : "لا توجد منتجات لهذا النشاط التجاري.");
    tr.appendChild(td);
    rows.appendChild(tr);
    return;
  }

  for (const product of visibleProducts) {
    const tr = document.createElement("tr");

    const productCell = document.createElement("td");
    if (product.image_url) {
      const image = document.createElement("img");
      image.src = product.image_url;
      image.alt = product.name || "صورة المنتج";
      image.loading = "lazy";
      image.style.cssText = "width:76px;height:76px;object-fit:cover;border-radius:8px;display:block;margin-bottom:6px";
      image.onerror = () => image.remove();
      productCell.appendChild(image);
    }
    const productLabel = document.createElement("div");
    productLabel.textContent = product.name || "";
    productCell.appendChild(productLabel);
    if (product.model_number) {
      const model = document.createElement("small");
      model.textContent = "الموديل: " + product.model_number;
      productCell.appendChild(model);
    }
    tr.appendChild(productCell);
    tr.appendChild(cell(product.category || product.description));
    tr.appendChild(cell(product.stock_quantity ?? 0));
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
    const remove = document.createElement("button");
    remove.textContent = "حذف";
    remove.className = "danger";
    remove.style.marginInlineStart = "6px";
    remove.onclick = () => deleteProduct(product);
    action.appendChild(remove);
    tr.appendChild(action);
    rows.appendChild(tr);
  }
}

function getUndoStorageKey(businessId) { return "catalogUndo:" + businessId; }

function updateUndoButton() {
  const businessId = document.getElementById("businessSelect").value;
  const button = document.getElementById("undoLastImportBtn");
  if (!button) return;
  let record = null;
  try { record = businessId ? JSON.parse(sessionStorage.getItem(getUndoStorageKey(businessId)) || "null") : null; } catch (_) {}
  button.disabled = !(record && Array.isArray(record.product_ids) && record.product_ids.length);
  button.title = button.disabled ? "لا يوجد استيراد حديث يمكن التراجع عنه في هذه الجلسة." : "حذف المنتجات التي أُضيفت في آخر عملية استيراد.";
}

async function undoLastImport() {
  const businessId = document.getElementById("businessSelect").value;
  const button = document.getElementById("undoLastImportBtn");
  const resultBox = document.getElementById("productImportResult");
  let record = null;
  try { record = JSON.parse(sessionStorage.getItem(getUndoStorageKey(businessId)) || "null"); } catch (_) {}
  if (!businessId || !record || !Array.isArray(record.product_ids) || !record.product_ids.length) {
    updateUndoButton(); return;
  }
  if (!confirm("سيتم حذف المنتجات التي أُضيفت في آخر استيراد فقط. المنتجات التي ارتبطت بطلبات لن تُحذف حفاظًا على سجل الطلبات. هل تريد المتابعة؟")) return;
  button.disabled = true;
  try {
    const result = await api("/api/admin/products/undo-import?business_id=" + encodeURIComponent(businessId), {
      method: "POST", body: JSON.stringify({product_ids: record.product_ids})
    });
    if (document.getElementById("businessSelect").value !== businessId) return;
    sessionStorage.removeItem(getUndoStorageKey(businessId));
    resultBox.textContent = "تم التراجع عن الاستيراد. حُذف: " + result.deleted + "، احتُفظ بمنتجات مرتبطة بطلبات: " + result.retained;
    await loadProducts();
  } catch (error) {
    resultBox.textContent = error.message || "تعذر التراجع عن الاستيراد.";
  } finally { updateUndoButton(); }
}

async function deleteProduct(product) {
  const businessId = document.getElementById("businessSelect").value;
  if (!businessId || !product || !product.id) return;
  if (!confirm("هل تريد حذف المنتج «" + product.name + "»؟ إذا كان مرتبطًا بطلب سابق، سيمنع النظام حذفه للحفاظ على سجل الطلبات.")) return;
  try {
    await api("/api/admin/products/" + encodeURIComponent(product.id) + "?business_id=" + encodeURIComponent(businessId), {method:"DELETE"});
    setMessage("تم حذف المنتج.");
    await loadProducts();
  } catch (error) { setMessage(error.message || "تعذر حذف المنتج."); }
}

function editProduct(product) {
  showProductSection("editor");
  document.getElementById("productImageFile").value = "";
  const preview = document.getElementById("productImagePreview");
  preview.replaceChildren();
  if (product.image_url) { const img = document.createElement("img"); img.src = product.image_url; img.alt = "معاينة صورة المنتج"; img.style.cssText = "width:100px;height:100px;object-fit:cover;border-radius:8px"; preview.appendChild(img); }
  document.getElementById("productId").value = product.id;
  document.getElementById("productName").value = product.name || "";
  document.getElementById("productDescription").value = product.description || "";
  document.getElementById("productModelNumber").value = product.model_number || "";
  document.getElementById("productCategory").value = product.category || "";
  document.getElementById("productImageUrl").value = product.image_url || "";
  document.getElementById("productStock").value = product.stock_quantity ?? 0;
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
  showProductSection("catalog");
  document.getElementById("productForm").reset();
  document.getElementById("productId").value = "";
  document.getElementById("productCurrency").value = "YER";
  document.getElementById("productModelNumber").value = "";
  document.getElementById("productCategory").value = "";
  document.getElementById("productImageUrl").value = "";
  document.getElementById("productImageFile").value = "";
  document.getElementById("productImagePreview").replaceChildren();
  document.getElementById("productStock").value = "0";
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

    const imageFile = document.getElementById("productImageFile").files[0];
    const payload = {
      business_id: businessId,
      name: document.getElementById("productName").value.trim(),
      description: document.getElementById("productDescription")
        .value.trim(),
      price: document.getElementById("productPrice").value,
      currency: document.getElementById("productCurrency")
        .value.trim(),
      is_available: document.getElementById("productAvailable").value === "true",
      model_number: document.getElementById("productModelNumber").value.trim(),
      category: document.getElementById("productCategory").value.trim(),
      image_url: document.getElementById("productImageUrl").value.trim(),
      stock_quantity: Number(document.getElementById("productStock").value)
    };
    if (!Number.isInteger(payload.stock_quantity) || payload.stock_quantity < 0) {
      setMessage("أدخل كمية مخزون صحيحة (صفر أو أكثر).");
      return;
    }

    if (!payload.name || !payload.currency || payload.price === "") {
      setMessage("يرجى تعبئة اسم المنتج والسعر والعملة.");
      return;
    }

    button.disabled = true;

    try {
      if (imageFile) {
        const form = new FormData();
        form.append("business_id", businessId);
        form.append("file", imageFile);
        const uploaded = await api("/api/admin/products/upload-image", {method:"POST", body:form});
        if (document.getElementById("businessSelect").value !== businessId) return;
        payload.image_url = uploaded.image_url;
      }
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
    // Clear the previous tenant's data immediately; never leave it visible while
    // the newly selected tenant's requests are loading.
    clearOverviewForStoreSwitch();
    currentProducts = [];
    allOrders = [];
    renderProducts();
    renderOrders();
    resetProductForm();
    updateTelegramCustomerLink();
    updateUndoButton();
    loadProducts();
    loadOrders();
    loadReports();
    if (!document.getElementById("overviewPanel").classList.contains("hidden")) loadOverview();
  });

document.getElementById("filter")
  .addEventListener("change", renderOrders);

document.getElementById("productImageFile").addEventListener("change", () => {
  const preview = document.getElementById("productImagePreview");
  preview.replaceChildren();
  const file = document.getElementById("productImageFile").files[0];
  if (!file) return;
  if (!["image/jpeg","image/png","image/webp"].includes(file.type) || file.size > 8 * 1024 * 1024) {
    setMessage("ارفع صورة JPG أو PNG أو WEBP بحجم لا يتجاوز 8 ميجابايت.");
    document.getElementById("productImageFile").value = "";
    return;
  }
  const img = document.createElement("img");
  img.alt = "معاينة الصورة قبل الحفظ";
  img.style.cssText = "width:100px;height:100px;object-fit:cover;border-radius:8px;margin-top:8px";
  img.src = URL.createObjectURL(file);
  preview.appendChild(img);
});

document.getElementById("productSearch")
  .addEventListener("input", renderProducts);

document.getElementById("orderSearch")
  .addEventListener("input", renderOrders);

updateUndoButton();
if (auth) {
  document.getElementById("accountBtn").classList.add("hidden");
  document.getElementById("logoutBtn").classList.remove("hidden");
  // Restore the selected business after refresh before loading any private data.
  // Merchant sessions receive only their own business from /api/admin/businesses.
  (async () => {
    await loadBusinesses();
    await loadOrders();
    await loadReports();
  })();
} else {
  openLogin();
}
</script>
</body>
</html>
""")

    @router.get("/api/admin/ai-usage")
    def get_ai_usage(_admin=Depends(check_admin)):
        if _admin.get("role") != "admin":
            raise HTTPException(status_code=403, detail="تقارير استخدام الذكاء الاصطناعي متاحة للمدير العام فقط.")
        try:
            result = (
                supabase.table("ai_usage_logs")
                .select("created_at, business_id, provider, model, operation, input_tokens, output_tokens, total_tokens, estimated_cost_usd")
                .order("created_at", desc=True)
                .limit(1000)
                .execute()
            )
            records = result.data or []
            total_input = sum(int(row.get("input_tokens") or 0) for row in records)
            total_output = sum(int(row.get("output_tokens") or 0) for row in records)
            total_tokens = sum(int(row.get("total_tokens") or 0) for row in records)
            priced = [
                float(row["estimated_cost_usd"])
                for row in records
                if row.get("estimated_cost_usd") is not None
            ]
            return {
                "sample_size": len(records),
                "limit": 1000,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_tokens,
                "estimated_cost_usd": round(sum(priced), 8) if priced else None,
                "priced_requests": len(priced),
                "unpriced_requests": len(records) - len(priced),
                "recent": [
                    {
                        **row,
                        "estimated_cost_usd": (
                            float(row["estimated_cost_usd"])
                            if row.get("estimated_cost_usd") is not None else None
                        ),
                    }
                    for row in records[:50]
                ],
            }
        except Exception:
            logger.exception("تعذر تحميل مقاييس استخدام الذكاء الاصطناعي.")
            raise HTTPException(
                status_code=503,
                detail="تعذر تحميل المقاييس. تأكد من تنفيذ ترحيل ai_usage_monitoring في Supabase.",
            )

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

    @router.get("/api/admin/business-profile")
    def get_business_profile(
        business_id: str,
        _admin=Depends(check_admin),
    ):
        ensure_business_access(business_id, _admin)
        try:
            result = (
                supabase.table("businesses")
                .select("id, name, industry, description")
                .eq("id", business_id)
                .limit(1)
                .execute()
            )
            if not result.data:
                raise HTTPException(status_code=404, detail="المتجر غير موجود")
            return {"business": result.data[0]}
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر تحميل إعدادات المتجر.")
            raise HTTPException(status_code=500, detail="تعذر تحميل إعدادات المتجر")

    @router.patch("/api/admin/business-profile")
    def update_business_profile(
        payload: BusinessProfileUpdate,
        _admin=Depends(check_admin),
    ):
        ensure_business_access(payload.business_id, _admin)
        name = payload.name.strip()
        if len(name) < 2:
            raise HTTPException(status_code=400, detail="اسم المتجر يجب أن يتكون من حرفين على الأقل")
        try:
            result = (
                supabase.table("businesses")
                .update({
                    "name": name,
                    "industry": payload.industry.strip(),
                    "description": payload.description.strip(),
                })
                .eq("id", payload.business_id)
                .execute()
            )
            if not result.data:
                raise HTTPException(status_code=404, detail="لم يتم العثور على المتجر لتحديثه")
            return {"success": True, "business": result.data[0]}
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر حفظ إعدادات المتجر.")
            raise HTTPException(status_code=500, detail="تعذر حفظ إعدادات المتجر")

    @router.get("/api/admin/business-assistant-settings")
    def get_business_assistant_settings(
        business_id: str,
        _admin=Depends(check_admin),
    ):
        ensure_business_access(business_id, _admin)
        get_business_or_404(business_id)
        try:
            result = (
                supabase.table("business_settings")
                .select("business_id, business_description, business_policies, business_hours, currency")
                .eq("business_id", business_id)
                .limit(1)
                .execute()
            )
            settings = result.data[0] if result.data else {
                "business_id": business_id,
                "business_description": "",
                "business_policies": "",
                "business_hours": "",
                "currency": "YER",
            }
            return {"settings": settings}
        except Exception:
            logger.exception("تعذر تحميل تعليمات المساعد.")
            raise HTTPException(
                status_code=503,
                detail="جدول إعدادات المساعد غير جاهز. نفّذ ملف ترحيل business_settings في Supabase ثم أعد المحاولة.",
            )

    @router.patch("/api/admin/business-assistant-settings")
    def update_business_assistant_settings(
        payload: BusinessAssistantSettingsUpdate,
        _admin=Depends(check_admin),
    ):
        ensure_business_access(payload.business_id, _admin)
        get_business_or_404(payload.business_id)
        currency = payload.currency.strip().upper()
        if currency not in {"YER", "SAR", "USD", "AED", "EGP"}:
            raise HTTPException(status_code=400, detail="العملة المحددة غير مدعومة")
        values = {
            "business_id": payload.business_id,
            "business_description": payload.business_description.strip(),
            "business_policies": payload.business_policies.strip(),
            "business_hours": payload.business_hours.strip(),
            "currency": currency,
        }
        try:
            result = (
                supabase.table("business_settings")
                .upsert(values, on_conflict="business_id")
                .execute()
            )
            if not result.data:
                verify = (
                    supabase.table("business_settings")
                    .select("business_id, business_description, business_policies, business_hours, currency")
                    .eq("business_id", payload.business_id)
                    .limit(1)
                    .execute()
                )
                saved = verify.data[0] if verify.data else None
            else:
                saved = result.data[0]
            if not saved:
                raise HTTPException(status_code=500, detail="لم يتم تأكيد حفظ تعليمات المساعد")
            return {"success": True, "settings": saved}
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر حفظ تعليمات المساعد.")
            raise HTTPException(
                status_code=503,
                detail="تعذر حفظ تعليمات المساعد. تأكد من تنفيذ ملف ترحيل business_settings في Supabase.",
            )

    @router.get("/api/admin/store-growth-settings")
    def get_store_growth_settings_route(
        business_id: str,
        _actor=Depends(check_admin),
    ):
        ensure_business_access(business_id, _actor)
        get_business_or_404(business_id)
        defaults = {
            "business_id": business_id,
            "shopping_advisor_enabled": True,
            "followups_enabled": True,
            "followup_delay_hours": 24,
            "loyalty_enabled": True,
            "points_per_1000": 1,
            "point_value_yer": 1,
            "minimum_redeem_points": 20,
            "repeat_purchase_threshold": 3,
            "repeat_purchase_bonus_points": 10,
            "coupon_expiry_days": 30,
        }
        try:
            result = (
                supabase.table("store_growth_settings")
                .select("business_id,shopping_advisor_enabled,followups_enabled,followup_delay_hours,loyalty_enabled,points_per_1000,point_value_yer,minimum_redeem_points,repeat_purchase_threshold,repeat_purchase_bonus_points,coupon_expiry_days,updated_at")
                .eq("business_id", business_id)
                .limit(1)
                .execute()
            )
            return {"settings": {**defaults, **(result.data[0] if result.data else {})}}
        except Exception:
            logger.exception("تعذر تحميل إعدادات النمو والولاء.")
            raise HTTPException(status_code=503, detail="نفّذ ترحيل store_growth_features في Supabase ثم أعد المحاولة.")

    @router.patch("/api/admin/store-growth-settings")
    def update_store_growth_settings_route(
        payload: StoreGrowthSettingsUpdate,
        _actor=Depends(check_admin),
    ):
        ensure_business_access(payload.business_id, _actor)
        get_business_or_404(payload.business_id)
        values = payload.model_dump()
        values["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            supabase.table("store_growth_settings").upsert(
                values, on_conflict="business_id"
            ).execute()
            saved = (
                supabase.table("store_growth_settings")
                .select("*")
                .eq("business_id", payload.business_id)
                .limit(1)
                .execute()
            )
            if not saved.data:
                raise HTTPException(status_code=500, detail="لم يتم تأكيد حفظ الإعدادات.")
            return {"success": True, "settings": saved.data[0]}
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر حفظ إعدادات النمو والولاء.")
            raise HTTPException(status_code=503, detail="تعذر حفظ الإعدادات. تأكد من تطبيق ترحيل store_growth_features.")

    @router.get("/api/admin/store-coupons")
    def list_store_coupons(
        business_id: str,
        _actor=Depends(check_admin),
    ):
        ensure_business_access(business_id, _actor)
        get_business_or_404(business_id)
        try:
            result = (
                supabase.table("store_coupons")
                .select("id,business_id,code,title,points_cost,discount_amount,currency,expires_at,max_redemptions,redemption_count,is_active,created_at")
                .eq("business_id", business_id)
                .order("created_at", desc=True)
                .limit(100)
                .execute()
            )
            return {"coupons": result.data or []}
        except Exception:
            logger.exception("تعذر تحميل كوبونات المتجر.")
            raise HTTPException(status_code=503, detail="نفّذ ترحيل store_growth_features في Supabase ثم أعد المحاولة.")

    @router.post("/api/admin/store-coupons")
    def create_store_coupon(
        payload: StoreCouponCreate,
        _actor=Depends(check_admin),
    ):
        ensure_business_access(payload.business_id, _actor)
        get_business_or_404(payload.business_id)
        code = payload.code.strip().upper()
        currency = payload.currency.strip().upper()
        if not code.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail="رمز المكافأة يقبل الحروف والأرقام والشرطة فقط.")
        if currency not in {"YER", "SAR", "USD", "AED", "EGP"}:
            raise HTTPException(status_code=400, detail="العملة المحددة غير مدعومة.")
        values = {
            "business_id": payload.business_id,
            "code": code,
            "title": payload.title.strip(),
            "points_cost": payload.points_cost,
            "discount_amount": payload.discount_amount,
            "currency": currency,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=payload.expiry_days)).isoformat(),
            "max_redemptions": payload.max_redemptions,
            "is_active": True,
        }
        try:
            inserted = supabase.table("store_coupons").insert(values).select("*").execute()
            if inserted.data:
                return {"success": True, "coupon": inserted.data[0]}
            check = (
                supabase.table("store_coupons")
                .select("id,code,title,points_cost,discount_amount,currency,expires_at,max_redemptions,redemption_count,is_active")
                .eq("business_id", payload.business_id)
                .eq("code", code)
                .limit(1)
                .execute()
            )
            if check.data:
                raise HTTPException(status_code=409, detail="رمز المكافأة مستخدم بالفعل في هذا المتجر.")
            raise HTTPException(status_code=500, detail="لم يتم تأكيد إنشاء المكافأة.")
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر إنشاء كوبون ولاء.")
            raise HTTPException(status_code=503, detail="تعذر إنشاء المكافأة. تأكد من تطبيق ترحيل store_growth_features.")

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
                    "price, currency, is_available, model_number, category, image_url, stock_quantity"
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
                    "model_number": payload.model_number.strip(),
                    "category": payload.category.strip(),
                    "image_url": payload.image_url.strip(),
                    "stock_quantity": payload.stock_quantity,
                })
                .select(
                    "id, business_id, name, description, "
                    "price, currency, is_available, model_number, category, image_url, stock_quantity"
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

    @router.post("/api/admin/products/upload-image")
    def upload_product_image(
        business_id: str = Form(...),
        file: UploadFile = File(...),
        _admin=Depends(check_admin),
    ):
        ensure_business_access(business_id, _admin)
        get_business_or_404(business_id)
        content_type = str(file.content_type or "").lower()
        raw = file.file.read(8 * 1024 * 1024 + 1)
        if not raw or len(raw) > 8 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="اختر صورة غير فارغة لا يتجاوز حجمها 8 ميجابايت.")
        signatures = {
            "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
            "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP",
        }
        if content_type not in signatures or not signatures[content_type]:
            raise HTTPException(status_code=400, detail="الصيغ المدعومة هي JPG وPNG وWEBP فقط.")
        try:
            bucket = supabase.storage.from_("product-images")
            object_path = f"{business_id}/{secrets.token_hex(16)}.{ {'image/jpeg':'jpg','image/png':'png','image/webp':'webp'}[content_type] }"
            try:
                supabase.storage.create_bucket("product-images", options={"public": True})
            except Exception:
                # Bucket may already exist. Upload below will surface any real permission/configuration error.
                pass
            bucket.upload(object_path, raw, {"content-type": content_type, "upsert": "true"})
            public_url = bucket.get_public_url(object_path)
            if isinstance(public_url, dict):
                public_url = public_url.get("publicUrl") or public_url.get("publicURL") or ""
            if not isinstance(public_url, str) or not public_url.startswith("http"):
                raise RuntimeError("تعذر إنشاء رابط عام للصورة")
            return {"success": True, "image_url": public_url}
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر رفع صورة المنتج إلى Supabase Storage")
            raise HTTPException(status_code=503, detail="تعذر رفع الصورة. تحقق من صلاحيات Supabase Storage وإنشاء bucket باسم product-images.")

    @router.post("/api/admin/products/extract-catalog")
    def extract_product_catalog(
        business_id: str = Form(...),
        file: UploadFile = File(...),
        _admin=Depends(check_admin),
    ):
        ensure_business_access(business_id, _admin)
        get_business_or_404(business_id)

        filename = str(file.filename or "").lower()
        content_type = str(file.content_type or "").lower()
        try:
            raw = file.file.read(20 * 1024 * 1024 + 1)
            if not raw:
                raise HTTPException(status_code=400, detail="الملف فارغ.")
            if len(raw) > 20 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="الحد الأقصى لحجم الملف 20 ميجابايت.")

            pages = []
            if filename.endswith(".pdf") or content_type == "application/pdf" or raw.startswith(b"%PDF-"):
                try:
                    import fitz
                    document = fitz.open(stream=raw, filetype="pdf")
                except Exception:
                    raise HTTPException(status_code=400, detail="تعذر فتح PDF. تأكد أن الملف سليم وغير محمي بكلمة مرور.")
                if document.page_count > 40:
                    document.close()
                    raise HTTPException(status_code=400, detail="الحد الأقصى 40 صفحة في العملية. قسّم الكتالوج الكبير إلى ملفات أصغر.")
                for page_number, page in enumerate(document, start=1):
                    page_text = (page.get_text("text") or "").strip()
                    # Render pages as images so scanned/image-only PDFs can be read too.
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False)
                    pages.append({
                        "page": page_number,
                        "text": page_text[:12000],
                        "image": pixmap.tobytes("png"),
                        "mime": "image/png",
                    })
                document.close()
            else:
                valid_image = (
                    raw.startswith(b"\xff\xd8\xff")
                    or raw.startswith(b"\x89PNG\r\n\x1a\n")
                    or (len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")
                )
                if not valid_image:
                    raise HTTPException(status_code=400, detail="الملف غير مدعوم. ارفع PDF أو صورة JPG أو PNG أو WEBP.")
                if len(raw) > 12 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="حجم الصورة يتجاوز حد المعالجة 12 ميجابايت.")
                pages.append({"page": 1, "text": "", "image": raw, "mime": "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else ("image/webp" if raw[8:12] == b"WEBP" else "image/png")})

            from bot_logic import vision_llm
            from langchain_core.messages import HumanMessage, SystemMessage
            import base64
            import json as json_module
            import re as re_module

            extracted = []
            warnings = []
            system_prompt = (
                "أنت أداة استخراج بيانات كتالوج منتجات من صفحات مصورة. "
                "أعد JSON صالحًا فقط بالشكل {\"products\":[{\"name\":\"\",\"price\":null,"
                "\"currency\":\"YER\",\"model_number\":\"\",\"category\":\"\","
                "\"stock_quantity\":0,\"image_url\":\"\",\"description\":\"\",\"is_available\":false}]}. "
                "استخرج فقط ما يظهر بوضوح. لا تخترع اسمًا أو سعرًا أو رقم موديل. "
                "إذا كان السعر غير مقروء اجعل price null، وإذا لم يوجد موديل اتركه فارغًا. "
                "لا تعتبر أرقام الهاتف أو أرقام الصفحات أسعارًا. "
                "لا تستخرج أكثر من 1000 منتج إجمالًا. اجعل المخزون 0 والتوفر false لأنهما غير معروفين ما لم يظهرا صراحة. "
                "لا تنشئ روابط صور؛ image_url يبقى فارغًا."
            )
            for item in pages:
                image_b64 = base64.b64encode(item["image"]).decode("ascii")
                prompt = (
                    f"حلّل الصفحة رقم {item['page']} من كتالوج متجر. "
                    "استخرج جميع المنتجات الواضحة، منتجًا لكل عنصر. "
                    "أرفق النص المطبوع المستخرج آليًا للمساعدة إن وجد، لكن اعتمد على الصورة للتحقق. "
                    "أعد كائن JSON فقط بالمفتاح products. لا تضف شرحًا.\n"
                    f"النص المستخرج من الصفحة:\n{item['text'] or '[لا يوجد نص قابل للاستخراج]'}"
                )
                message = HumanMessage(content=[
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{item['mime']};base64,{image_b64}"}},
                ])
                response = vision_llm.invoke([SystemMessage(content=system_prompt), message])
                answer = response.content if isinstance(response.content, str) else str(response.content)
                answer = re_module.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", answer.strip(), flags=re_module.IGNORECASE)
                try:
                    parsed = json_module.loads(answer)
                except Exception:
                    match = re_module.search(r"\{.*\}", answer, flags=re_module.DOTALL)
                    if not match:
                        warnings.append(f"تعذر فهم نتيجة الصفحة {item['page']}.")
                        continue
                    try:
                        parsed = json_module.loads(match.group(0))
                    except Exception:
                        warnings.append(f"نتيجة الصفحة {item['page']} ليست JSON صالحًا.")
                        continue
                rows = parsed.get("products", []) if isinstance(parsed, dict) else []
                if not isinstance(rows, list):
                    warnings.append(f"لم تُقرأ قائمة المنتجات في الصفحة {item['page']}.")
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("name") or "").strip()
                    price = row.get("price")
                    if not name:
                        continue
                    if len(extracted) >= 1000:
                        warnings.append("وصلنا إلى الحد الأقصى 1000 منتج.")
                        break
                    if price is not None:
                        price_text = str(price).replace(",", "").strip()
                        try:
                            if not Decimal(price_text).is_finite() or Decimal(price_text) < 0:
                                price = None
                            else:
                                price = price_text
                        except Exception:
                            price = None
                    extracted.append({
                        "name": name[:200],
                        "price": price if price is not None else "",
                        "currency": str(row.get("currency") or "YER")[:10],
                        "model_number": str(row.get("model_number") or "")[:120],
                        "category": str(row.get("category") or "")[:120],
                        "stock_quantity": str(row.get("stock_quantity") if row.get("stock_quantity") is not None else 0),
                        "image_url": "",
                        "description": str(row.get("description") or "")[:2000],
                        "is_available": str(row.get("is_available")).lower() if isinstance(row.get("is_available"), bool) else "false",
                    })
            if not extracted:
                raise HTTPException(status_code=422, detail="لم يتم استخراج منتجات واضحة. جرّب ملفًا أوضح أو استخدم استيراد CSV.")
            return {"success": True, "products": extracted, "warnings": warnings, "pages_processed": len(pages)}
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر استخراج كتالوج المنتجات من الملف.")
            raise HTTPException(status_code=502, detail="تعذر تحليل الكتالوج بالذكاء الاصطناعي. تحقق من إعداد GROQ_API_KEY ونموذج الصور ثم أعد المحاولة.")
        finally:
            try:
                file.file.close()
            except Exception:
                pass

    @router.post("/api/admin/products/import-csv")
    def import_products_csv(
        payload: ProductCsvImport,
        _admin=Depends(check_admin),
    ):
        ensure_business_access(payload.business_id, _admin)
        get_business_or_404(payload.business_id)

        try:
            reader = csv.DictReader(io.StringIO(payload.csv_text.lstrip("\ufeff")))
            if not reader.fieldnames:
                raise HTTPException(status_code=400, detail="لم يتم العثور على صف عناوين الأعمدة.")
            aliases = {
                "name": ("name", "product_name", "اسم المنتج", "المنتج"),
                "price": ("price", "السعر"),
                "currency": ("currency", "العملة"),
                "model_number": ("model_number", "model", "sku", "رقم الموديل", "الموديل", "رقم الصنف"),
                "category": ("category", "التصنيف", "الفئة"),
                "stock_quantity": ("stock_quantity", "stock", "quantity", "المخزون", "الكمية"),
                "image_url": ("image_url", "image", "رابط الصورة", "صورة المنتج"),
                "description": ("description", "الوصف", "وصف المنتج"),
                "is_available": ("is_available", "available", "متاح"),
            }
            normalized_headers = {
                str(header or "").strip().lower(): str(header or "")
                for header in reader.fieldnames
            }
            columns = {}
            for key, choices in aliases.items():
                for choice in choices:
                    if choice.lower() in normalized_headers:
                        columns[key] = normalized_headers[choice.lower()]
                        break
            if "name" not in columns or "price" not in columns:
                raise HTTPException(
                    status_code=400,
                    detail="يجب أن يحتوي CSV على عمودي name وprice أو «اسم المنتج» و«السعر».",
                )

            rows = list(reader)
            if len(rows) > 1000:
                raise HTTPException(status_code=400, detail="الحد الأقصى 1000 صف في كل عملية.")
            if not rows:
                raise HTTPException(status_code=400, detail="لا توجد صفوف منتجات بعد صف العناوين.")

            existing_result = (
                supabase.table("products")
                .select("name, model_number")
                .eq("business_id", payload.business_id)
                .limit(5000)
                .execute()
            )
            existing_names = {
                str(row.get("name") or "").strip().casefold()
                for row in (existing_result.data or [])
            }
            existing_models = {
                str(row.get("model_number") or "").strip().casefold()
                for row in (existing_result.data or [])
                if str(row.get("model_number") or "").strip()
            }
            new_products = []
            errors = []
            skipped = 0
            seen_names = set(existing_names)
            seen_models = set(existing_models)

            def cell_value(row, key, default=""):
                header = columns.get(key)
                return str(row.get(header, default) or "").strip() if header else default

            for row_number, row in enumerate(rows, start=2):
                name = cell_value(row, "name")
                price_text = cell_value(row, "price").replace(",", "")
                model = cell_value(row, "model_number")
                try:
                    if not name:
                        raise ValueError("اسم المنتج فارغ")
                    price = Decimal(price_text)
                    if not price.is_finite() or price < 0:
                        raise ValueError("السعر يجب أن يكون رقمًا موجبًا أو صفرًا")
                    stock_text = cell_value(row, "stock_quantity", "0") or "0"
                    stock = int(stock_text)
                    if stock < 0 or stock > 100000000:
                        raise ValueError("كمية المخزون خارج النطاق")
                    currency = (cell_value(row, "currency", "YER") or "YER").upper()
                    if len(currency) > 10:
                        raise ValueError("رمز العملة طويل جدًا")
                    if name.casefold() in seen_names or (model and model.casefold() in seen_models):
                        skipped += 1
                        continue
                    available_text = (cell_value(row, "is_available", "true") or "true").lower()
                    if available_text in ("true", "1", "yes", "y", "نعم", "متاح"):
                        available = True
                    elif available_text in ("false", "0", "no", "n", "لا", "غير متاح"):
                        available = False
                    else:
                        raise ValueError("قيمة is_available يجب أن تكون true أو false")
                    product = {
                        "business_id": payload.business_id,
                        "name": name[:200],
                        "description": cell_value(row, "description")[:2000],
                        "price": float(price),
                        "currency": currency,
                        "is_available": available,
                        "model_number": model[:120],
                        "category": cell_value(row, "category")[:120],
                        "image_url": cell_value(row, "image_url")[:2000],
                        "stock_quantity": stock,
                    }
                    new_products.append(product)
                    seen_names.add(name.casefold())
                    if model:
                        seen_models.add(model.casefold())
                except (ValueError, ArithmeticError) as exc:
                    errors.append(f"الصف {row_number}: {str(exc)[:180]}")
            created_rows = []
            if new_products:
                inserted = supabase.table("products").insert(new_products).select("id").execute()
                created_rows = inserted.data or []
            return {"success": True, "created": len(created_rows), "product_ids": [str(row["id"]) for row in created_rows if row.get("id")], "skipped": skipped, "errors": errors}
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر استيراد كتالوج المنتجات")
            raise HTTPException(status_code=500, detail="تعذر استيراد الكتالوج. تحقق من تنسيق CSV وأعمدة جدول products.")

    @router.delete("/api/admin/products/{product_id}")
    def delete_product(
        product_id: str,
        business_id: str,
        _admin=Depends(check_admin),
    ):
        ensure_business_access(business_id, _admin)
        get_business_or_404(business_id)
        try:
            existing = (
                supabase.table("products").select("id")
                .eq("id", product_id).eq("business_id", business_id).limit(1).execute()
            )
            if not existing.data:
                raise HTTPException(status_code=404, detail="المنتج غير موجود في هذا المتجر.")
            linked_orders = (
                supabase.table("orders").select("id")
                .eq("business_id", business_id).eq("product_id", product_id).limit(1).execute()
            )
            if linked_orders.data:
                raise HTTPException(status_code=409, detail="لا يمكن حذف هذا المنتج لأنه مرتبط بطلب سابق. عطّله من التوفر بدلًا من ذلك للحفاظ على سجل الطلبات.")
            deleted = (
                supabase.table("products").delete()
                .eq("id", product_id).eq("business_id", business_id).select("id").execute()
            )
            if not deleted.data:
                raise HTTPException(status_code=404, detail="لم يُحذف المنتج؛ تأكد أنه تابع لهذا المتجر.")
            return {"success": True, "deleted": 1}
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر حذف المنتج")
            raise HTTPException(status_code=500, detail="تعذر حذف المنتج. إذا كان مرتبطًا بطلب، عطّله بدل الحذف.")

    @router.post("/api/admin/products/undo-import")
    def undo_product_import(
        payload: dict,
        business_id: str,
        _admin=Depends(check_admin),
    ):
        ensure_business_access(business_id, _admin)
        get_business_or_404(business_id)
        product_ids = payload.get("product_ids") if isinstance(payload, dict) else None
        if not isinstance(product_ids, list) or not product_ids or len(product_ids) > 1000:
            raise HTTPException(status_code=400, detail="قائمة المنتجات المطلوب التراجع عنها غير صحيحة.")
        ids = list(dict.fromkeys(str(value) for value in product_ids if value))
        try:
            # Never trust IDs supplied by the browser: first verify that every
            # candidate belongs to the authenticated merchant's business.
            owned = (
                supabase.table("products").select("id")
                .eq("business_id", business_id).in_("id", ids).limit(1000).execute()
            )
            owned_ids = [str(row["id"]) for row in (owned.data or []) if row.get("id")]
            if len(owned_ids) != len(ids):
                raise HTTPException(status_code=403, detail="بعض المنتجات لا تتبع هذا المتجر أو لم تعد موجودة؛ لم يتم تنفيذ التراجع.")
            orders = (
                supabase.table("orders").select("product_id")
                .eq("business_id", business_id).in_("product_id", ids).limit(1000).execute()
            )
            linked = {str(row.get("product_id")) for row in (orders.data or []) if row.get("product_id")}
            safe_ids = [value for value in ids if value not in linked]
            if safe_ids:
                supabase.table("products").delete().eq("business_id", business_id).in_("id", safe_ids).execute()
            return {"success": True, "deleted": len(safe_ids), "retained": len(ids) - len(safe_ids)}
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر التراجع عن استيراد المنتجات")
            raise HTTPException(status_code=500, detail="تعذر التراجع عن الاستيراد. لم تُنفّذ العملية كاملة؛ راجع المنتجات قبل المحاولة مجددًا.")

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
                    "model_number": payload.model_number.strip(),
                    "category": payload.category.strip(),
                    "image_url": payload.image_url.strip(),
                    "stock_quantity": payload.stock_quantity,
                })
                .eq("id", product_id)
                .eq("business_id", business_id)
                .select(
                    "id, business_id, name, description, price, currency, "
                    "is_available, model_number, category, image_url, stock_quantity"
                )
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

    @router.post("/api/admin/manager-assistant")
    def merchant_manager_assistant(
        payload: dict,
        _admin=Depends(check_admin),
    ):
        """Explain store performance using only the selected store's saved data."""
        business_id = str((payload or {}).get("business_id") or "").strip()
        question = str((payload or {}).get("question") or "").strip()[:1000]
        if not business_id:
            raise HTTPException(status_code=400, detail="اختر المتجر أولًا.")
        ensure_business_access(business_id, _admin)
        get_business_or_404(business_id)
        try:
            from datetime import datetime, timedelta, timezone

            now = datetime.now(timezone.utc)
            today = now.date()
            week_start = today - timedelta(days=today.weekday())
            week_start_dt = datetime.combine(week_start, datetime.min.time(), tzinfo=timezone.utc)
            previous_week_start = week_start_dt - timedelta(days=7)
            last_7_start = now - timedelta(days=7)
            prior_7_start = now - timedelta(days=14)
            month_start = now - timedelta(days=30)

            order_result = (
                supabase.table("orders").select("*")
                .eq("business_id", business_id)
                .order("created_at", desc=True).limit(1000).execute()
            )
            product_result = (
                supabase.table("products")
                .select("id, name, price, currency, is_available, stock_quantity")
                .eq("business_id", business_id).limit(5000).execute()
            )
            orders = order_result.data or []
            products = product_result.data or []

            def parsed_date(row):
                value = row.get("created_at")
                if not value:
                    return None
                try:
                    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return parsed.astimezone(timezone.utc)
                except (TypeError, ValueError):
                    return None

            def amount_for(order):
                quantity = float(order.get("quantity") or 0)
                if quantity <= 0:
                    return None, None, False
                if order.get("total_price") is not None:
                    return float(order["total_price"]), order.get("currency") or "YER", True
                product_name = str(order.get("product_name") or "").strip().casefold()
                matches = [p for p in products if str(p.get("name") or "").strip().casefold() == product_name]
                if len(matches) == 1 and matches[0].get("price") is not None:
                    return float(matches[0]["price"]) * quantity, matches[0].get("currency") or "YER", False
                return None, None, False

            def grouped_revenue(rows):
                totals, exact_totals = {}, {}
                missing = 0
                for order in rows:
                    if order.get("status") != "delivered":
                        continue
                    amount, currency, exact = amount_for(order)
                    if amount is None:
                        missing += 1
                        continue
                    totals[currency] = round(totals.get(currency, 0) + amount, 2)
                    if exact:
                        exact_totals[currency] = round(exact_totals.get(currency, 0) + amount, 2)
                return totals, exact_totals, missing

            week_orders = [o for o in orders if (d := parsed_date(o)) and d >= week_start_dt]
            previous_week_orders = [o for o in orders if (d := parsed_date(o)) and previous_week_start <= d < week_start_dt]
            recent_7 = [o for o in orders if (d := parsed_date(o)) and d >= last_7_start]
            prior_7 = [o for o in orders if (d := parsed_date(o)) and prior_7_start <= d < last_7_start]
            last_30 = [o for o in orders if (d := parsed_date(o)) and d >= month_start]
            week_revenue, week_exact, week_missing = grouped_revenue(week_orders)
            prev_revenue, _, _ = grouped_revenue(previous_week_orders)

            delivered_orders_last_30 = [
                order for order in last_30 if order.get("status") == "delivered"
            ]

            low_stock = [
                {"name": p.get("name") or "منتج دون اسم", "stock_quantity": p.get("stock_quantity"), "is_available": p.get("is_available")}
                for p in products
                if not p.get("is_available") or (p.get("stock_quantity") is not None and int(p.get("stock_quantity") or 0) <= 3)
            ]
            slow_products = products_without_recent_deliveries(
                products, delivered_orders_last_30
            )
            pending_count = sum(1 for o in orders if o.get("status") in ("pending", "approved"))
            processing_count = sum(1 for o in orders if o.get("status") == "processing")
            recent_count, prior_count = len(recent_7), len(prior_7)
            if prior_count:
                demand_change = round((recent_count - prior_count) * 100 / prior_count)
            else:
                demand_change = 100 if recent_count else 0

            def money_text(groups):
                if not groups:
                    return "لا توجد إيرادات مسجلة"
                return "، ".join(f"{amount:,.2f} {currency}" for currency, amount in sorted(groups.items()))

            insights = []
            if low_stock:
                insights.append({"kind": "warning", "text": f"هناك {len(low_stock)} منتج يحتاج مراجعة المخزون؛ يشمل ذلك المنتجات غير المتاحة أو التي بقي منها 3 قطع أو أقل."})
            else:
                insights.append({"kind": "good", "text": "لا توجد منتجات نفد مخزونها أو اقترب من النفاد وفق حد التنبيه الحالي (3 قطع)."})
            if pending_count:
                insights.append({"kind": "warning", "text": f"لديك {pending_count} طلبًا قيد المراجعة أو بانتظار الموافقة."})
            if processing_count:
                insights.append({"kind": "info", "text": f"هناك {processing_count} طلبات قيد التجهيز تحتاج متابعة."})
            if recent_count > prior_count:
                insights.append({"kind": "good", "text": f"زاد عدد الطلبات خلال آخر 7 أيام إلى {recent_count} مقارنةً بـ {prior_count} في الأيام السبعة السابقة ({demand_change:+d}%)."})
            elif recent_count < prior_count:
                insights.append({"kind": "info", "text": f"انخفض عدد الطلبات خلال آخر 7 أيام إلى {recent_count} مقارنةً بـ {prior_count} في الأيام السبعة السابقة ({demand_change:+d}%)."})
            else:
                insights.append({"kind": "info", "text": f"عدد الطلبات في آخر 7 أيام: {recent_count}، ومثلها في الأيام السبعة السابقة: {prior_count}."})
            if slow_products:
                names = "، ".join(slow_products[:5])
                insights.append({"kind": "info", "text": f"هذه المنتجات لم يظهر لها طلب مُسلّم خلال آخر 30 يومًا ضمن البيانات المحمّلة: {names}."})

            lowered = question.casefold()
            asks_week = any(term in lowered for term in ("هذا الأسبوع", "الاسبوع", "الأسبوع", "week", "ربحت", "الربح", "الإيراد", "المبيعات"))
            if asks_week:
                answer = f"من بداية هذا الأسبوع ({week_start.isoformat()}) حتى الآن، قيمة الطلبات المسلّمة المحسوبة هي {money_text(week_revenue)}. من هذا المبلغ، القيمة المحفوظة وقت إنشاء الطلبات هي {money_text(week_exact)}."
                if week_missing:
                    answer += f" تعذّر حساب قيمة {week_missing} طلب/طلبات مسلّمة لعدم توفر سعر موثوق."
                answer += " هذا إيراد/مبيعات وليس ربحًا صافيًا. لا أستطيع حساب الربح الحقيقي دون تكلفة شراء المنتجات والمصاريف مثل التوصيل والتغليف والعمولات. الأرقام تعتمد على آخر 1000 طلب محمّل."
            elif question:
                answer = "أستطيع تلخيص أداء المتجر من الطلبات والمنتجات المسجلة. اسأل مثلًا: كم بلغت مبيعات الطلبات المسلّمة هذا الأسبوع؟ ولحساب الربح الحقيقي يلزم تسجيل تكلفة المنتج والمصاريف."
            else:
                answer = "هذا ملخص تنبيهات مبني على بيانات المتجر المحدد فقط. الإيرادات لا تعني الربح الصافي؛ يلزم إدخال تكلفة البضاعة والمصاريف لحساب الربح الحقيقي."

            return {
                "business_id": business_id,
                "answer": answer,
                "insights": insights,
                "week_start": week_start.isoformat(),
                "week_delivered_revenue": week_revenue,
                "week_exact_revenue": week_exact,
                "week_missing_price_orders": week_missing,
                "recent_7_day_orders": recent_count,
                "previous_7_day_orders": prior_count,
                "low_stock_count": len(low_stock),
                "pending_orders": pending_count,
                "processing_orders": processing_count,
                "slow_products": slow_products,
                "data_limit": 1000,
            }
        except HTTPException:
            raise
        except Exception:
            logger.exception("تعذر إنشاء ملخص مدير المتجر الذكي.")
            raise HTTPException(status_code=500, detail="تعذر تحليل بيانات المتجر الآن. حاول تحديث الصفحة.")
 
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
            delivered_quantity = 0
            exact_revenue = {}
            estimated_revenue = {}
            missing_price_orders = 0
            cancelled_exact_value = {}
            cancelled_estimated_value = {}
            cancelled_missing_price_orders = 0

            for order in orders:
                status = order.get("status")
                if status == "pending":
                    counts["pending_orders"] += 1
                elif status == "processing":
                    counts["processing_orders"] += 1
                elif status == "delivered":
                    counts["delivered_orders"] += 1
                    try:
                        quantity_value = int(order.get("quantity") or 0)
                        if quantity_value > 0:
                            delivered_quantity += quantity_value
                    except (TypeError, ValueError):
                        logger.warning("Invalid quantity in delivered order %s", order.get("order_number"))
                elif status == "cancelled":
                    counts["cancelled_orders"] += 1

                # Delivered orders contribute to sales; cancelled orders are
                # reported separately and never counted as realized revenue.
                if status not in ("delivered", "cancelled"):
                    continue

                is_cancelled = status == "cancelled"
                exact_target = cancelled_exact_value if is_cancelled else exact_revenue
                estimated_target = (
                    cancelled_estimated_value if is_cancelled else estimated_revenue
                )

                try:
                    quantity = float(order.get("quantity") or 0)
                    if quantity <= 0:
                        raise ValueError("invalid quantity")
                    if order.get("total_price") is not None:
                        amount = float(order["total_price"])
                        currency = order.get("currency") or "YER"
                        target = exact_target
                    else:
                        key = (
                            str(order.get("business_id") or ""),
                            str(order.get("product_name") or "").strip().casefold(),
                        )
                        product = product_prices.get(key)
                        if not product or product.get("price") is None:
                            if is_cancelled:
                                cancelled_missing_price_orders += 1
                            else:
                                missing_price_orders += 1
                            continue
                        amount = float(product["price"]) * quantity
                        currency = product.get("currency") or "YER"
                        target = estimated_target
                    target[currency] = round(target.get(currency, 0) + amount, 2)
                except (TypeError, ValueError):
                    if is_cancelled:
                        cancelled_missing_price_orders += 1
                    else:
                        missing_price_orders += 1

            return {
                "total_orders": len(orders),
                **counts,
                "delivered_quantity": delivered_quantity,
                "exact_revenue": exact_revenue,
                "estimated_revenue": estimated_revenue,
                "missing_price_orders": missing_price_orders,
                "cancelled_exact_value": cancelled_exact_value,
                "cancelled_estimated_value": cancelled_estimated_value,
                "cancelled_missing_price_orders": cancelled_missing_price_orders,
                "limit": 1000,
            }
        except Exception:
            logger.exception("تعذر إنشاء تقرير المبيعات")
            raise HTTPException(
                status_code=500,
                detail="تعذر إنشاء تقرير المبيعات. تحقق من جداول الطلبات والمنتجات.",
            )

    @router.get("/api/admin/reports/sales.csv")
    def export_sales_csv(
        business_id: str,
        _admin=Depends(check_admin),
    ):
        ensure_business_access(business_id, _admin)
        get_business_or_404(business_id)
        try:
            result = (
                supabase.table("orders")
                .select(
                    "order_number, created_at, customer_name, product_name, "
                    "quantity, status, total_price, currency, payment_method"
                )
                .eq("business_id", business_id)
                .order("created_at", desc=True)
                .limit(1000)
                .execute()
            )
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow([
                "رقم الطلب", "تاريخ الإنشاء", "اسم العميل", "المنتج",
                "الكمية", "الحالة", "الإجمالي المحفوظ", "العملة", "طريقة الدفع",
            ])

            def safe_csv_text(value):
                text = "" if value is None else str(value)
                # Prevent spreadsheet formula execution when opening exported CSV.
                if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                    return "'" + text
                return text

            for order in result.data or []:
                writer.writerow([
                    safe_csv_text(order.get("order_number")),
                    safe_csv_text(order.get("created_at")),
                    safe_csv_text(order.get("customer_name")),
                    safe_csv_text(order.get("product_name")),
                    safe_csv_text(order.get("quantity")),
                    safe_csv_text(order.get("status")),
                    safe_csv_text(order.get("total_price")),
                    safe_csv_text(order.get("currency")),
                    safe_csv_text(order.get("payment_method")),
                ])

            content = "\ufeff" + output.getvalue()
            return Response(
                content=content,
                media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="sales-orders-{business_id}.csv"'
                    ),
                    "Cache-Control": "no-store",
                },
            )
        except Exception:
            logger.exception("تعذر تصدير طلبات المبيعات")
            raise HTTPException(status_code=500, detail="تعذر تصدير ملف الطلبات.")

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
            raise HTTPException(status_code=400, detail="حالة غير مسموح بها")

        try:
            existing_result = (
                supabase.table("orders")
                .select("id, order_number, customer_name, product_name, status, telegram_chat_id")
                .eq("id", order_id)
                .eq("business_id", business_id)
                .limit(1)
                .execute()
            )
            if not existing_result.data:
                raise HTTPException(status_code=404, detail="الطلب غير موجود")

            existing = existing_result.data[0]
            old_status = existing.get("status")

            # Delivered and cancelled orders are terminal states. Prevent later
            # edits from silently changing sales totals or reopening a cancelled
            # order without a dedicated stock-safe workflow.
            if old_status in ("delivered", "cancelled") and payload.status != old_status:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "لا يمكن تغيير حالة طلب تم تسليمه أو إلغاؤه. "
                        "إذا كان هناك خطأ، راجعه من خلال المدير المسؤول."
                    ),
                )

            if payload.status == "cancelled":
                # Database transaction locks the order and restores reserved stock
                # only on the first transition to cancelled.
                rpc_result = supabase.rpc(
                    "update_telegram_order_status_with_stock",
                    {
                        "p_business_id": business_id,
                        "p_order_id": order_id,
                        "p_new_status": payload.status,
                    },
                ).execute()
                result_data = rpc_result.data
                if isinstance(result_data, list):
                    result_data = result_data[0] if result_data else None
                if not isinstance(result_data, dict) or not result_data.get("success"):
                    reason = result_data.get("reason") if isinstance(result_data, dict) else "unknown"
                    if reason == "order_not_found":
                        raise HTTPException(status_code=404, detail="الطلب غير موجود")
                    if reason == "cannot_cancel_after_shipping":
                        raise HTTPException(
                            status_code=409,
                            detail="لا يمكن إلغاء الطلب بعد شحنه أو تسليمه.",
                        )
                    if reason == "product_not_found":
                        raise HTTPException(
                            status_code=409,
                            detail="تعذر العثور على المنتج المرتبط بالطلب؛ لم يتم تغيير الحالة.",
                        )
                    raise HTTPException(
                        status_code=409,
                        detail="تعذر إلغاء الطلب وإرجاع المخزون بأمان.",
                    )
                updated = result_data.get("order") or {}
            else:
                updated_result = (
                    supabase.table("orders")
                    .update({"status": payload.status})
                    .eq("id", order_id)
                    .eq("business_id", business_id)
                    .select("*")
                    .execute()
                )
                if not updated_result.data:
                    raise HTTPException(status_code=404, detail="تعذر تحديث الطلب أو أن الطلب غير موجود")
                updated = updated_result.data[0]

            notification_sent = False
            if old_status != payload.status:
                notification_sent = send_telegram_status_notification(
                    existing.get("telegram_chat_id"), updated
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
                    "ووجود الأعمدة المطلوبة في جدول orders."
                ),
            )

    return router
