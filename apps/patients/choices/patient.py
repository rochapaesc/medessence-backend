from django.db.models import TextChoices


class Gender(TextChoices):
    UNKNOWN = "unknown", "Não informado"
    MALE = "male", "Masculino"
    FEMALE = "female", "Feminino"


class PatientSource(TextChoices):
    LOCAL = "local", "Cadastrado aqui"
    EHR = "ehr", "Importado do EHR"


class PatientStatus(TextChoices):
    """
    Status CALCULADO (RF-PAC-2, decisão de 09/07/2026) — não é coluna no
    banco; deriva de `last_appointment_at`: ativo (consulta nos últimos
    90 dias), inativo (acima de 90 dias, ou nunca consultou).
    """

    ACTIVE = "active", "Ativo"
    INACTIVE = "inactive", "Inativo"
