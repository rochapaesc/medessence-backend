from django.db.models import (
    CASCADE,
    SET_NULL,
    BooleanField,
    CharField,
    ForeignKey,
    Q,
    TextField,
    UniqueConstraint,
)

from apps.core.models import BaseModel, TenantScopedModel


class Contact(TenantScopedModel):
    """
    Um número de WhatsApp; pode atender N pacientes (RF-PAC-7 - responsável
    familiar). O vínculo com Patient é N:N via PatientContact.
    """

    wa_id = CharField(
        verbose_name="Número (wa_id)",
        max_length=20,
        help_text="E.164 sem '+' (formato Meta).",
    )
    display_name = CharField(verbose_name="Nome no WhatsApp", max_length=160, blank=True)

    class Meta:
        verbose_name = "Contato"
        verbose_name_plural = "Contatos"
        constraints = [
            UniqueConstraint(fields=["clinic", "wa_id"], name="uniq_contact_wa_id"),
        ]

    def __str__(self):
        return self.display_name or self.wa_id


class PatientContact(BaseModel):
    """Vínculo N:N contato↔paciente, com UM paciente principal por contato (M4)."""

    patient = ForeignKey(
        "patients.Patient",
        verbose_name="Paciente",
        on_delete=CASCADE,
        related_name="patient_contacts",
    )
    contact = ForeignKey(
        Contact,
        verbose_name="Contato",
        on_delete=CASCADE,
        related_name="patient_contacts",
    )
    is_primary = BooleanField(
        verbose_name="Paciente principal",
        default=False,
        help_text="Desambiguação do Inbox: a quem o número pertence por padrão.",
    )

    class Meta:
        verbose_name = "Vínculo contato-paciente"
        verbose_name_plural = "Vínculos contato-paciente"
        constraints = [
            # M4 - unicidade entre registros vivos (soft delete não bloqueia recriação)
            UniqueConstraint(
                fields=["patient", "contact"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_patient_contact",
            ),
            UniqueConstraint(
                fields=["contact"],
                condition=Q(is_primary=True, deleted_at__isnull=True),
                name="uniq_primary_patient_per_contact",
            ),
        ]

    def __str__(self):
        return f"{self.contact} → {self.patient}"


class ContactNote(TenantScopedModel):
    """
    Anotação sobre a PESSOA do outro lado do número — não sobre o atendimento.

    Diferente da nota interna da conversa (RF-ATD-3), que morre junto com o
    atendimento encerrado. "Prefere ser chamada de Malu", "o filho João agenda
    por ela", "não atende antes das 10h" precisa sobreviver ao encerramento e
    aparecer na próxima vez que este número escrever. É a nota do contato do
    Chatwoot (`ContactNotes`) e do wacrm (`contact_notes`).

    Não é dado clínico: mora no contato, não no prontuário.
    """

    contact = ForeignKey(
        Contact,
        verbose_name="Contato",
        on_delete=CASCADE,
        related_name="notes",
    )
    author = ForeignKey(
        "accounts.User",
        verbose_name="Autor",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="contact_notes",
    )
    body = TextField(verbose_name="Anotação")

    class Meta:
        verbose_name = "Nota do contato"
        verbose_name_plural = "Notas do contato"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.contact}: {self.body[:40]}"
