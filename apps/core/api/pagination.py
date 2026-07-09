from rest_framework.pagination import LimitOffsetPagination


class DefaultLimitOffsetPagination(LimitOffsetPagination):
    """Paginação padrão do projeto: `?limit=&offset=`.

    Tem um teto (`max_limit`) pra o cliente não conseguir pedir a lista inteira
    de uma vez (ex.: `?limit=100000`); qualquer valor acima é capado em 100.
    """

    default_limit = 20
    max_limit = 100
