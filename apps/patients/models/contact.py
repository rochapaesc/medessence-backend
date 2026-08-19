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
        blank=True,
        help_text=(
            "E.164 sem '+' (formato Meta). ⚠️ Pode vir VAZIO desde a F2.7: a "
            "Meta esconde o telefone de quem adota nome de usuário e não fala "
            "com a clínica há 30 dias (RF-CON-6)."
        ),
    )
    user_id = CharField(
        verbose_name="Identificador da Meta",
        max_length=40,
        blank=True,
        db_index=True,
        help_text=(
            "O BSUID, no formato 'BR.1234...' (RF-CON-6). Identifica a pessoa "
            "PARA ESTA EMPRESA e é o único caminho quando o telefone não vem. "
            "⚠️ Ele MUDA quando a pessoa troca de telefone: é identificador de "
            "conversa, não identidade do paciente."
        ),
    )
    display_name = CharField(verbose_name="Nome no WhatsApp", max_length=160, blank=True)
    marketing_opt_out = BooleanField(
        verbose_name="Pediu para não receber promoções",
        default=False,
        help_text=(
            "RF-SEQ-8. Sai do botão nativo da Meta nos modelos de marketing "
            "(webhook `user_preferences`) ou da mão da equipe. O pedido é do "
            "NÚMERO: se dois pacientes usam o mesmo aparelho, a casa pediu "
            "para parar. Barra sequência de marketing e modelo MARKETING; "
            "confirmação de consulta continua saindo."
        ),
    )

    class Meta:
        verbose_name = "Contato"
        verbose_name_plural = "Contatos"
        constraints = [
            # Unicidade entre registros VIVOS, como nas demais constraints do
            # projeto — sem a condição, um contato soft-deletado travava a
            # recriação do mesmo número para sempre (era a única sem ela).
            #
            # ⚠️ E entre os que TÊM número: desde a F2.7 o contato pode nascer
            # sem telefone (RF-CON-6.4), e sem o `~Q(wa_id="")` o segundo
            # contato sem número colidiria com o primeiro no vazio.
            UniqueConstraint(
                fields=["clinic", "wa_id"],
                condition=Q(deleted_at__isnull=True) & ~Q(wa_id=""),
                name="uniq_contact_wa_id",
            ),
            # A mesma regra pelo outro identificador: dois contatos com o mesmo
            # `user_id` seriam a mesma pessoa em duas linhas, e as conversas
            # dela se dividiriam entre as duas.
            UniqueConstraint(
                fields=["clinic", "user_id"],
                condition=Q(deleted_at__isnull=True) & ~Q(user_id=""),
                name="uniq_contact_user_id",
            ),
        ]

    @property
    def destino(self) -> str:
        """
        Por qual identificador se fala com este contato (RF-CON-6.3).

        ⚠️ **O telefone vem primeiro**, e o identificador da Meta só entra
        quando ele não existe. Duas razões: o BSUID é REGENERADO quando a
        pessoa troca de telefone (a Meta o documenta), então um guardado aqui
        pode estar velho; e a própria Meta dá precedência ao telefone quando
        recebe os dois. O caminho por `user_id` existe para o caso que a F2.7
        veio resolver, que é o telefone não vir mais.

        O adapter reconhece o formato `BR.1234...` e o manda no campo certo, e
        por isso quem envia não precisa saber qual dos dois está usando.
        """
        return self.wa_id or self.user_id

    def __str__(self):
        return self.display_name or self.wa_id or self.user_id


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
