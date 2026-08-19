"""
Quem está segurando uma sequência, do ponto de vista da CONVERSA (RF-SEQ-5.5).

O RF-SEQ-5 escolheu que o atendente com a conversa segura o disparo. Este
módulo existe para que a consequência apareça para ele: o Inbox pergunta aqui
"esta conversa está segurando alguma trilha?", e a resposta é a mesma em todo
lugar que perguntar (listagem, evento de tempo real e serializer avulso).

⚠️ Mora em `automation` porque o conceito é daqui, e o `inbox` importa DENTRO
das funções, não no topo: `automation` já importa `inbox.services` no disparo,
e um import de mão dupla no topo fecharia o ciclo.
"""

from django.db.models import Count, OuterRef, Subquery

from apps.automation.choices import HoldReason, SequenceEnrollmentStatus
from apps.automation.models import SequenceEnrollment

#: Nomes das anotações que a listagem de conversas pendura. Ficam aqui, e não
#: soltos no viewset, porque o serializer lê estes mesmos nomes.
ANOTACAO_QUANTAS = "sequencias_segurando_quantas"
ANOTACAO_NOME = "sequencias_segurando_nome"
ANOTACAO_DESDE = "sequencias_segurando_desde"


def _inscricoes_seguradas(contact_id):
    """
    As inscrições ATIVAS que estão paradas esperando esta conversa ficar livre.

    ⚠️ A ligação com a conversa é pelo CONTATO, e isso não é atalho: o disparo
    resolve a conversa com `conversa_para_disparo(clínica, contato)`, que faz
    `get_or_create` por `(clínica, canal, contato)`. A conversa da espera é,
    por construção, a do contato da inscrição.

    ⚠️ `sequence__deleted_at__isnull=True` não é decoração: o gerenciador
    padrão filtra o `deleted_at` da INSCRIÇÃO, não o da SEQUÊNCIA. Foi esse
    mesmo buraco que fez sequência apagada continuar disparando.
    """
    return SequenceEnrollment.objects.filter(
        contact_id=contact_id,
        status=SequenceEnrollmentStatus.ACTIVE,
        hold_reason=HoldReason.BUSY,
        sequence__deleted_at__isnull=True,
        # Trilha desligada segura por OUTRO motivo (`sequence_off`), que não é
        # culpa de quem atende. Se ela foi desligada depois de segurar, o
        # `hold_reason` só muda na próxima varredura, e até lá o aviso mentiria.
        sequence__is_active=True,
    )


def anotar_sequencias_seguradas(queryset):
    """
    Pendura as três anotações na listagem de conversas.

    São subconsultas na MESMA ida ao banco: perguntar por linha custaria trinta
    consultas por página da fila.
    """
    seguradas = _inscricoes_seguradas(OuterRef("contact_id"))
    # A mais antiga é a que o aviso mostra: com duas ou mais, o nome sai da
    # frase e fica a contagem, mas a espera contada é sempre a da primeira.
    primeira = seguradas.order_by("held_since", "pk")
    return queryset.annotate(
        **{
            ANOTACAO_QUANTAS: Subquery(
                seguradas.values("contact_id")
                .annotate(n=Count("pk"))
                .values("n")[:1]
            ),
            ANOTACAO_NOME: Subquery(primeira.values("sequence__name")[:1]),
            ANOTACAO_DESDE: Subquery(primeira.values("held_since")[:1]),
        }
    )


def espera_da_conversa(conversation):
    """
    O mesmo dado para UMA conversa, sem anotação (evento de tempo real e
    serializer de objeto avulso). Devolve `None` quando não há nada esperando.
    """
    primeira = (
        _inscricoes_seguradas(conversation.contact_id)
        .select_related("sequence")
        .order_by("held_since", "pk")
        .first()
    )
    if primeira is None:
        return None
    quantas = _inscricoes_seguradas(conversation.contact_id).count()
    return montar(quantas, primeira.sequence.name, primeira.held_since)


def montar(quantas, nome, desde):
    """Formato único do campo, para os dois caminhos não divergirem."""
    if not quantas:
        return None
    return {
        "quantas": quantas,
        "nome": nome,
        "desde": desde.isoformat() if desde else None,
    }
