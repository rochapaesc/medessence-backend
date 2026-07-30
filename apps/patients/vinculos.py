"""
Vínculo número↔paciente (RF-PAC-7.1) — a família que compartilha um telefone.

Fora do viewset porque as regras têm de valer para todo caminho que mexe no
vínculo: o painel do contato hoje, a ficha do paciente amanhã, a ingestão e o
sync do EHR (que já criam vínculo por conta própria).

O invariante que este módulo protege: **um número nunca fica sem principal**
enquanto tiver alguém vinculado. Quem decide para onde vai a mensagem que a
clínica inicia é o principal (RF-PAC-8/RF-INB-11) e o auto-vínculo da conversa
nova (RF-INB-7) também depende dele — sem principal, os dois deixam de achar
alguém EM SILÊNCIO, que é o pior jeito de quebrar.
"""

from django.db import transaction


def _vinculos_vivos(contact):
    from apps.patients.models import PatientContact

    return PatientContact.objects.filter(contact=contact)


def contato_do_numero(clinic, phone, *, display_name=""):
    """
    Contato do número, procurando pelas DUAS grafias do nono dígito (§6.2)
    antes de criar — é o que impede o contato duplicado quando o cadastro tem
    o 9 e a Meta usa a forma curta (ou o contrário).

    Devolve `(contact, criado)`. `(None, False)` se não há número.
    """
    from apps.patients.models import Contact
    from apps.patients.phone import canonizar_telefone, grafia_alternativa

    numero = canonizar_telefone(phone)
    if not numero:
        return None, False

    grafias = [numero]
    alternativa = grafia_alternativa(numero)
    if alternativa:
        grafias.append(alternativa)

    contact = Contact.objects.filter(clinic=clinic, wa_id__in=grafias).first()
    if contact is not None:
        return contact, False
    return (
        Contact.objects.create(
            clinic=clinic, wa_id=numero, display_name=display_name[:160]
        ),
        True,
    )


def tem_principal(contact) -> bool:
    return _vinculos_vivos(contact).filter(is_primary=True).exists()


@transaction.atomic
def vincular(patient, contact):
    """
    Liga paciente e número. O PRIMEIRO do número vira o principal — mesma
    regra do sync do EHR e do `/conversations/start/`.

    Devolve `(vinculo, criado)`.
    """
    from apps.patients.models import PatientContact

    return PatientContact.objects.get_or_create(
        patient=patient,
        contact=contact,
        defaults={"is_primary": not tem_principal(contact)},
    )


@transaction.atomic
def definir_principal(patient, contact):
    """
    Troca o principal do número. Devolve o vínculo promovido.

    A limpeza dos outros vem ANTES da promoção porque a unicidade é imposta
    pelo banco (`uniq_primary_patient_per_contact`, condicional): promover
    primeiro estouraria IntegrityError com o principal antigo ainda de pé.
    """
    from apps.patients.models import PatientContact

    vinculo = _vinculos_vivos(contact).filter(patient=patient).first()
    if vinculo is None:
        raise PatientContact.DoesNotExist("Este paciente não usa este número.")

    _vinculos_vivos(contact).exclude(pk=vinculo.pk).filter(is_primary=True).update(
        is_primary=False
    )
    if not vinculo.is_primary:
        vinculo.is_primary = True
        vinculo.save(update_fields=["is_primary", "updated_at"])
    return vinculo


@transaction.atomic
def desvincular(patient, contact) -> dict:
    """
    Remove o paciente do número. Devolve o que aconteceu, para a tela poder
    contar a verdade:

        {"promoveu": <Patient|None>, "conversas_soltas": <int>}

    Duas consequências que o chamador NÃO deve ter de lembrar:
      1. Saiu o principal e sobrou gente → promove o vínculo MAIS ANTIGO
         (o `pk` menor: quem chegou primeiro é o palpite honesto, e qualquer
         escolha é melhor que número sem principal).
      2. Alguma conversa deste contato aponta para o paciente removido →
         solta a conversa; deixar o vínculo na tela depois de removê-lo do
         número mostraria dado que já não existe.
    """
    from apps.inbox.models import Conversation
    from apps.patients.models import PatientContact

    vinculo = _vinculos_vivos(contact).filter(patient=patient).first()
    if vinculo is None:
        raise PatientContact.DoesNotExist("Este paciente não usa este número.")

    era_principal = vinculo.is_primary
    vinculo.delete()  # soft delete do projeto

    promovido = None
    if era_principal:
        proximo = _vinculos_vivos(contact).order_by("pk").select_related("patient").first()
        if proximo is not None:
            proximo.is_primary = True
            proximo.save(update_fields=["is_primary", "updated_at"])
            promovido = proximo.patient

    soltas = Conversation.objects.filter(contact=contact, patient=patient).update(
        patient=None
    )
    return {"promoveu": promovido, "conversas_soltas": soltas}


def pacientes_do_contato(contact):
    """
    Todos os vínculos vivos do número, principal primeiro e depois por nome —
    a ordem em que a seção "Quem usa este número" é lida.
    """
    return (
        _vinculos_vivos(contact)
        .select_related("patient")
        .order_by("-is_primary", "patient__name")
    )
