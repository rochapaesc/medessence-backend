"""
Janela de atividade selecionável em tempo de leitura (RF-PAC-2).

O `?window=` sobrepõe a janela ativa apenas para AQUELA consulta - não altera
a configuração da clínica (`Clinic.active_window_days`) nem do profissional.
Valores permitidos: 3/6/12 meses (90/180/360 dias), o padrão do mercado.
"""

from rest_framework.exceptions import ValidationError

ALLOWED_WINDOWS = (90, 180, 360)


def parse_window(request):
    """Devolve o override de janela (int em ALLOWED_WINDOWS) ou None."""
    params = getattr(request, "query_params", None) or request.GET
    raw = params.get("window")
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValidationError({"window": "Janela inválida."}) from None
    if value not in ALLOWED_WINDOWS:
        raise ValidationError({"window": f"Janela deve ser uma de {ALLOWED_WINDOWS} dias."})
    return value


def parse_practitioner_ids(raw) -> list[int]:
    """
    Os ids de profissional pedidos: um, ou vários separados por vírgula.

    ⚠️ Existe para os TRÊS lugares que leem esse parâmetro darem a mesma
    resposta. Eles não davam: a listagem devolvia 400 (`NumberFilter` recusava
    "1,2" antes de o método rodar), o resumo da fila de resgate devolvia 200
    ignorando o filtro em silêncio, e o contador estourava em 500 num
    `filter(pk="1,2")`. Três comportamentos para o mesmo pedido, e a tela de
    Reativação deixa marcar quantos o gestor quiser.

    Valor inválido é DESCARTADO em vez de estourar: o parâmetro vem da URL, e
    derrubar a tela por causa de um id torto é pior do que ignorá-lo.
    """
    return [int(parte) for parte in str(raw or "").split(",") if parte.strip().isdigit()]
