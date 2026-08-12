from django.db.models import TextChoices


class VariableSource(TextChoices):
    """
    De onde sai o valor de cada `{{n}}` do template (RF-REA-2.3).

    O conjunto é FECHADO de propósito: a alternativa seria aceitar um caminho
    de atributo digitado ("patient.insurance_name"), e aí a mensagem que sai
    para 1.891 pessoas passa a depender de texto livre que ninguém valida.
    """

    PATIENT_FIRST_NAME = "patient_first_name", "Primeiro nome do paciente"
    PATIENT_FULL_NAME = "patient_full_name", "Nome completo do paciente"
    PATIENT_CITY = "patient_city", "Cidade do paciente"
    CLINIC_NAME = "clinic_name", "Nome da clínica"
    FIXED = "fixed", "Texto fixo"
