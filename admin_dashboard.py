
import os
import hmac

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

security = HTTPBasic()

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

    @router.get("/admin", response_class=HTMLResponse)
    def dashboard():
        return HTMLResponse("""
        <!doctype html>
        <html lang="ar" dir="rtl">
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>إدارة الطلبات</title>
        <style>
        body{font-family:Arial;background:#f2f5fa;margin:20px;color:#182238}
        h2{margin-bottom:8px}
        button,select{padding:9px;border-radius:6px;margin:4px}
        button{cursor:pointer;background:#1559a8;color:white;border:0}
        .wrap{overflow-x:auto;background:white;padding:12px;border-radius:10px}
        table{width:100%;border-collapse:collapse;min-width:850px}
        th,td{padding:11px;border-bottom:1px solid #ddd;text-align:right}
        th{background:#eaf0f8}
        #msg{margin:15px 0}
        </style>
        <h2>لوحة إدارة طلبات العملاء</h2>
        <p>عرض الطلبات وتحديث حالتها في قاعدة البيانات.</p>
        <label>الحالة:</label>
        <select id="filter">
          <option value="">كل الطلبات</option>
          <option value="pending">قيد المراجعة</option>
          <option value="approved">تمت الموافقة</option>
          <option value="processing">قيد التجهيز</option>
          <option value="shipped">قيد التوصيل</option>
          <option value="delivered">تم التسليم</option>
          <option value="cancelled">ملغي</option>
        </select>
        <button onclick="loadOrders()">تحديث</button>
        <div id="msg">جارٍ الاتصال...</div>
        <div class="wrap">
        <table>
        <thead><tr>
        <th>رقم الطلب</th><th>المنتج</th><th>الكمية</th>
        <th>العميل</th><th>الهاتف</th><th>العنوان</th>
        <th>الدفع</th><th>الحالة</th><th>حفظ</th>
        </tr></thead>
        <tbody id="rows"></tbody>
        </table></div>
        <script>
        const labels={
          pending:"قيد المراجعة",approved:"تمت الموافقة",
          processing:"قيد التجهيز",shipped:"قيد التوصيل",
          delivered:"تم التسليم",cancelled:"ملغي"
        };
        let auth="";
        async function api(url,options={}){
          const r=await fetch(url,{
            ...options,
            headers:{
              "Authorization":auth,
              "Content-Type":"application/json"
            }
          });
          if(r.status===401){
            auth="";
            throw Error("بيانات الدخول غير صحيحة");
          }
          const data=await r.json();
          if(!r.ok)throw Error(data.detail||"حدث خطأ");
          return data;
        }
        async function login(){
          const u=prompt("اسم المستخدم الإداري");
          if(u===null)return false;
          const p=prompt("كلمة المرور الإدارية");
          if(p===null)return false;
          auth="Basic "+btoa(unescape(encodeURIComponent(u+":"+p)));
          await api("/api/admin/orders");
          return true;
        }
        async function loadOrders(){
          try{
            if(!auth && !(await login()))return;
            document.getElementById("msg").textContent="جارٍ تحميل الطلبات...";
            const filter=document.getElementById("filter").value;
            const data=await api("/api/admin/orders"+
              (filter?"?status="+encodeURIComponent(filter):""));
            const rows=document.getElementById("rows");
            rows.replaceChildren();
            for(const o of data.orders){
              const tr=document.createElement("tr");
              const values=[
                o.order_number??o.id,o.product_name,o.quantity,
                o.customer_name,o.customer_phone,o.customer_address,
                o.payment_method
              ];
              for(const v of values){
                const td=document.createElement("td");
                td.textContent=v??"—";
                tr.appendChild(td);
              }
              const statusCell=document.createElement("td");
              const select=document.createElement("select");
              for(const [value,label] of Object.entries(labels)){
                const option=document.createElement("option");
                option.value=value;
                option.textContent=label;
                option.selected=value===o.status;
                select.appendChild(option);
              }
              statusCell.appendChild(select);
              tr.appendChild(statusCell);
              const actionCell=document.createElement("td");
              const btn=document.createElement("button");
              btn.textContent="حفظ";
              btn.onclick=async()=>{
                btn.disabled=true;
                try{
                  await api("/api/admin/orders/"+
                    encodeURIComponent(o.id)+"/status",{
                      method:"PATCH",
                      body:JSON.stringify({status:select.value})
                    });
                  document.getElementById("msg").textContent=
                    "تم حفظ حالة الطلب بنجاح.";
                  await loadOrders();
                }catch(e){
                  document.getElementById("msg").textContent=e.message;
                }finally{btn.disabled=false;}
              };
              actionCell.appendChild(btn);
              tr.appendChild(actionCell);
              rows.appendChild(tr);
            }
            document.getElementById("msg").textContent=
              "عدد الطلبات: "+data.orders.length;
          }catch(e){
            document.getElementById("msg").textContent=e.message;
          }
        }
        document.getElementById("filter").onchange=loadOrders;
        loadOrders();
        </script>
        </html>
        """)

    @router.get("/api/admin/orders")
    def list_orders(
        _admin: bool = Depends(check_admin),
        status: str = "",
    ):
        if status and status not in STATUSES:
            raise HTTPException(400, "حالة غير صحيحة")

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
            raise HTTPException(500, "تعذر تحميل الطلبات")

    @router.patch("/api/admin/orders/{order_id}/status")
    def update_status(
        order_id: str,
        payload: StatusUpdate,
        _admin: bool = Depends(check_admin),
    ):
        if payload.status not in STATUSES:
            raise HTTPException(400, "حالة غير مسموح بها")

        try:
            result = (
                supabase.table("orders")
                .update({"status": payload.status})
                .eq("id", order_id)
                .execute()
            )
            if not result.data:
                raise HTTPException(404, "الطلب غير موجود")
            return {"success": True, "order": result.data[0]}
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(500, "تعذر تحديث حالة الطلب")

    return router
