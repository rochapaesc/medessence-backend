"""Mascaramento de dado pessoal na saída da API (LGPD, §15)."""


def mask_cpf(value: str | None) -> str:
    """
    Revela os 3 primeiros e os 2 últimos dígitos; o resto vira `*`.

    Mascara na SAÍDA, não no banco: o CPF inteiro segue existindo para o push
    ao EHR e para a busca por CPF, que rodam no servidor. Mascarar só na tela
    não protegeria nada — o payload cru apareceria no devtools.

    Preserva a pontuação que estiver no valor, então
    `123.456.789-00` vira `123.***.***-00` e `12345678900` vira `123******00`.
    """
    if not value:
        return value or ""

    positions = [i for i, char in enumerate(value) if char.isdigit()]
    # Com menos de 6 dígitos, revelar as duas pontas entregaria quase tudo.
    hidden = set(positions if len(positions) < 6 else positions[3:-2])

    return "".join(
        "*" if index in hidden else char for index, char in enumerate(value)
    )


def is_masked(value: str | None) -> bool:
    """
    O valor recebido é uma máscara devolvida pelo cliente?

    Uma tela que exibe `123.***.***-00` e reenvia isso no salvar apagaria o
    documento real - foi o que aconteceu em 21/07/2026: o CPF `12345678909`
    virou `12309` (só os dígitos visíveis) e seguiu para o EHR. Quem escreve
    trata isto como "não mudou".
    """
    return "*" in (value or "")
