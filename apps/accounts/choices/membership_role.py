from django.db.models import TextChoices


class MembershipRole(TextChoices):
    MANAGER = "manager", "Gestor"
    DOCTOR = "doctor", "Médico"
    ATTENDANT = "attendant", "Atendente"
