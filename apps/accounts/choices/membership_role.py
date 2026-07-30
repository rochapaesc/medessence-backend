from django.db.models import TextChoices


class MembershipRole(TextChoices):
    MANAGER = "manager", "Gestor"
    DOCTOR = "doctor", "Médico"
    ATTENDANT = "attendant", "Atendente"
    # Usuário EXTERNO (RF-PAR-6): enxerga só a área de Parceiros. A cerca fica
    # em IsClinicMember - view sem `partner_allowed` o recusa.
    PARTNER = "partner", "Parceiro"
