"""
A mensagem de resgate (RF-REA-2.2/2.3/2.4).

O que é DA reativação mora aqui; o que vale para todo template mora em
`template_vars`, compartilhado com o Inbox e o nó de fluxo. Resolver variável
em três lugares é como a mensagem começa a sair diferente da prévia que a
clínica aprovou.
"""

from apps.inbox.template_vars import (  # noqa: F401  (reexporta o que já era importado daqui)
    Contexto,
    corpo_do_template,
    nome_proprio,
    variaveis_do_template,
)
from apps.inbox.template_vars import montar as _montar
from apps.inbox.template_vars import valor_da_variavel as _valor
from apps.inbox.template_vars import valores as _valores


def valor_da_variavel(config: dict, patient, clinic) -> str:
    """Uma variável, no contexto da campanha: paciente do cadastro e clínica."""
    return _valor(config, Contexto(clinic=clinic, patient=patient))


def valores(mensagem, patient, clinic) -> dict[str, str]:
    """O mapa `{"1": "Ivanita", "2": "MedEssence"}` para um paciente."""
    if mensagem is None:
        return {}
    return _valores(
        mensagem.template,
        mensagem.variables or {},
        Contexto(clinic=clinic, patient=patient),
    )


def previa(mensagem, patient, clinic) -> str:
    """
    A mensagem montada como vai chegar (RF-REA-2.4).

    Usa um paciente REAL da fila, nunca `[Nome]`: placeholder esconde
    justamente o que quebra - nome em caixa alta vindo do prontuário, nome
    composto comprido, cidade vazia deixando "consulta na ." no meio da frase.
    """
    if mensagem is None or mensagem.template_id is None:
        return ""
    return _montar(
        mensagem.template,
        mensagem.variables or {},
        Contexto(clinic=clinic, patient=patient),
    )
