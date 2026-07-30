"""
Telefone brasileiro ⇄ wa_id da Meta — a regra do nono dígito (§6.2).

O cadastro tem "(85) 98876-5432"; o WhatsApp identifica o MESMO celular ora com
o 9 (5585988765432), ora sem (558588765432) — contas antigas guardam o wa_id
curto. Errar aqui é conversa criada num número que nunca recebe, ou contato
duplicado quando a pessoa responde.

Base: BrazilPhoneNormalizer do Chatwoot (issue #5840) — o canônico é a forma
COM o 9 —, corrigindo o bug deles: lá QUALQUER número BR fora de 13 dígitos
ganha um 9, inclusive fixo, e a reconciliação nunca funciona para fixos. Aqui
o 9 só entra quando o assinante PODE ser celular (8 dígitos começando em 6-9).
"""

import re

DDI_BR = "55"
# Antes do nono dígito, celular começava em 6-9; fixo começa em 2-5.
_INICIO_DE_CELULAR = ("6", "7", "8", "9")


def so_digitos(valor: str | None) -> str:
    return re.sub(r"\D", "", valor or "")


def _e_br(digits: str) -> bool:
    return digits.startswith(DDI_BR) and len(digits) in (12, 13)


def canonizar_telefone(valor: str | None) -> str:
    """
    Forma canônica de armazenamento (`Patient.phone` e wa_id criado por nós):
    dígitos com DDI; celular BR SEMPRE com o nono 9 (13 dígitos).

    10/11 dígitos sem DDI = número local BR (mesma premissa do `_clean_phone`
    do adapter vSaúde). O que não dá para reconhecer como BR volta só em
    dígitos — guardar o que foi digitado é melhor que "corrigir" para um
    número que não existe.
    """
    digits = so_digitos(valor)
    if not digits:
        return ""
    if len(digits) in (10, 11):
        digits = DDI_BR + digits
    if not _e_br(digits):
        return digits
    assinante = digits[4:]
    if len(assinante) == 8 and assinante[0] in _INICIO_DE_CELULAR:
        return digits[:4] + "9" + assinante
    return digits


def grafia_alternativa(wa_id: str | None) -> str | None:
    """
    A OUTRA grafia do mesmo celular BR (com 9 ⇄ sem 9), ou None quando não
    existe outra: número não-BR, fixo ou tamanho estranho.
    """
    digits = so_digitos(wa_id)
    if not _e_br(digits):
        return None
    assinante = digits[4:]
    if len(assinante) == 9 and assinante[0] == "9" and assinante[1] in _INICIO_DE_CELULAR:
        return digits[:4] + assinante[1:]
    if len(assinante) == 8 and assinante[0] in _INICIO_DE_CELULAR:
        return digits[:4] + "9" + assinante
    return None


def grafias_de_busca(valor: str | None) -> list[str]:
    """
    Todas as grafias sob as quais este número pode viver no banco — o telefone
    entrou por três portas com três formatos (EHR: dígitos com 55; form: o que
    foi digitado; Meta: wa_id com ou sem o 9). Para busca EXATA (`phone__in`).
    """
    digits = so_digitos(valor)
    if not digits:
        return []
    grafias = {digits, canonizar_telefone(digits)}
    alternativa = grafia_alternativa(canonizar_telefone(digits))
    if alternativa:
        grafias.add(alternativa)
    # Formas locais (sem DDI) dos cadastros feitos no form antes desta regra.
    for grafia in list(grafias):
        if grafia.startswith(DDI_BR) and len(grafia) in (12, 13):
            grafias.add(grafia[2:])
    return sorted(grafias)


def pode_ser_celular(valor: str | None) -> bool:
    """
    Fixo não recebe WhatsApp: decide se "conversar" habilita. Permissivo com
    número não-BR (não dá para afirmar que é fixo); vazio é sempre falso.
    """
    canonico = canonizar_telefone(valor)
    if not canonico:
        return False
    if not _e_br(canonico):
        return True
    # Depois do canônico, todo celular BR tem 13 dígitos e assinante em 9;
    # o que sobrou com 12 é fixo (assinante 2-5, que nunca ganha o 9).
    return len(canonico) == 13 and canonico[4] == "9"
