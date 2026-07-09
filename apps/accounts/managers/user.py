from django.contrib.auth.models import BaseUserManager as DJUserManager


class UserManager(DJUserManager):
    """
    Manager personalizado para o modelo de usuário que utiliza
    email como campo principal de autenticação.
    """

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("O usuário deve ter um email.")

        if not password:
            raise ValueError("O usuário deve ter uma senha.")

        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)

        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)

        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        # Superuser técnico também opera o plano plataforma
        extra_fields.setdefault("is_platform_admin", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("O superusuário deve ter is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("O superusuário deve ter is_superuser=True.")

        return self._create_user(email, password, **extra_fields)
