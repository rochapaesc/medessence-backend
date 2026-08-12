"""
A mensagem de resgate: quais variáveis o template pede, o que cada uma
recebe e como a mensagem fica (RF-REA-2.2/2.3/2.4).

Fica fora do viewset porque a campanha (RF-REA-2, ainda bloqueada) vai
precisar exatamente das mesmas regras na hora de disparar - e resolver
variável em dois lugares é como a mensagem começa a sair diferente da
prévia que a clínica aprovou.
"""

import re

from apps.inbox.choices import VariableSource

#: `{{1}}`, `{{2}}` ... na ordem em que a Meta numera.
_VARIAVEL = re.compile(r"\{\{\s*(\d+)\s*\}\}")

#: Espelha `Fmt._particulas` do front (`lib/core/utils/formatters.dart`). Os
#: dois lados precisam produzir o MESMO texto: a lista mostra o nome do
#: paciente e a prévia mostra a mensagem com ele dentro, lado a lado na
#: mesma tela.
_PARTICULAS = {"de", "da", "do", "das", "dos", "e", "di", "du"}


def nome_proprio(nome: str | None) -> str:
    """
    Caixa de Título para leitura, mantendo o cadastro como está.

    Nome que JÁ vem com maiúsculas e minúsculas fica intacto: quem digitou
    escolheu, e "McArthur" não pode virar "Mcarthur". O prontuário da clínica
    real guarda tudo em caixa alta ("IVANITA DIAS DE SOUSA"), que é o caso que
    esta função existe para tratar.
    """
    limpo = (nome or "").strip()
    if not limpo or limpo != limpo.upper():
        return limpo
    partes = re.split(r"\s+", limpo.lower())
    return " ".join(
        parte if (i > 0 and parte in _PARTICULAS) else parte[:1].upper() + parte[1:]
        for i, parte in enumerate(partes)
    )


def corpo_do_template(template) -> str:
    """O texto do componente BODY, que é onde as variáveis moram."""
    for componente in (template.components if template else None) or []:
        if componente.get("type") == "BODY":
            return componente.get("text") or ""
    return ""


def variaveis_do_template(template) -> list[str]:
    """
    As variáveis que o template pede, sem repetir e em ordem numérica.

    A Meta numera a partir de 1 e nada garante que venham em ordem no texto,
    nem que não se repitam - `{{1}}` pode aparecer duas vezes na mesma frase.
    """
    achadas = {m.group(1) for m in _VARIAVEL.finditer(corpo_do_template(template))}
    return sorted(achadas, key=int)


def valor_da_variavel(config: dict, patient, clinic) -> str:
    """
    O valor de UMA variável, pela fonte configurada (RF-REA-2.3).

    Fonte desconhecida ou dado vazio devolvem string vazia em vez de estourar:
    a prévia precisa continuar montando para a clínica VER o buraco (cidade em
    branco no meio da frase) em vez de receber uma tela de erro.
    """
    fonte = (config or {}).get("source")
    if fonte == VariableSource.FIXED:
        return ((config or {}).get("value") or "").strip()
    if fonte == VariableSource.CLINIC_NAME:
        return (getattr(clinic, "name", "") or "").strip()
    if fonte == VariableSource.PATIENT_CITY:
        # Mesma Caixa de Título do nome: o prontuário guarda "DOM INOCÊNCIO" e
        # "SÃO JOÃO DO PIAUÍ", e a mensagem sairia gritando o nome da cidade no
        # meio da frase. As partículas de/do/da valem igual para topônimo.
        return nome_proprio(getattr(patient, "city", ""))
    if fonte == VariableSource.PATIENT_FULL_NAME:
        return nome_proprio(getattr(patient, "name", ""))
    if fonte == VariableSource.PATIENT_FIRST_NAME:
        completo = nome_proprio(getattr(patient, "name", ""))
        return completo.split(" ")[0] if completo else ""
    return ""


def valores(mensagem, patient, clinic) -> dict[str, str]:
    """O mapa `{"1": "Ivanita", "2": "MedEssence"}` para um paciente."""
    mapa = (mensagem.variables if mensagem else None) or {}
    return {
        chave: valor_da_variavel(mapa.get(chave), patient, clinic)
        for chave in variaveis_do_template(mensagem.template if mensagem else None)
    }


def previa(mensagem, patient, clinic) -> str:
    """
    A mensagem montada como vai chegar (RF-REA-2.4).

    Usa um paciente REAL da fila, nunca `[Nome]`: placeholder esconde
    justamente o que quebra - nome em caixa alta vindo do prontuário, nome
    composto comprido, cidade vazia deixando "consulta na ." no meio da frase.
    """
    if mensagem is None or mensagem.template_id is None:
        return ""
    resolvidos = valores(mensagem, patient, clinic)
    return _VARIAVEL.sub(lambda m: resolvidos.get(m.group(1), ""), corpo_do_template(mensagem.template))
