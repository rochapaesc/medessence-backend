"""
Settings de teste — pytest roda sem Docker: SQLite em memória, cache local,
hash de senha rápido. O settings real continua sendo o config.settings.
"""

from cryptography.fernet import Fernet

from config.settings import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Chave efêmera por sessão de teste — cifra e decifra no mesmo processo.
FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()

CELERY_TASK_ALWAYS_EAGER = True
