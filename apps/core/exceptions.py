from rest_framework import status
from rest_framework.exceptions import APIException


class ConflictException(APIException):
    """
    Exceção para indicar um conflito, como quando um recurso não pode ser
    excluído devido a dependências.

    Retorna HTTP 409 Conflict com mensagem personalizada.

    Exemplo de uso:
        raise ConflictException("Não é possível excluir: recurso em uso.")
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = "O recurso não pôde ser excluído devido a dependências."
    default_code = "conflict"
