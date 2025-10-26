from datetime import timedelta
from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.getenv("SECRET_KEY")

DEBUG = os.getenv("DEBUG")

INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'drf_yasg',
    'django_filters',
    'rest_framework',
    'corsheaders',
    'api',
    'apps.billing',
    'apps.payment',
    'apps.superadmin',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'apps')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

LANGUAGE_CODE = 'en-us'
USE_I18N = True
USE_TZ = True
TIME_ZONE = "Africa/Lagos"

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 10,
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'config.authentication.CustomJWTAuthentication',
    ),
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=10),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'ROTATE_REFRESH_TOKENS': False,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': False,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': os.getenv('JWT_SECRET_KEY'),
    'VERIFYING_KEY': None,
    'AUDIENCE': 'billing-ms',
    'ISSUER': 'identity-ms',
    'AUTH_HEADER_TYPES': ('JWT',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    'TOKEN_TYPE_CLAIM': 'token_type',
    'JTI_CLAIM': 'jti',
    'TOKEN_USER_CLASS': 'rest_framework_simplejwt.models.TokenUser',
}

SWAGGER_SETTINGS = {
    'SECURITY_DEFINITIONS': {
        'JWT': {
            'type': 'apiKey',
            'name': 'Authorization',
            'in': 'header',
            'description': 'Enter JWT token as: JWT <token>',
        }
    },
    'USE_SESSION_AUTH': False,
    'PERSIST_AUTH': True,
    'REFRESH_URL': os.getenv('IDENTITY_MICROSERVICE_URL', 'http://localhost:8000') + '/api/v1/user/login/refresh-token/',
}

CORS_ALLOWED_ORIGINS = [
    os.getenv("FRONTEND_PATH", "http://localhost:5173"),
    os.getenv("IDENTITY_MICROSERVICE_URL", "http://localhost:8000"),
]
CORS_ALLOW_HEADERS = ['Authorization', 'Content-Type', 'Accept']
CORS_ALLOW_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']

FRONTEND_PATH = os.getenv("FRONTEND_PATH")
IDENTITY_MICROSERVICE_URL = os.getenv("IDENTITY_MICROSERVICE_URL")
BILLING_MICROSERVICE_URL = os.getenv("BILLING_MICROSERVICE_URL")
FINANCE_MICROSERVICE_URL = os.getenv("FINANCE_MICROSERVICE_URL")
SUPPORT_MICROSERVICE_URL = os.getenv("SUPPORT_MICROSERVICE_URL")
SUPPORT_JWT_SECRET_KEY = os.getenv("SUPPORT_JWT_SECRET_KEY")


PAYMENT_CURRENCY = os.getenv("PAYMENT_CURRENCY", "NGN")

PAYMENT_PROVIDERS = {
    "flutterwave": {
        "verify_url": "https://api.flutterwave.com/v3/transactions/{}/verify",
        "secret_hash": os.getenv("FLW_SECRET_HASH"),
        "secret_key": os.getenv("FLW_SEC_KEY"),
    },
    "paystack": {
        "verify_url": "https://api.paystack.co/transaction/verify/{}",
        "secret_key": os.getenv("PAYSTACK_SEC_KEY"),
    }
}

TRIAL_COOLDOWN_MONTHS = os.getenv("TRIAL_COOLDOWN_MONTHS")
TRIAL_COOLDOWN_DAYS = os.getenv("TRIAL_COOLDOWN_DAYS")
SUBSCRIPTION_GRACE_PERIOD_DAYS = os.getenv("SUBSCRIPTION_GRACE_PERIOD_DAYS")
TRIAL_DURATION_DAYS=os.getenv("TRIAL_DURATION_DAYS")
ENABLE_REFUNDS = False
