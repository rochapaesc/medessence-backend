"""
O nó que chama um sistema da clínica (RF-FLW-16, cerca do RF-FLW-16.1).

Ficou adiado pela P15 desde o desenho do catálogo, e entrou em 20/08/2026
depois de o usuário aprovar a cerca. O motivo do adiamento não desapareceu, e
é bom lembrar dele lendo este arquivo: um nó que faz POST para uma URL
digitada, com dado de paciente no corpo, é exfiltração e SSRF pela porta da
frente num produto multi-tenant. Sintomático: o wacrm previu `http_fetch` no
schema e **nunca implementou no motor**.

O que segura cada risco, e onde:

| Risco | Onde está a trava |
|---|---|
| URL apontando para dentro da rede | `apps.core.ssrf.check_public_url`, no cadastro E aqui |
| Redirecionamento para dentro | `follow_redirects=False`, neste arquivo |
| Dado demais no corpo | lista fechada de chaves, `_corpo` |
| Endpoint travado segurando o fluxo | `TIMEOUT` curto, uma tentativa só |
| Resposta gigante | `MAX_RESPOSTA` |
| Não saber o que saiu | `AuditAction.HTTP_CALL` |
| Endpoint fora do ar matando a conversa | saída `false` do nó |
"""

import logging

import httpx
from django.utils import timezone

from apps.core.ssrf import BlockedDestination, check_public_url

logger = logging.getLogger(__name__)

# Um nó de fluxo roda com o paciente esperando resposta do outro lado. Cinco
# segundos é o mesmo teto que a referência usa para entrega de webhook.
TIMEOUT = 5.0

# Teto do que lemos de volta. O nó guarda campos da resposta em variáveis, e
# sem teto um endpoint devolvendo um arquivo enorme viraria memória nossa.
MAX_RESPOSTA = 64 * 1024

# Cabeçalho do segredo compartilhado. Vai no cabeçalho, NUNCA na URL: query
# string aparece em log de proxy e em histórico de servidor.
HEADER_SEGREDO = "X-MedEssence-Secret"


def chamar(node, run, conversation) -> bool:
    """
    Executa o nó. Devolve True quando deu certo, e é isso que escolhe a saída.

    ⚠️ **Nunca estoura.** Mesma regra do nó de sequência e do RF-FLW-21:
    exceção aqui derruba o avanço inteiro do fluxo, e a conversa do paciente
    vale mais do que a integração. O que não deu certo vira saída `false`,
    que o gestor liga onde quiser.
    """
    from apps.automation.models import HttpDestination

    cfg = node.config or {}
    destino = HttpDestination.objects.filter(
        pk=cfg.get("destination_id"), clinic=conversation.clinic, is_active=True
    ).first()
    if destino is None:
        # Cadastro apagado ou desligado depois de o fluxo ser publicado. Não é
        # erro do paciente: segue pela falha e registra para o gestor ver.
        logger.warning("Nó %s aponta para destino inexistente ou desligado", node.id)
        return False

    corpo, chaves = _corpo(cfg, run, conversation)

    if run.is_test:
        # RF-FLW-25.4, mesma regra da sequência: no teste a chamada é
        # ANUNCIADA, nunca feita. Disparar de verdade mandaria o contato de
        # teste para dentro do ERP da clínica.
        _auditar(destino, conversation, chaves, situacao="anunciado", codigo=0)
        return True

    try:
        # ⚠️ A cerca roda DE NOVO aqui, e não só no cadastro: o nome pode ter
        # mudado de endereço desde então.
        check_public_url(destino.url)
    except BlockedDestination as erro:
        logger.warning("Destino %s recusado na hora do disparo: %s", destino.pk, erro)
        _auditar(destino, conversation, chaves, situacao="recusado_pela_cerca", codigo=0)
        return False

    cabecalhos = {"Content-Type": "application/json"}
    if destino.secret:
        cabecalhos[HEADER_SEGREDO] = destino.secret

    try:
        with httpx.Client(
            timeout=TIMEOUT,
            # ⚠️ Sem isto a cerca acima é decorativa: uma URL pública responde
            # 302 para 169.254.169.254 e o cliente segue sozinho. A referência
            # do wacrm diz isso por escrito, e é o detalhe que passa
            # despercebido em toda implementação caseira de webhook.
            follow_redirects=False,
        ) as cliente:
            resposta = cliente.post(destino.url, json=corpo, headers=cabecalhos)
            bruto = resposta.read()[:MAX_RESPOSTA]
    except httpx.HTTPError as erro:
        logger.warning("Chamada ao destino %s falhou: %s", destino.pk, erro)
        _auditar(destino, conversation, chaves, situacao="erro_de_rede", codigo=0)
        return False

    _auditar(destino, conversation, chaves, situacao="respondeu", codigo=resposta.status_code)

    # 3xx conta como falha: com o redirecionamento desligado, a resposta não
    # tem conteúdo nenhum e seguir como sucesso mentiria para o fluxo.
    if not 200 <= resposta.status_code < 300:
        return False

    _guardar_resposta(cfg, run, bruto)
    return True


def _corpo(cfg, run, conversation) -> tuple[dict, list[str]]:
    """
    Monta o JSON com uma LISTA FECHADA de chaves (RF-FLW-16.1 item f).

    ⚠️ Nunca "o contexto todo". Mandar o objeto do paciente inteiro para o
    endpoint de uma clínica é exatamente a exfiltração que a P15 nomeia, e ela
    aconteceria por acidente de desenho, não por má fé: é o que qualquer
    implementação faz quando ninguém decide o contrário.

    As fontes que o nó pode escolher são as variáveis coletadas no fluxo, mais
    telefone e nome do contato. Nada de prontuário: se um dia precisar, entra
    aqui de propósito e com o RF por escrito.
    """
    disponiveis = dict(run.vars or {})
    disponiveis["contato_nome"] = getattr(conversation.contact, "display_name", "") or ""
    disponiveis["contato_telefone"] = getattr(conversation.contact, "phone", "") or ""

    pedidas = [str(c).strip() for c in (cfg.get("send") or []) if str(c).strip()]
    corpo = {chave: disponiveis.get(chave, "") for chave in pedidas if chave in disponiveis}
    return corpo, sorted(corpo)


def _guardar_resposta(cfg, run, bruto: bytes) -> None:
    """
    Guarda campos da resposta em variáveis do fluxo, para o nó seguinte usar.

    Só o primeiro nível e só valor simples: um `dict` aninhado virando texto
    no meio de uma mensagem para o paciente é pior do que não guardar nada.
    """
    import json

    mapa = cfg.get("save") or {}
    if not mapa:
        return
    try:
        dados = json.loads(bruto.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(dados, dict):
        return

    run.vars = dict(run.vars or {})
    for campo, var_key in mapa.items():
        chave = str(var_key or "").strip()
        valor = dados.get(campo)
        if chave and isinstance(valor, (str, int, float, bool)):
            run.vars[chave] = str(valor)


def _auditar(destino, conversation, chaves, *, situacao: str, codigo: int) -> None:
    """
    O registro do que saiu (RF-FLW-16.1 item h).

    ⚠️ **As CHAVES, nunca os valores.** O log existe para responder ao titular
    o que foi compartilhado sobre ele; guardar o conteúdo aqui criaria uma
    segunda cópia do dado no lugar em que ninguém procura para expurgar.
    """
    from apps.core.models import AuditLog
    from apps.core.models.audit_log import AuditAction

    AuditLog.objects.create(
        user=None,
        clinic=conversation.clinic,
        action=AuditAction.HTTP_CALL,
        resource="automation.HttpDestination",
        resource_id=str(destino.pk),
        payload={
            "destino": destino.name,
            "url": destino.url,
            "campos_enviados": chaves,
            "situacao": situacao,
            "codigo": codigo,
            "conversa": conversation.pk,
            "quando": timezone.now().isoformat(),
        },
    )
