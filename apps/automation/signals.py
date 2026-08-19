import logging

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.inbox.dispatch import inbound_ingested

logger = logging.getLogger(__name__)


@receiver(inbound_ingested)
def on_inbound_ingested(sender, conversation, message, **kwargs):
    """
    O paciente falou: o fluxo continua ou começa, e quem respondeu sai da
    trilha que pede isso (RF-SEQ-6.2).

    Falha aqui NÃO pode derrubar a ingestão. Uma mensagem que não foi gravada
    porque o motor de fluxos estourou é pior do que um fluxo que não
    respondeu: a recepção deixa de ver que o paciente escreveu, e o webhook da
    Meta ainda seria reentregue tentando de novo.

    ⚠️ São DOIS `try` e não um. A saída da trilha é arrumação de calendário e
    não pode levar junto a resposta do fluxo ao paciente, nem ser levada por
    ela: num único bloco, a primeira exceção calaria a segunda tarefa.
    """
    from apps.automation.sequences import aplicar_saida_por_resposta
    from apps.automation.triggers import handle_inbound

    try:
        handle_inbound(conversation, message)
    except Exception:
        logger.exception("Motor de fluxos falhou no inbound da conversa %s", conversation.pk)

    try:
        if conversation.contact_id:
            quantas = aplicar_saida_por_resposta(conversation.contact)
            if quantas:
                logger.info(
                    "Contato %s respondeu e saiu de %s trilha(s).",
                    conversation.contact_id,
                    quantas,
                )
    except Exception:
        logger.exception("Saída por resposta falhou na conversa %s", conversation.pk)


@receiver(post_save, sender="scheduling.Appointment")
def on_appointment_saved(sender, instance, **kwargs):
    """
    A porta automática da sequência (RF-SEQ-3.4): consulta criada inscreve,
    data mudada reancora, consulta cancelada cancela.

    Escuta o MESMO sinal para os dois caminhos de origem - a consulta marcada
    aqui e a espelhada da vSaúde - porque o pull grava por `update_or_create`,
    que dispara `post_save` igual. Uma porta só, e nenhuma regra duplicada no
    adaptador.

    ⚠️ **Nunca estoura.** Este receptor roda dentro do pull da agenda e dentro
    de toda escrita de consulta: exceção aqui derrubaria a sincronização
    inteira por causa de uma trilha de relacionamento. Mesma regra do
    RF-FLW-21.
    """
    try:
        _sincronizar_inscricoes(instance, created=bool(kwargs.get("created")))
    except Exception:
        logger.exception("Sequência falhou no post_save da consulta %s", instance.pk)


def _sincronizar_inscricoes(appointment, *, created=False):
    from apps.automation.choices import (
        EnrollmentEndReason,
        EnrollmentSource,
        SequenceEnrollmentStatus,
    )
    from apps.automation.models import Sequence, SequenceEnrollment
    from apps.automation.sequences import (
        SemContato,
        aplicar_saida_por_agendamento,
        contato_do_paciente,
        inscrever,
        recalcular,
        remover,
    )
    from apps.scheduling.choices import PRE_ATTENDANCE_STATUSES, AppointmentStatus

    ativas = SequenceEnrollment.objects.filter(
        appointment=appointment, status=SequenceEnrollmentStatus.ACTIVE
    ).select_related("sequence")

    # A consulta saiu do ar (cancelada ou apagada): quem estava ancorado nela
    # sai junto. O D+1 de uma consulta que não vai acontecer é pior do que
    # silêncio.
    if appointment.deleted_at is not None or appointment.status == AppointmentStatus.CANCELED:
        for enrollment in ativas:
            remover(enrollment, reason=EnrollmentEndReason.APPOINTMENT_CANCELED)
        return

    # Reancoragem: a data mudou, então todo o calendário pendente muda com ela.
    # Comparar é idempotente, então rodar a cada save não custa nada quando
    # nada mudou - e dispensa rastrear o valor anterior do campo.
    for enrollment in ativas:
        if enrollment.anchor_at != appointment.starts_at:
            recalcular(enrollment, anchor_at=appointment.starts_at)

    # ---- inscrição ----
    #
    # As duas primeiras guardas são EM MEMÓRIA, e é isso que torna o backfill
    # barato: a clínica real tem 10.508 consultas espelhadas e 156 futuras, e
    # as 10.352 restantes morrem aqui sem tocar no banco.
    if appointment.starts_at <= timezone.now():
        return
    if appointment.status not in PRE_ATTENDANCE_STATUSES:
        return
    if appointment.patient_id is None:
        return

    # Marcar consulta tira das trilhas de divulgação (RF-SEQ-6.2). Vem depois
    # das guardas e só no ATO de marcar: o pull da agenda regrava as mesmas
    # consultas futuras a cada 5 minutos, e sem o `created` cada passada
    # refaria a conta para quem já saiu.
    #
    # Não morde a inscrição logo abaixo: quem sai aqui são as trilhas com
    # `enroll_on_appointment` DESLIGADO, e quem entra ali são as com ele
    # ligado. Conjuntos disjuntos por construção.
    if created:
        aplicar_saida_por_agendamento(appointment)

    candidatas = Sequence.objects.filter(
        clinic_id=appointment.clinic_id, enroll_on_appointment=True, is_active=True
    ).exclude(pk__in=[e.sequence_id for e in ativas])
    if not candidatas:
        return

    try:
        contact = contato_do_paciente(appointment.patient)
    except SemContato:
        # Paciente sem WhatsApp não é erro: é cadastro incompleto, e a conta
        # aparece no painel (RF-SEQ-11) em vez de virar exceção no sync.
        logger.info(
            "Consulta %s não inscreveu: paciente %s sem número.",
            appointment.pk,
            appointment.patient_id,
        )
        return

    for sequence in candidatas:
        inscrever(
            sequence,
            contact,
            source=EnrollmentSource.APPOINTMENT,
            patient=appointment.patient,
            appointment=appointment,
            anchor_at=appointment.starts_at,
        )
