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
        raise ValidationError({"window": "Janela inválida."})
    if value not in ALLOWED_WINDOWS:
        raise ValidationError({"window": f"Janela deve ser uma de {ALLOWED_WINDOWS} dias."})
    return value
