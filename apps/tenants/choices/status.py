from django.db.models import TextChoices


class ClinicStatus(TextChoices):
    """
    Estado da clínica na plataforma (RF-ADM-1.4).

    ⚠️ Dois estados, e só dois. A régua de assinatura do §8
    (TRIALING→ACTIVE→PAST_DUE→READ_ONLY→CANCELED) é do billing, que está fora
    por decisão do usuário: enquanto não houver gateway, quem suspende é uma
    PESSOA, escrevendo por quê. Um enum de cinco estados sem nada que os mova
    seria vocabulário para um mecanismo que não existe.
    """

    ACTIVE = "active", "Ativa"
    SUSPENDED = "suspended", "Suspensa"


class SuspensionCategory(TextChoices):
    """
    Por que a clínica foi suspensa (RF-ADM-1.4).

    A categoria é fechada porque ela é para MEDIR e filtrar; o texto livre ao
    lado é para explicar o caso. Só texto livre viraria "falta de pagamento",
    "inadimplente" e "não pagou" na mesma semana.
    """

    NON_PAYMENT = "non_payment", "Falta de pagamento"
    ABUSE = "abuse", "Uso indevido"
    CLINIC_REQUEST = "clinic_request", "A pedido da clínica"
    OTHER = "other", "Outro"
