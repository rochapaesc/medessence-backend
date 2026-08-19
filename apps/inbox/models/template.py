from django.db.models import (
    CharField,
    JSONField,
    Q,
    TextField,
    UniqueConstraint,
)

from apps.core.models import TenantScopedModel


class WhatsAppTemplate(TenantScopedModel):
    """
    Cache local dos templates aprovados na Meta (§9.5), atualizado pelo beat
    `refresh_wa_templates` (Fatia B). Fora da janela de 24h, o composer só
    permite enviar um destes com status APPROVED (RF-INB-3).
    """

    #: A CONTA da Meta (WABA) de onde este template veio (RF-INB-3.3).
    #:
    #: ⚠️ Template pertence à conta, não ao número: a clínica que troca de
    #: número ou de app passa a usar outra WABA, e sem este campo o catálogo
    #: antigo ficava misturado com o novo, sem como distinguir. É a conta e não
    #: o canal porque a troca mais comum REAPONTA o canal existente, e aí o
    #: registro do canal é o mesmo antes e depois.
    #:
    #: Vazio significa "não sei de que conta veio" e some da tela: é o estado
    #: das linhas anteriores a 19/08/2026 e o que a primeira sincronização
    #: recarimba.
    waba_id = CharField(
        verbose_name="Conta da Meta", max_length=32, blank=True, db_index=True
    )

    name = CharField(verbose_name="Nome", max_length=120)
    language = CharField(verbose_name="Idioma", max_length=10, default="pt_BR")
    category = CharField(verbose_name="Categoria", max_length=30, blank=True)
    status = CharField(verbose_name="Status", max_length=20, blank=True)
    components = JSONField(verbose_name="Componentes", default=list, blank=True)

    #: `POSITIONAL` (`{{1}}`) ou `NAMED` (`{{nome}}`), como a Meta devolve.
    #: Vazio = desconhecido, e aí vale posicional, que é o formato antigo.
    #: Decide o envio: NAMED exige `parameter_name` em cada parâmetro
    #: (RF-INB-3.4), e errar isso é `#132012`.
    parameter_format = CharField(
        verbose_name="Formato dos parâmetros", max_length=20, blank=True
    )

    #: O id da Meta desta VARIANTE de idioma (RF-INB-3.2).
    #:
    #: ⚠️ Sem ele não dá para editar nem apagar uma variante sozinha: a Meta
    #: apaga pelo NOME, e sem `hsm_id` remove todas as línguas de uma vez.
    #: Vazio nos que vieram só da sincronização, que é o caso de todos os
    #: anteriores a 13/08/2026.
    meta_template_id = CharField(
        verbose_name="Id na Meta", max_length=64, blank=True, db_index=True
    )

    #: A nota que a Meta dá ao template pelo comportamento de quem recebe:
    #: `GREEN`, `YELLOW` ou `RED`. Vazia enquanto ela não avalia.
    #:
    #: ⚠️ Vermelho é o passo ANTES de ela pausar o template sozinha - e
    #: template pausado para de enviar no meio de um fluxo, sem ninguém ter
    #: mexido em nada. Chega pelo webhook `message_template_quality_update`.
    quality_score = CharField(verbose_name="Qualidade", max_length=10, blank=True)

    #: Por que a Meta recusou, quando recusou.
    #:
    #: Template recusado NÃO some: fica como rascunho local com o motivo, para
    #: a clínica corrigir em cima do que escreveu em vez de recomeçar.
    rejection_reason = TextField(verbose_name="Motivo da recusa", blank=True)

    class Meta:
        verbose_name = "Template WhatsApp"
        verbose_name_plural = "Templates WhatsApp"
        ordering = ["name"]
        constraints = [
            # ⚠️ A CONTA entra na chave (RF-INB-3.3). Sem ela, dois templates de
            # mesmo nome em contas diferentes não coexistiam: a sincronização
            # sobrescrevia a definição antiga com a nova, e o envio montava os
            # parâmetros com os componentes errados.
            UniqueConstraint(
                fields=["clinic", "waba_id", "name", "language"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_template",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.language})"
