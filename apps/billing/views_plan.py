# apps/billing/views_plan.py
from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.filters import SearchFilter
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone

from .models import Plan
from .serializers import PlanSerializer
from .permissions import IsSuperuser, IsCEOorSuperuser
from .utils import IdentityServiceClient, swagger_helper
from .validators import InputValidator


class PlanView(viewsets.ModelViewSet):
    serializer_class = PlanSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter]
    search_fields = ['name']
    filterset_fields = ['id', 'name', 'price', 'industry', 'is_active', 'discontinued']

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            try:
                print("PlanView.get_permissions - user:", getattr(self.request, 'user', None))
                print("PlanView.get_permissions - possible role fields:", getattr(self.request, 'role', None),
                      getattr(self.request.user, 'role', None))
            except Exception:
                pass
            return [IsAuthenticated(), IsCEOorSuperuser()]
        return [IsAuthenticated(), IsSuperuser()]

    def get_queryset(self):
        user = self.request.user
        base_qs = Plan.objects.all()
        role = getattr(user, 'role', None)

        print(f"PlanView.get_queryset - User: {user}, Role: {role}")

        if user.is_superuser or (role and role.lower() == 'superuser'):
            print("User is superuser - showing all plans")
            return base_qs

        if role and role.lower() == 'ceo':
            print("User is CEO")
            tenant_id = getattr(user, 'tenant', None)
            if not tenant_id:
                print("No tenant ID found for CEO")
                return Plan.objects.none()
            try:
                client = IdentityServiceClient(request=self.request)
                tenant = client.get_tenant(tenant_id=tenant_id)
                industry = tenant.get('industry') if tenant and isinstance(tenant, dict) else None
                print(f"CEO tenant industry: {industry}")
                if not industry:
                    print("No industry found for CEO's tenant")
                    return Plan.objects.none()
                result = base_qs.filter(is_active=True, industry__iexact=industry, discontinued=False)
                print(f"Found {result.count()} plans for industry: {industry}")
                return result
            except Exception as e:
                print(f"Error getting tenant data: {str(e)}")
                return Plan.objects.none()

        print("User has no relevant role")
        return Plan.objects.none()

    @swagger_helper("Plan", "create")
    def create(self, request, *args, **kwargs):
        try:
            validator = InputValidator()
            errors = {}

            name = request.data.get('name')
            if not name:
                errors['name'] = ['Name is required']

            price = request.data.get('price')
            price_error = validator.validate_positive_number(price, 'Price')
            if price_error:
                errors['price'] = [price_error]

            industry = request.data.get('industry')
            if industry:
                industry_choices = [choice[0] for choice in Plan.INDUSTRY_CHOICES]
                industry_error = validator.validate_choice(industry, industry_choices, 'Industry')
                if industry_error:
                    errors['industry'] = [industry_error]

            billing_period = request.data.get('billing_period')
            if billing_period:
                period_choices = [choice[0] for choice in Plan.PERIOD_CHOICES]
                period_error = validator.validate_choice(billing_period, period_choices, 'Billing Period')
                if period_error:
                    errors['billing_period'] = [period_error]

            if errors:
                return Response({'errors': errors}, status=status.HTTP_400_BAD_REQUEST)

            return super().create(request, *args, **kwargs)

        except Exception as e:
            return Response({'error': 'Plan creation failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_helper("Plan", "health_check")
    @action(detail=False, methods=['get'], url_path='health')
    def health_check(self, request):
        try:
            total_plans = Plan.objects.count()
            active_plans = Plan.objects.filter(is_active=True).count()
            return Response({
                'status': 'healthy',
                'total_plans': total_plans,
                'active_plans': active_plans,
                'timestamp': timezone.now().isoformat()
            })
        except Exception as e:
            return Response({
                'status': 'unhealthy',
                'error': str(e),
                'timestamp': timezone.now().isoformat()
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    @swagger_helper("Plan", "list_plans")
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @swagger_helper("Plan", "retrieve")
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @swagger_helper("Plan", "partial_update")
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @swagger_helper("Plan", "update")
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @swagger_helper("Plan", "destroy")
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)
