# URL Routes Verification

## ✅ All URLs are properly configured

### 1. PlanView (`/api/v1/billing/plans/`)
- ✅ `GET /plans/` - List all plans
- ✅ `POST /plans/` - Create plan
- ✅ `GET /plans/<uuid:pk>/` - Retrieve plan
- ✅ `PUT /plans/<uuid:pk>/` - Update plan
- ✅ `PATCH /plans/<uuid:pk>/` - Partial update plan
- ✅ `DELETE /plans/<uuid:pk>/` - Delete plan
- ✅ `GET /plans/health/` - Health check

### 2. SubscriptionView (`/api/v1/billing/subscriptions/`)
- ✅ `GET /subscriptions/` - List subscriptions
- ✅ `POST /subscriptions/` - Create subscription
- ✅ `GET /subscriptions/<uuid:pk>/` - Retrieve subscription
- ✅ `PUT /subscriptions/<uuid:pk>/` - Update subscription
- ✅ `PATCH /subscriptions/<uuid:pk>/` - Partial update subscription
- ✅ `DELETE /subscriptions/<uuid:pk>/` - Delete subscription
- ✅ `POST /subscriptions/<uuid:pk>/renew/` - Renew subscription
- ✅ `POST /subscriptions/<uuid:pk>/suspend/` - Suspend subscription
- ✅ `POST /subscriptions/<uuid:pk>/change-plan/` - Change plan (upgrade/downgrade)
- ✅ `POST /subscriptions/<uuid:pk>/advance-renewal/` - Advance renewal
- ✅ `POST /subscriptions/<uuid:pk>/extend/` - Extend subscription (when < 30 days remaining)
- ✅ `POST /subscriptions/<uuid:pk>/toggle-auto-renew/` - Toggle auto-renew
- ✅ `GET /subscriptions/<uuid:pk>/audit-logs/` - Get audit logs
- ✅ `POST /subscriptions/activate-trial/` - Activate trial (requires machine_number)
- ⚠️ `POST /subscriptions/check-expired/` - **COMMENTED OUT** (admin-only endpoint)

### 3. CustomerPortalViewSet (`/api/v1/billing/customer-portal/`)
- ✅ `GET /customer-portal/details/` - Get subscription details
- ✅ `POST /customer-portal/change-plan/` - Change plan
- ✅ `POST /customer-portal/advance-renewal/` - Advance renewal
- ✅ `POST /customer-portal/extend/` - Extend subscription
- ✅ `POST /customer-portal/toggle-auto-renew/` - Toggle auto-renew

### 4. AutoRenewalViewSet (`/api/v1/billing/auto-renewals/`)
- ✅ `GET /auto-renewals/` - List auto-renewals
- ✅ `POST /auto-renewals/` - Create auto-renewal
- ✅ `GET /auto-renewals/<uuid:pk>/` - Retrieve auto-renewal
- ✅ `PUT /auto-renewals/<uuid:pk>/` - Update auto-renewal
- ✅ `PATCH /auto-renewals/<uuid:pk>/` - Partial update auto-renewal
- ✅ `DELETE /auto-renewals/<uuid:pk>/` - Delete auto-renewal
- ✅ `POST /auto-renewals/<uuid:pk>/process/` - Manually process renewal
- ✅ `POST /auto-renewals/<uuid:pk>/cancel/` - Cancel auto-renewal
- ✅ `POST /auto-renewals/process-due/` - Process all due renewals (admin)

### 5. AccessCheckView (`/api/v1/billing/access-check/`)
- ✅ `GET /access-check/` - Check access status

### 6. SystemHealthView (`/api/v1/billing/health/`)
- ✅ `GET /health/` - System health check
- ✅ `GET /detailed-health/` - Detailed health check

## Notes

1. **check-expired endpoint is commented out** - This is intentional as it's an admin-only operation that should be run via management command instead.

2. **All new features are properly routed**:
   - ✅ Extend subscription endpoint (both SubscriptionView and CustomerPortalViewSet)
   - ✅ Auto-renewal endpoints (full CRUD + process operations)
   - ✅ Trial activation with machine_number support

3. **URL patterns are consistent**:
   - Detail actions use `<uuid:pk>/` prefix
   - List actions use no prefix
   - All URLs follow RESTful conventions

