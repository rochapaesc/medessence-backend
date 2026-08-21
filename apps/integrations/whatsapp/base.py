"""
Port WhatsApp (§5) - a fronteira entre o MedEssence e o transporte de
mensageria. É o "gateway" do §7: campanha, segmentação e follow-up ficam no
domínio e só falam com o WhatsApp por esta interface.

Adapters normalizam o formato de terceiro na ENTRADA (`parse_webhook` →
`WhatsAppEvent`) e recebem valores já limpos na SAÍDA. O inbox NUNCA vê o
formato Meta cru fora do `raw`/`WebhookEvent` (preservado para replay).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


class WhatsAppEventKind:
    """Tipos de evento normalizados vindos do webhook (formato Meta, §7)."""

    INBOUND = "inbound"  # mensagem recebida do contato
    STATUS = "status"  # atualização de entrega de uma mensagem OUT
    ECHO = "echo"  # mensagem OUT enviada pelo app do celular (coexistência)
    # Preferência de marketing (RF-SEQ-8.1): o contato apertou o botão nativo
    # de parar promoções que a Meta põe nos modelos MARKETING. Não é mensagem
    # e não vira balão - muda um campo do contato.
    PREFERENCE = "preference"
    # Contato da agenda do celular (RF-CON-5.3, coexistência). Chega quando o
    # dono do número mexe na agenda do aparelho. Não é mensagem e não abre
    # conversa: só acrescenta ou atualiza o contato.
    CONTACT_SYNC = "contact_sync"
    # O paciente APAGOU uma mensagem que já tinha mandado (21/08/2026).
    #
    # ⚠️ Só existe em COEXISTÊNCIA, e é por isso que ele passou despercebido:
    # a Meta o documenta numa página à parte da de `unsupported`, e sem estar
    # no mapa de tipos ele caía no balde de "não suportado" - virando um balão
    # novo e vazio na conversa, com o `original_message_id` jogado fora.
    #
    # Não é mensagem e não vira balão: MARCA a mensagem que já está lá.
    REVOKE = "revoke"
    # O paciente EDITOU uma mensagem já enviada (só coexistência, até 15 min
    # depois do envio). Como o revoke: não vira balão, atualiza a que existe.
    EDIT = "edit"
    # O contato TROCOU DE NÚMERO (`system.type=user_changed_number`).
    # ⚠️ Sem tratar isto, o paciente que troca de chip vira um contato NOVO,
    # sem ficha e sem histórico - o mesmo estrago do nono dígito.
    NUMBER_CHANGE = "number_change"


@dataclass(frozen=True)
class WhatsAppEvent:
    """Evento único e normalizado (uma mensagem, um status ou um echo)."""

    kind: str  # WhatsAppEventKind
    provider_message_id: str = ""  # wamid
    wa_id: str = ""  # E.164 sem "+" (contato do outro lado)
    # O identificador da pessoa por empresa ("BR.1234...", RF-CON-6). A Meta o
    # manda SEMPRE, e ele é o único quando o telefone não vem. Não substitui o
    # `wa_id`: os dois viajam juntos e os dois são guardados.
    user_id: str = ""
    message_kind: str = "text"  # já mapeado para MessageKind (choices do inbox)
    body: str = ""
    caption: str = ""
    media_id: str = ""
    mime_type: str = ""
    filename: str = ""  # só documento traz: é o nome que o paciente vê
    # Conteúdo estruturado que não cabe em texto: cartão de contato,
    # coordenadas da localização, id da resposta de botão.
    content_data: dict = field(default_factory=dict)
    # Reação (👍 numa mensagem): NÃO é mensagem nova — é um selo colado numa
    # mensagem que já existe. `reaction_to` é o wamid do alvo; emoji vazio
    # significa que a pessoa REMOVEU a reação.
    reaction_emoji: str = ""
    reaction_to: str = ""
    reply_to_provider_id: str = ""
    # para kind=REVOKE: o wamid da mensagem que o paciente apagou. É a Meta
    # quem diz QUAL foi (`revoke.original_message_id`), então aqui não há
    # adivinhação nenhuma.
    revoked_message_id: str = ""
    # para kind=EDIT: o wamid da mensagem editada; o texto/legenda novos vêm
    # em `body`/`caption`, como numa mensagem comum.
    edited_message_id: str = ""
    # para kind=NUMBER_CHANGE: o número NOVO (`system.wa_id`). O antigo vem em
    # `wa_id`, que neste evento é o `from`.
    new_wa_id: str = ""
    status: str = ""  # para kind=STATUS: sent/delivered/read/failed
    status_error: str = ""  # para status=failed: motivo legível (errors[] da Meta)
    # para kind=PREFERENCE: True quando o contato pediu para PARAR (RF-SEQ-8.1)
    marketing_opt_out: bool = False
    # para kind=CONTACT_SYNC: `add`, `update` ou `remove` (RF-CON-5.3)
    sync_action: str = ""
    wa_timestamp: datetime | None = None  # aware
    contact_name: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str
    status: str = "sent"  # mapeado para MessageStatus
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DownloadedMedia:
    """Bytes do ativo, já baixados pelo adapter.

    O port entrega CONTEÚDO, não URL: na Cloud API a URL de mídia expira em
    ~5 minutos e o download exige o token — devolver a URL crua convidaria o
    caller a baixar sem auth (e foi assim com o Datafy, que dava 30 dias
    públicos). Content vazio = provedor sem bytes (FAKE) → caller pula.
    """

    content: bytes = b""
    mime_type: str = ""


@dataclass(frozen=True)
class Template:
    name: str
    language: str = "pt_BR"
    category: str = ""
    status: str = ""
    components: list = field(default_factory=list)
    #: O id desta variante na Meta.
    #:
    #: ⚠️ Vem no `get_templates` e era DESCARTADO. Sem ele, template
    #: sincronizado não pode ser editado nem apagado por aqui - o que deixava
    #: a clínica sem mexer justamente nos templates que ela já tem.
    meta_id: str = ""

    #: `POSITIONAL` (`{{1}}`) ou `NAMED` (`{{nome}}`).
    #:
    #: ⚠️ Também vinha e era descartado, e o preço apareceu em produção
    #: (18/08): template NOMEADO exige `parameter_name` em cada parâmetro, e
    #: mandar posicional devolve `#132012 Parameter format does not match`.
    #: O caminho é o do Chatwoot (`template_processor_service.rb`), que decide
    #: por este campo.
    parameter_format: str = ""


@dataclass(frozen=True)
class TemplateCriado:
    """
    O que a Meta devolve ao aceitar um template para revisão.

    ⚠️ `id` é o que permite EDITAR e APAGAR uma variante de idioma sozinha
    depois: sem ele, apagar pelo nome remove todas as variantes de uma vez.
    """

    id: str
    status: str = "PENDING"
    category: str = ""


@runtime_checkable
class WhatsAppProvider(Protocol):
    """Interface da F2 - um adapter por provedor (resolvido por canal)."""

    def send_text(self, to: str, body: str, reply_to: str | None = None) -> SendResult:
        """Texto livre - só válido com a janela de 24h aberta (RF-INB-3)."""
        ...

    def send_template(
        self, to: str, name: str, language: str, components: list | None = None
    ) -> SendResult:
        """Template aprovado - único envio permitido fora da janela de 24h."""
        ...

    def send_media(
        self,
        to: str,
        kind: str,
        url_or_id: str,
        caption: str | None = None,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        reply_to: str | None = None,
        is_voice: bool = False,
    ) -> SendResult:
        """Anexo. `url_or_id` aceita caminho local, URL pública ou media id."""
        ...

    def send_buttons(self, to: str, body: str, buttons: list[dict]) -> SendResult:
        """
        Até 3 botões de resposta rápida (F2.6). `buttons` é
        `[{"id": "agendar", "title": "Marcar consulta"}]`.

        O `id` é o que volta no webhook como `content_data["interactive_id"]`,
        e é por ele que o motor de fluxos sabe QUAL caminho o paciente
        escolheu - o título muda quando o gestor reescreve o botão, o id não.
        """
        ...

    def send_list(self, to: str, body: str, button_label: str, sections: list[dict]) -> SendResult:
        """
        Lista de até 10 itens no total (F2.6). `sections` é
        `[{"title": "Especialidades", "rows": [{"id": "cardio", "title": "Cardiologia"}]}]`.
        """
        ...

    def send_reaction(self, to: str, provider_message_id: str, emoji: str) -> SendResult:
        """Cola (ou tira, com `emoji` vazio) o selo numa mensagem da conversa."""
        ...

    def mark_read(self, provider_message_id: str) -> None:
        """`messages/read` no provedor (RF-INB-4)."""
        ...

    def download_media(self, media_id: str) -> DownloadedMedia:
        """Baixa o ativo no provedor (autenticado) - para re-hospedar (RF-INB-6)."""
        ...

    def verify_credentials(self) -> dict:
        """Sonda que NÃO envia nada — a porta de saída do canal desconectado."""
        ...

    def list_templates(self) -> list[Template]:
        """Cache de templates aprovados (beat 6h)."""
        ...

    def update_template(self, meta_template_id: str, payload: dict) -> None:
        """
        Reescreve um template que já está na Meta (RF-INB-3.2.7).

        ⚠️ Ela SUBSTITUI os componentes inteiros, não aplica diferença: o
        payload precisa trazer tudo, inclusive o que não mudou.
        """
        ...

    def delete_template(self, name: str, meta_template_id: str = "") -> None:
        """
        Apaga um template na Meta.

        ⚠️ Sem `meta_template_id` ela apaga TODAS as variantes de idioma com
        aquele nome, e não só a que se pediu.
        """
        ...

    def create_template(self, payload: dict) -> TemplateCriado:
        """
        Manda um template para a revisão da Meta (RF-INB-3.2).

        O `payload` já vem montado e validado por `template_builder`: aqui só
        se fala com a Meta, para o erro dela chegar traduzido em vez de
        estourar como exceção do PyWa no meio da view.
        """
        ...

    def parse_webhook(self, payload: dict) -> list[WhatsAppEvent]:
        """Formato Meta (`entry[].changes[].value.*`) → eventos normalizados."""
        ...
