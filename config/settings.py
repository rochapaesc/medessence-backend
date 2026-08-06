from datetime import timedelta
from pathlib import Path

from celery.schedules import crontab
from decouple import config
from kombu import Queue

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("SECRET_KEY", default="^a-^xa(@")

DEBUG = config("DEBUG", default=False, cast=bool)

ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="*").split(",")

CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="*").split(",")

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
]

THIRD_PART_APPS = [
    "channels",  # tempo real (§12) - habilita o ASGI_APPLICATION do Channels
    "rest_framework",
    "django_filters",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "corsheaders",
    "django_celery_beat",
    # "anymail",
]

LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.tenants",
    "apps.patients",
    "apps.scheduling",
    "apps.inbox",
    "apps.automation",
    "apps.integrations",
    "apps.notifications",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PART_APPS + LOCAL_APPS

REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "apps.core.api.pagination.DefaultLimitOffsetPagination",
    "DEFAULT_AUTHENTICATION_CLASSES": (
        [
            "rest_framework_simplejwt.authentication.JWTAuthentication",
            "rest_framework.authentication.SessionAuthentication",
        ]
        if DEBUG
        else ["rest_framework_simplejwt.authentication.JWTAuthentication"]
    ),
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "PAGE_SIZE": 10,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "MedEssence API",
    "DESCRIPTION": "API SaaS multi-tenant de relacionamento com pacientes",
    "VERSION": "1.0.0",
    "TAGS": [
        {
            "name": "auth",
            "description": "Autenticação (JWT) e sessão",
        },
        {
            "name": "me",
            "description": "Perfil do usuário logado e seus vínculos com clínicas",
        },
        {
            "name": "core",
            "description": "Recursos internos e auditoria",
        },
    ],
    "COMPONENT_SPLIT_REQUEST": True,
    "SORT_OPERATIONS": False,
}


SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=4),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": False,
    "ALGORITHM": "HS256",
    "VERIFYING_KEY": None,
    "AUDIENCE": None,
    "ISSUER": None,
    "JSON_ENCODER": None,
    "JWK_URL": None,
    "LEEWAY": 0,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_HEADER_NAME": "HTTP_AUTHORIZATION",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "USER_AUTHENTICATION_RULE": (
        "rest_framework_simplejwt.authentication.default_user_authentication_rule"
    ),
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "TOKEN_TYPE_CLAIM": "token_type",
    "TOKEN_USER_CLASS": "rest_framework_simplejwt.models.TokenUser",
    "JTI_CLAIM": "jti",
    "TOKEN_OBTAIN_SERIALIZER": "rest_framework_simplejwt.serializers.TokenObtainPairSerializer",
    "TOKEN_REFRESH_SERIALIZER": "rest_framework_simplejwt.serializers.TokenRefreshSerializer",
    "TOKEN_VERIFY_SERIALIZER": "rest_framework_simplejwt.serializers.TokenVerifySerializer",
    "TOKEN_BLACKLIST_SERIALIZER": "rest_framework_simplejwt.serializers.TokenBlacklistSerializer",
}

LOG_LEVEL = config("LOG_LEVEL", default="INFO")
LOG_FORMAT = config("LOG_FORMAT", default="verbose")  # "verbose" | "json"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname:<8} {name} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {message}",
            "style": "{",
        },
        "json": {
            "()": "logging.Formatter",
            "format": (
                '{"time":"%(asctime)s","level":"%(levelname)s",'
                '"logger":"%(name)s","message":"%(message)s"}'
            ),
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": LOG_FORMAT,
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        # Django interno - warnings e acima para evitar ruído
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",  # 4xx/5xx
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["console"],
            "level": "WARNING",  # mude para DEBUG para ver SQL
            "propagate": False,
        },
        "django.security": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        # Nosso código
        "apps": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        # Falhas do AuditLog (do logger.exception em apps.core.audit)
        "apps.core.audit": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        # Faker (seed) é ruidoso demais em DEBUG
        "faker": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        # Celery: recebimento/conclusão de tasks visíveis no docker logs do
        # worker (sem isto o logger fica órfão e o worker parece "mudo").
        "celery": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "celery.task": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serve /static/ do STATIC_ROOT sob qualquer servidor ASGI/WSGI (produção
    # sem nginx). Em DEBUG quem serve é o ASGIStaticFilesHandler do asgi.py,
    # que lê direto das pastas dos apps (sem precisar de collectstatic).
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# O projeto nasce ASGI (Channels entra na F2 sem migração de servidor)
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": config("DB_ENGINE", default="django.db.backends.postgresql"),
        "NAME": config("DB_NAME", default="github-actions"),
        "USER": config("DB_USER", default="postgres"),
        "PASSWORD": config("DB_PASSWORD", default="postgres"),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "pt-br"

TIME_ZONE = "America/Fortaleza"

USE_I18N = True

USE_TZ = True

STATIC_URL = "/static/"

STATIC_ROOT = BASE_DIR / "static/"

MEDIA_URL = "/media/"

MEDIA_ROOT = BASE_DIR / "media/"

# WhiteNoise: comprime (gzip/brotli) e versiona os estáticos com hash no nome,
# permitindo cache imutável de longo prazo. Aplica-se em produção (o manifesto
# é gerado no collectstatic); em DEBUG quem serve é o ASGIStaticFilesHandler.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "/secure-admin/login/"
LOGOUT_REDIRECT_URL = "swagger-ui"

# Chave Fernet (base64, 32 bytes) usada pelo EncryptedJSONField - credenciais de
# integração (Clinic.ehr_credentials, Channel.credentials) cifradas em repouso.
# Gere com:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FIELD_ENCRYPTION_KEY = config("FIELD_ENCRYPTION_KEY")

CORS_ALLOW_ALL_ORIGINS = True

# O front web envia o contexto de clínica no header X-Clinic-Id (§3.1); sem
# liberá-lo, o preflight CORS barra TODA request escopada.
from corsheaders.defaults import default_headers  # noqa: E402

CORS_ALLOW_HEADERS = (*default_headers, "x-clinic-id")

SESSION_CACHE_ALIAS = "default"

CACHE_TTL = 60 * 1
CACHE_TTL_REMOTE_CONFIG = 60 * 24

REDIS_HOST = config("REDIS_HOST", default="localhost")
REDIS_PORT = config("REDIS_PORT", default="6379")
REDIS_PASSWORD = config("REDIS_PASSWORD", default="")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0",
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    },
}

# Channel layer do tempo real (§12) - mesmo Redis, DB separado (/1) do
# cache/broker (/0). O front recebe eventos; a fonte da verdade é a API REST.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/1"],
        },
    },
}

CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default="redis://localhost:6379/0")

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RUN_INTERVAL_MINUTES = int(config("CELERY_RUN_INTERVAL_MINUTES", default=1))

CELERY_EMAIL_QUEUE = "email"
CELERY_SYNC_QUEUE = "sync"
CELERY_DEFAULT_QUEUE = "default"
# Filas do inbox (F2, §13)
CELERY_WEBHOOKS_QUEUE = "webhooks"
CELERY_OUTBOUND_QUEUE = "outbound"
CELERY_MEDIA_QUEUE = "media"
# Fila da automação (F2.6, §13). Precisa estar DECLARADA aqui: o worker sobe
# sem `-Q` e consome exatamente as filas desta tupla - task apontada para uma
# fila de fora dela fica enfileirada para sempre, sem erro nenhum.
CELERY_AUTOMATION_QUEUE = "automation"

CELERY_QUEUES = (
    Queue(CELERY_DEFAULT_QUEUE, routing_key=CELERY_DEFAULT_QUEUE),
    Queue(CELERY_EMAIL_QUEUE, routing_key=CELERY_EMAIL_QUEUE),
    Queue(CELERY_SYNC_QUEUE, routing_key=CELERY_SYNC_QUEUE),
    Queue(CELERY_WEBHOOKS_QUEUE, routing_key=CELERY_WEBHOOKS_QUEUE),
    Queue(CELERY_OUTBOUND_QUEUE, routing_key=CELERY_OUTBOUND_QUEUE),
    Queue(CELERY_MEDIA_QUEUE, routing_key=CELERY_MEDIA_QUEUE),
    Queue(CELERY_AUTOMATION_QUEUE, routing_key=CELERY_AUTOMATION_QUEUE),
)

CELERY_TASK_DEFAULT_QUEUE = CELERY_DEFAULT_QUEUE
CELERY_TASK_QUEUES = CELERY_QUEUES

# Beat (§13): agenda a cada 10 min; catálogos + pacientes na madrugada.
# Fan-out por tenant acontece dentro das tasks schedule_* (lock por clínica).
CELERY_BEAT_SCHEDULE = {
    # Batimento do worker (apps/core/health.py). A cada minuto porque o que se
    # quer detectar — processamento parado — vira mensagem que não sai para o
    # paciente, e cinco minutos de silêncio já é reclamação na recepção.
    "worker-heartbeat": {
        "task": "apps.core.tasks.worker_heartbeat",
        "schedule": crontab(minute="*"),
    },
    "sync-appointments-fanout": {
        "task": "apps.integrations.tasks.schedule_appointment_syncs",
        "schedule": crontab(minute="*/5"),  # agenda muda rápido - 5 min
    },
    "sync-daily-fanout": {
        "task": "apps.integrations.tasks.schedule_daily_syncs",
        "schedule": crontab(hour=3, minute=0),
    },
    # Cache de templates aprovados do WhatsApp (§13) - a cada 6h.
    "wake-snoozed-conversations": {
        "task": "apps.inbox.tasks.wake_snoozed_conversations",
        "schedule": crontab(minute="*"),  # "volta às 9h" tem precisão de minuto
    },
    "refresh-wa-templates": {
        "task": "apps.inbox.tasks.refresh_wa_templates",
        "schedule": crontab(minute=0, hour="*/6"),
    },
    # Fila de write-back nós → EHR (§10.2): rede de segurança do disparo
    # pós-commit - retoma operações que ficaram PENDING (EHR fora do ar).
    "push-sync-operations": {
        "task": "apps.integrations.tasks.push_sync_operations",
        "schedule": crontab(minute="*"),
    },
    # Execuções de fluxo (F2.6, RF-FLW-11): espera vencida, silêncio do
    # paciente e conversa que mudou de dono. Um minuto porque o nó "Aguardar"
    # é usado para lembrete com hora marcada.
    "sweep-flow-runs": {
        "task": "apps.automation.tasks.sweep_flow_runs",
        "schedule": crontab(minute="*"),
    },
}


RESET_PASSWORD_TIME = int(config("RESET_PASSWORD_TIME", default=60))

ANYMAIL = {
    "MAILGUN_API_KEY": config("MAILGUN_API_KEY", default=""),
    "MAILGUN_API_URL": config("MAILGUN_API_URL", default="https://api.mailgun.net/v3"),
    "MAILGUN_SENDER_DOMAIN": config("MAILGUN_SENDER_DOMAIN", default="mail.licittudo.com.br"),
}

DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="noreply@mail.licittudo.com.br")
EMAIL_BACKEND = config("EMAIL_BACKEND", default="anymail.backends.mailgun.EmailBackend")
SERVER_EMAIL = config("SERVER_EMAIL", default="noreply@mail.licittudo.com.br")

ASAAS_API_KEY = "$" + config("ASAAS_API_KEY", default="")
ASAAS_API_URL = config("ASAAS_API_URL", default="https://api-sandbox.asaas.com")

# Base URL padrão da vSaúde - pode ser sobrescrita por clínica em
# Clinic.ehr_credentials["base_url"] (instalações self-hosted).
VSAUDE_API_URL = config("VSAUDE_API_URL", default="")

# Trava TEMPORÁRIA de dado de produção (apps/core/api/guards.py): recusa
# excluir registro espelhado do EHR enquanto o dev roda sobre a clínica real.
# Desligar quando o ambiente deixar de apontar para dados de verdade.
EHR_DATA_GUARD = config("EHR_DATA_GUARD", default=True, cast=bool)

# WhatsApp Meta Cloud API (§7) - credenciais DO APP da plataforma. As do
# canal (access_token/phone_number_id/waba_id) ficam cifradas no Channel.
# Vazios = webhook fecha tudo (fail closed) - preencher na calibração.
WHATSAPP_APP_SECRET = config("WHATSAPP_APP_SECRET", default="")
WHATSAPP_VERIFY_TOKEN = config("WHATSAPP_VERIFY_TOKEN", default="")
