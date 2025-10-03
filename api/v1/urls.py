from django.urls import path, include

urlpatterns = [
    path("billing/", include("apps.billing.urls")),
    path("payment/", include("apps.payment.urls")),
    path("admin/", include("apps.superadmin.urls"))
]
