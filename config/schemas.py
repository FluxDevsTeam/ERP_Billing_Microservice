from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi

schema_view = get_schema_view(
    openapi.Info(
        title="ERP Billing Microservice",
        default_version="v1",
        description="""
            An API Template For ERP Billing App.

            **Servers:**
            - Local: [http://localhost:8000](http://localhost:8000)
            - Production: [https://domain.com/](https://domain.com/)
            """,
        contact=openapi.Contact(email="suskidee@gmail.com"),
        license=openapi.License(name="MIT License"),
    ),
    public=True,
    permission_classes=[permissions.AllowAny],
)

