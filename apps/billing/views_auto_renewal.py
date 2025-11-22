# apps/billing/views_auto_renewal.py
from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.filters import SearchFilter
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.core.exceptions import ValidationError
import uuid
import logging

logger = logging.getLogger(__name__)

from .models import AutoRenewal
from .serializers import (
    AutoRenewalSerializer, AutoRenewalCreateSerializer, AutoRenewalUpdateSerializer
)
from .permissions import IsCEOorSuperuser, CanViewEditSubscription
from .utils import swagger_helper
from .services import AutoRenewalService


class AutoRenewalViewSet(viewsets.ModelViewSet):
    """ViewSet for managing auto-renewals"""
    queryset = AutoRenewal.objects.all()
    serializer_class = AutoRenewalSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter]
    search_fields = ['tenant_id', 'user_id']
    filterset_fields = ['tenant_id', 'plan', 'status']

    def get_queryset(self):
        user = self.request.user
        role = getattr(user, 'role', None)
        
        if user.is_superuser or (role and role.lower() == 'superuser'):
            return AutoRenewal.objects.select_related('plan', 'subscription').all()
        
        tenant_id = getattr(user, 'tenant', None)
        if tenant_id and role and role.lower() == 'ceo':
            try:
                tenant_id = uuid.UUID(str(tenant_id))
                return AutoRenewal.objects.select_related('plan', 'subscription').filter(tenant_id=tenant_id)
            except ValueError:
                return AutoRenewal.objects.none()
        
        return AutoRenewal.objects.none()

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [IsAuthenticated(), IsCEOorSuperuser()]
        return [IsAuthenticated(), CanViewEditSubscription()]

    def get_serializer_class(self):
        if self.action == 'create':
            return AutoRenewalCreateSerializer
        if self.action in ['update', 'partial_update']:
            return AutoRenewalUpdateSerializer
        return AutoRenewalSerializer

    @swagger_helper("AutoRenewal", "create")
    def create(self, request, *args, **kwargs):
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            auto_renewal_service = AutoRenewalService(request)
            
            auto_renewal, result = auto_renewal_service.create_auto_renewal(
                tenant_id=str(serializer.validated_data['tenant_id']),
                plan_id=str(serializer.validated_data['plan_id']),
                expiry_date=serializer.validated_data['expiry_date'],
                user_id=str(request.user.id),
                subscription_id=str(serializer.validated_data.get('subscription_id')) if serializer.validated_data.get('subscription_id') else None
            )
            
            response_serializer = AutoRenewalSerializer(auto_renewal)
            return Response({
                'data': 'Auto-renewal created successfully.',
                'auto_renewal': response_serializer.data
            }, status=status.HTTP_201_CREATED)
            
        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Auto-renewal creation failed: {str(e)}")
            return Response({'error': 'Auto-renewal creation failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='process')
    @swagger_helper("AutoRenewal", "process")
    def process_renewal(self, request, pk=None):
        """Manually trigger processing of an auto-renewal"""
        try:
            auto_renewal_service = AutoRenewalService(request)
            result = auto_renewal_service.process_auto_renewal(auto_renewal_id=pk)
            
            return Response(result)
            
        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Auto-renewal processing failed: {str(e)}")
            return Response({'error': 'Auto-renewal processing failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='cancel')
    @swagger_helper("AutoRenewal", "cancel")
    def cancel_renewal(self, request, pk=None):
        """Cancel an auto-renewal"""
        try:
            auto_renewal_service = AutoRenewalService(request)
            auto_renewal, result = auto_renewal_service.cancel_auto_renewal(
                auto_renewal_id=pk,
                user_id=str(request.user.id)
            )
            
            serializer = AutoRenewalSerializer(auto_renewal)
            return Response({
                'data': 'Auto-renewal canceled successfully.',
                'auto_renewal': serializer.data
            })
            
        except ValidationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Auto-renewal cancellation failed: {str(e)}")
            return Response({'error': 'Auto-renewal cancellation failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='process-due')
    @swagger_helper("AutoRenewal", "process_due")
    def process_due_renewals(self, request):
        """Process all due auto-renewals (admin only)"""
        try:
            role = getattr(request.user, 'role', None)
            if not (request.user.is_superuser or (role and role.lower() == 'superuser')):
                return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
            
            auto_renewal_service = AutoRenewalService(request)
            result = auto_renewal_service.process_due_auto_renewals()
            
            return Response(result)
            
        except Exception as e:
            logger.error(f"Processing due auto-renewals failed: {str(e)}")
            return Response({'error': 'Processing failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
