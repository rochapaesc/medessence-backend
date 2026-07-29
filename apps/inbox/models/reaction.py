from django.db.models import (
    CASCADE,
    SET_NULL,
    CharField,
    ForeignKey,
    Index,
    Q,
    UniqueConstraint,
)

from apps.core.models import TenantScopedModel
from apps.inbox.choices import ReactionActor


class MessageReaction(TenantScopedModel):
    """
    Uma reação (👍, ❤️…) numa mensagem — UMA LINHA POR ATOR.

    Desenho copiado do wacrm (`message_reactions`, migration 009), depois de
    ler as três referências: o Chatwoot descarta reações e o whatomate guarda
    um array dentro do JSON da mensagem e faz ler-alterar-gravar. A linha por
    ator dispensa essa corrida — quem reage duas vezes bate na chave única.

    A primeira versão nossa era um campo de texto na própria mensagem: cabia
    UMA reação e ninguém sabia de quem era, então a clínica reagindo pelo
    celular apagava a do paciente.

    Emoji vazio não existe aqui: desfazer a reação APAGA a linha (é o que a
    Meta manda — o mesmo evento com emoji em branco).
    """

    message = ForeignKey(
        "inbox.Message",
        verbose_name="Mensagem",
        on_delete=CASCADE,
        related_name="reactions",
    )
    # Desnormalizada de propósito, pelo mesmo motivo do wacrm: o evento de
    # tempo real filtra por conversa, e filtro não faz join.
    conversation = ForeignKey(
        "inbox.Conversation",
        verbose_name="Conversa",
        on_delete=CASCADE,
        related_name="reactions",
    )
    actor_kind = CharField(
        verbose_name="Quem reagiu",
        max_length=7,
        choices=ReactionActor.choices,
    )
    # Só para `actor_kind=AGENT`: qual pessoa da equipe. O contato não tem
    # usuário — ele é o outro lado da conversa.
    actor_user = ForeignKey(
        "accounts.User",
        verbose_name="Atendente",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="message_reactions",
    )
    emoji = CharField(verbose_name="Emoji", max_length=16)

    class Meta:
        verbose_name = "Reação"
        verbose_name_plural = "Reações"
        constraints = [
            # Duas restrições em vez de uma UNIQUE(message, actor_kind,
            # actor_user): no Postgres, NULOS são distintos entre si, então a
            # chave única simples deixaria o contato reagir infinitas vezes.
            UniqueConstraint(
                fields=["message"],
                condition=Q(actor_kind=ReactionActor.CONTACT),
                name="uma_reacao_do_contato_por_mensagem",
            ),
            UniqueConstraint(
                fields=["message", "actor_user"],
                condition=Q(actor_kind=ReactionActor.AGENT),
                name="uma_reacao_por_atendente_por_mensagem",
            ),
        ]
        indexes = [Index(fields=["conversation"])]

    def __str__(self):
        return f"{self.emoji} em #{self.message_id}"
