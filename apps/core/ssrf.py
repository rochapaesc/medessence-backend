"""
Cerca contra SSRF para requisição que SAI do nosso servidor (RF-FLW-16.1).

O problema: quando a URL é escolhida por alguém de fora (aqui, o gestor da
clínica que cadastra um destino) e quem faz o pedido é o nosso servidor, o
endereço vira uma alavanca para alcançar o que só a nossa rede alcança —
`127.0.0.1`, um banco interno, ou a **metadata da nuvem** em
`169.254.169.254`, que devolve credencial de máquina para quem perguntar.

Referência lida: `references/wacrm/src/lib/webhooks/ssrf.ts`, que resolve o
nome e recusa endereço não roteável. Aqui a tabela de faixas dele foi trocada
pelo `ipaddress` da biblioteca padrão, que é mais completo: a regex do
original **deixa passar CGNAT** em alguns casos, e `is_global` cobre também
`192.0.2.0/24`, `198.18.0.0/15` e o endereço mapeado de IPv4 em IPv6.

⚠️ **Risco residual DECLARADO: DNS rebinding.** Entre resolver o nome e abrir
o socket, o nome pode passar a apontar para outro endereço. Fechar isso exige
prender o IP resolvido no socket, o que em `httpx` custa transporte próprio.
A mitigação aqui é resolver o mais perto possível do envio e recusar quando
QUALQUER resposta do DNS for privada — o que também barra o truque de
devolver um endereço público e um interno na mesma resposta.
"""

import socket
from ipaddress import ip_address
from urllib.parse import urlsplit

# Nomes que nem chegam a ser resolvidos. `.internal` é o sufixo que as nuvens
# usam para serviço interno, e `.local` é o mDNS da rede da máquina.
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")
BLOCKED_HOSTS = ("localhost",)

# Só estas portas. Destino legítimo de integração é web; liberar porta
# arbitrária transforma o nó num scanner da rede interna.
ALLOWED_PORTS = (443,)


class BlockedDestination(ValueError):
    """
    O destino não passou na cerca.

    A mensagem é escrita para o gestor que cadastrou a URL, não para o log:
    ela aparece na tela dele no momento do cadastro.
    """


def is_public_address(value: str) -> bool:
    """
    O endereço é roteável na internet pública?

    ⚠️ Usa `is_global`, e NÃO `is_private`: `100.64.0.1` (CGNAT) tem
    `is_private=False` e `is_global=False`, então quem confia em `is_private`
    deixa a faixa inteira passar.
    """
    try:
        return ip_address(value).is_global
    except ValueError:
        return False


def _resolve(host: str) -> list[str]:
    """Todos os endereços do nome, IPv4 e IPv6. Lista vazia se não resolve."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    return sorted({info[4][0] for info in infos})


def check_public_url(url: str) -> list[str]:
    """
    Aprova (ou recusa) a URL, e devolve os endereços para os quais ela aponta.

    Roda no CADASTRO do destino, para o gestor descobrir na hora, **e de novo
    antes de cada disparo**, porque o DNS de hoje não é o de amanhã: um nome
    cadastrado apontando para fora pode passar a apontar para dentro sem que
    ninguém toque no cadastro. Uma consulta de DNS por disparo é barata perto
    do que a alternativa custa.
    """
    partes = urlsplit((url or "").strip())

    if partes.scheme != "https":
        raise BlockedDestination(
            "O endereço precisa começar com https. Sem ele, o que sai daqui "
            "viaja aberto pela rede."
        )
    if partes.username or partes.password:
        raise BlockedDestination(
            "O endereço não pode levar usuário e senha embutidos. Use um "
            "cabeçalho de autorização."
        )

    host = (partes.hostname or "").strip().rstrip(".")
    if not host:
        raise BlockedDestination("O endereço não tem servidor de destino.")

    try:
        porta = partes.port
    except ValueError as erro:
        raise BlockedDestination("A porta do endereço não é um número.") from erro
    if porta is not None and porta not in ALLOWED_PORTS:
        raise BlockedDestination(
            f"A porta {porta} não é aceita. Use a porta padrão do https."
        )

    minusculo = host.lower()
    if minusculo in BLOCKED_HOSTS or minusculo.endswith(BLOCKED_SUFFIXES):
        raise BlockedDestination(
            f'"{host}" é um nome da rede interna. O destino precisa ser um '
            f"endereço que exista na internet."
        )

    # IP digitado direto: não há o que resolver, e a checagem é a mesma.
    if _parece_ip(minusculo):
        if not is_public_address(minusculo):
            raise BlockedDestination(
                f"O endereço {host} é da rede interna e não pode ser destino."
            )
        return [minusculo]

    enderecos = _resolve(host)
    if not enderecos:
        raise BlockedDestination(
            f'Não consegui encontrar "{host}". Confira se o endereço está certo.'
        )
    # TODOS precisam ser públicos. Um nome que devolve um endereço público e
    # um interno na mesma resposta escolheria o interno em metade das vezes.
    internos = [e for e in enderecos if not is_public_address(e)]
    if internos:
        raise BlockedDestination(
            f'"{host}" aponta para um endereço da rede interna '
            f"({internos[0]}) e não pode ser destino."
        )
    return enderecos


def _parece_ip(host: str) -> bool:
    try:
        ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True
