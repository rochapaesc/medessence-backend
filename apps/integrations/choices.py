from django.db.models import TextChoices


class SyncDirection(TextChoices):
    PUSH = "push", "Push (nós → EHR)"
    # PULL reservado - pulls são jobs, não entram na fila de operações


class SyncResource(TextChoices):
    PATIENT = "patient", "Paciente"
    APPOINTMENT = "appointment", "Consulta"
    TAG = "tag", "Tag"
    PATIENT_TAGS = "patient_tags", "Tags do paciente"


class OperationStatus(TextChoices):
    PENDING = "pending", "Pendente"
    RUNNING = "running", "Executando"
    SUCCESS = "success", "Sucesso"
    FAILED = "failed", "Falhou"


class SyncRunKind(TextChoices):
    PATIENTS_FULL = "patients_full", "Pacientes (completo)"
    PATIENTS_DELTA = "patients_delta", "Pacientes (incremental)"
    APPOINTMENTS = "appointments", "Agenda"
    TAGS = "tags", "Tags"
    CATALOGS = "catalogs", "Catálogos"
    MEDICAL_RECORDS = "medical_records", "Prontuário"
