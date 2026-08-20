"""
As regras de senha do §4.12, fora da view porque três caminhos precisam das
mesmas: a troca pelo Meu perfil (RF-CTA-2), o primeiro acesso (RF-EQP-7) e o
reset feito pelo gestor (RF-EQP-6).
"""

import secrets

from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

# Palavras curtas, sem acento e sem ambiguidade ao ouvir: a senha temporária é
# DITADA no balcão (o produto não tem e-mail para enviá-la), e uma sequência
# embaralhada obrigaria a soletrar letra por letra.
_WORDS = (
    "barco porta folha tarde campo livro ponte praia chuva festa planta janela "
    "cadeira mesa prato copo banco carta caneta papel ilha monte rio mar sol "
    "lua estrela nuvem vento pedra areia flor fruta uva manga pera cavalo gato "
    "peixe pato galo boi mel leite bolo sal arroz massa sopa chave roda trem "
    "carro estrada cidade bairro rua parque jardim horta casa teto muro telha "
    "tijolo madeira ferro vidro linha verde azul amarelo branco preto cinza "
    "claro forte leve alto largo curto novo calmo lento quente frio seco macio "
    "limpo cheio"
).split()

_TEMPORARY_PASSWORD_WORDS = 3


def generate_temporary_password() -> str:
    """
    Senha de primeiro acesso, no formato `tarde-verde-barco-42`.

    São três palavras mais dois dígitos porque duas palavras dariam pouca
    combinação para uma senha que trafega no papel: com esta lista, o espaço é
    de ~46 milhões, e some assim que a pessoa escolhe a dela.
    """
    words = [secrets.choice(_WORDS) for _ in range(_TEMPORARY_PASSWORD_WORDS)]
    return "-".join(words) + f"-{secrets.randbelow(90) + 10}"


def set_user_password(user, raw_password: str, *, temporary: bool = False) -> None:
    """
    Grava a senha e carimba a troca.

    O carimbo e a flag saem na MESMA escrita da senha: se a flag caísse depois,
    existiria um instante em que a senha nova já vale e o gate ainda cobra a
    troca.
    """
    user.set_password(raw_password)
    user.must_change_password = temporary
    user.password_changed_at = timezone.now()
    user.save(update_fields=["password", "must_change_password", "password_changed_at"])


def issue_tokens(user) -> dict[str, str]:
    """
    Par de tokens novo para quem acabou de trocar a própria senha.

    Sem isto, trocar a senha derrubaria também a sessão de quem trocou, que é
    o único lugar onde a troca não deveria ter efeito.
    """
    refresh = RefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}
