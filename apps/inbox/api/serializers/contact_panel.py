"""
O painel do contato: quem está do outro lado, em uma chamada só.

Chatwoot divide isso em cinco requisições (contato, notas, arquivos, conversas
anteriores, atributos) porque cada acordeão carrega sob demanda. Aqui vai
junto: o painel abre inteiro, a recepção não espera cinco vezes, e o custo é
uma consulta a mais numa tela que já está aberta.

O que NÃO vem: CPF completo. Sai mascarado, como na listagem de pacientes — o
documento inteiro é da ficha (§15), onde o acesso é auditado. Um painel lateral
de atendimento não é lugar de expor documento.
"""

from datetime import date

from rest_framework.serializers import ModelSerializer, SerializerMethodField

from apps.patients.models import ContactNote


def _idade(nascimento) -> int | None:
    if nascimento is None:
        return None
    hoje = date.today()
    fez_aniversario = (hoje.month, hoje.day) >= (nascimento.month, nascimento.day)
    return hoje.year - nascimento.year - (0 if fez_aniversario else 1)


class ContactNoteSerializer(ModelSerializer):
    """Anotação sobre a pessoa — sobrevive ao encerramento da conversa."""

    # O nome por extenso, não o id: a assinatura da nota é lida, e um id
    # obrigaria a tela a buscar o nome nota a nota.
    author_name = SerializerMethodField()

    class Meta:
        model = ContactNote
        fields = ["id", "contact", "body", "author_name", "created_at"]

    def get_author_name(self, obj) -> str:
        if not obj.author_id:
            return ""
        return obj.author.get_full_name() or obj.author.email


def paciente_payload(patient, *, is_primary=True) -> dict | None:
    from apps.core.masking import mask_cpf

    if patient is None:
        return None
    return {
        "id": patient.pk,
        "name": patient.name,
        "cpf": mask_cpf(patient.cpf),
        "birth_date": patient.birth_date,
        "age": _idade(patient.birth_date),
        "insurance_name": patient.insurance_name,
        "phone": patient.phone,
        "is_primary": is_primary,
        # A agenda é o que a recepção mais precisa saber antes de responder:
        # metade das perguntas do WhatsApp é sobre a consulta que vem aí.
        # Sem isto, quem atende abre a ficha em outra aba para conferir.
        "last_appointment_at": patient.last_appointment_at,
        "next_appointment_at": patient.next_appointment_at,
    }


def arquivo_payload(message, request) -> dict:
    """Uma mídia da conversa, com de quem veio — a galeria mistura os dois
    lados e sem isso não dá para saber quem mandou o laudo."""
    from apps.inbox.api.serializers.message import media_payload

    payload = media_payload(message.media, request) or {}
    payload["message_id"] = message.pk
    payload["direction"] = message.direction
    payload["wa_timestamp"] = message.wa_timestamp
    return payload


def atendimento_anterior_payload(evento) -> dict:
    """
    Um atendimento já encerrado deste contato.

    NÃO é a "conversa anterior" do Chatwoot, e a diferença importa: lá cada
    atendimento abre uma conversa nova, então listar conversas lista o
    histórico. Aqui a conversa é ÚNICA por contato (`uniq_conversation_channel_contact`)
    e a thread não quebra — o que se encerra é o atendimento, marcado na linha
    do tempo como evento `resolved`.

    Listar conversas aqui devolveria quase sempre uma lista vazia, com cara de
    "esta pessoa nunca falou antes" para quem fala com a clínica há dois anos.
    """
    return {
        "id": evento.pk,
        "conversation": evento.conversation_id,
        "at": evento.wa_timestamp,
        "by": (
            (evento.sent_by.get_full_name() or evento.sent_by.email)
            if evento.sent_by_id
            else ""
        ),
    }
