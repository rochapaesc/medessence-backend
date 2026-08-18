"""
O motor de sequências (§4.4, RF-SEQ-1 a RF-SEQ-12).

O outro motor de automação, ao lado do de fluxos. A divisão de trabalho é
nítida e vale mais do que qualquer detalhe daqui: **a sequência anda pelo
RELÓGIO e não conversa; o fluxo anda pela RESPOSTA e não decide calendário.**
O ponto de contato é um só, o `start_run` do §4.3.2.

Por que motor separado, e não um nó a mais no fluxo: duas peças do motor de
fluxo dizem não. A varredura mata a execução quando um humano assume (certo
para uma conversa, fatal para uma espera de 55 dias), e a trava de uma
execução ativa por contato ficaria ocupada por semanas, deixando o paciente
sem robô nenhum se ele mandasse "oi" no meio da trilha.

A lógica mora aqui e não nos viewsets porque são QUATRO portas de inscrição
(RF-SEQ-3), e regra que mora no viewset só vale para quem entra pela API -
mesmo motivo do `inbox/attendance.py`.
"""

import logging
import zoneinfo
from datetime import UTC, datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.automation.choices import (
    DispatchSkipReason,
    EnrollmentEndReason,
    FlowNodeType,
    HoldReason,
    SequenceDispatchStatus,
    SequenceEnrollmentStatus,
)
from apps.automation.graph import FlowGraph
from apps.automation.models import (
    DISPATCH_RETRY_MINUTES,
    SequenceDispatch,
    SequenceEnrollment,
)

logger = logging.getLogger(__name__)

# Nós que FALAM com o paciente. A janela de 24h só é problema quando o fluxo
# abre com um destes e ele não é o de modelo aprovado (RF-SEQ-5.3).
NOS_QUE_FALAM = (
    FlowNodeType.SEND_MESSAGE,
    FlowNodeType.SEND_BUTTONS,
    FlowNodeType.SEND_LIST,
    FlowNodeType.SEND_MEDIA,
    FlowNodeType.SEND_TEMPLATE,
    FlowNodeType.COLLECT_INPUT,
)


# Quantas inscrições de um lote saem no mesmo minuto (RF-SEQ-9).
#
# Não é limite do nosso banco, é da Meta: o teto DIÁRIO de conversas iniciadas
# por número é finito, e a régua de qualidade derruba o canal inteiro quando
# muita gente bloqueia de uma vez. Sessenta por minuto espalha a fila real de
# resgate (1.891) por meia hora, que é o suficiente para o envio não parecer
# rajada e ainda terminar no mesmo turno da recepção.
INSCRICOES_POR_MINUTO = 60

# Teto de uma seleção. É o mesmo lote de 1.000 destinatários por difusão que a
# Cloud API impõe (P17), trazido para cá porque o lugar de recusar é onde a
# pessoa ainda pode desfazer a seleção, e não no meio do envio.
MAX_POR_LOTE = 1000

# Deslocamento usado ao renumerar os passos, para nenhum valor temporário
# colidir com um final. `order` é `PositiveSmallInteger` (teto 32.767), então
# dez mil cabe com folga para qualquer trilha de tamanho plausível.
_FOLGA_ORDEM = 10000


class SemContato(ValueError):
    """Paciente sem número de WhatsApp: não há para onde a sequência falar."""


# --------------------------------------------------------------------- #
# Calendário
# --------------------------------------------------------------------- #


def horario_do_passo(step, anchor_at, clinic) -> datetime:
    """
    Quando este passo deve sair (RF-SEQ-2).

    ⚠️ O deslocamento é em DIAS DE CALENDÁRIO no fuso da clínica, e não 24h
    multiplicadas: somar horas atravessa o horário de verão e faz o lembrete
    das 08:00 chegar às 07:00. Converte para o fuso local, anda os dias, crava
    a hora e volta para UTC.
    """
    tz = zoneinfo.ZoneInfo(getattr(clinic, "timezone", None) or "America/Sao_Paulo")
    local = timezone.localtime(anchor_at, tz)
    dia = (local + timedelta(days=step.offset_days)).date()
    return datetime.combine(dia, step.send_time, tzinfo=tz).astimezone(UTC)


def chave_do_passo(step) -> tuple:
    """
    A ordem REAL de um passo é o relógio dele (RF-SEQ-2.2).

    ⚠️ `order` é só desempate entre dois passos no mesmo instante exato, e não
    a sequência. Isto aqui corrige um defeito reproduzido em 13/08: com o motor
    avançando por `order` e calculando a hora por `offset_days`, uma trilha com
    o passo 1 em D+10 e o passo 2 em D+5 disparava o primeiro e agendava o
    segundo para uma data no PASSADO, que era pulada como vencida. O paciente
    nunca recebia o segundo, e o painel dizia "passou da validade" - verdade
    que esconde a causa.
    """
    return (step.offset_days, step.send_time, step.order)


def _passos_vivos(sequence):
    return list(
        sequence.steps.filter(is_active=True).order_by("offset_days", "send_time", "order")
    )


def _proximo_passo(enrollment, depois_de=None):
    """O próximo passo ativo depois deste NO RELÓGIO, ou None quando acabou."""
    passos = _passos_vivos(enrollment.sequence)
    if depois_de is None:
        return passos[0] if passos else None
    atual = chave_do_passo(depois_de)
    for passo in passos:
        if chave_do_passo(passo) > atual:
            return passo
    return None


def normalizar_ordem(sequence) -> int:
    """
    Reescreve `order` a partir do relógio (RF-SEQ-2.2). Devolve quantos mudaram.

    Chamado depois de criar, editar ou apagar um passo. O motor não depende
    disto para funcionar (ele já ordena pelo relógio), mas a tela e a API
    expõem `order`, e campo exposto que discorda do comportamento é a mesma
    armadilha das duas verdades, só que mais devagar.

    ⚠️ Duas passadas por causa da unicidade `(sequence, order)`: renumerar em
    cima colidiria no meio do caminho, porque a trava do banco não é adiável.
    """
    from apps.automation.models import SequenceStep

    passos = list(sequence.steps.order_by("offset_days", "send_time", "order"))
    desejada = {passo.pk: i for i, passo in enumerate(passos, start=1)}
    mudaram = [p for p in passos if p.order != desejada[p.pk]]
    if not mudaram:
        return 0

    with transaction.atomic():
        # Primeiro tira todos do caminho, preservando a distinção entre eles.
        for passo in passos:
            SequenceStep.objects.filter(pk=passo.pk).update(order=passo.order + _FOLGA_ORDEM)
        for passo in passos:
            SequenceStep.objects.filter(pk=passo.pk).update(order=desejada[passo.pk])
    return len(mudaram)


def _agendar(enrollment, step, *, commit=True):
    """Aponta a inscrição para um passo, ou a conclui quando não há mais nenhum."""
    if step is None:
        enrollment.status = SequenceEnrollmentStatus.COMPLETED
        enrollment.end_reason = EnrollmentEndReason.FINISHED
        enrollment.current_step = None
        enrollment.next_dispatch_at = None
    else:
        enrollment.current_step = step
        enrollment.next_dispatch_at = horario_do_passo(
            step, enrollment.anchor_at, enrollment.clinic
        )
    if commit:
        enrollment.save(
            update_fields=[
                "status",
                "end_reason",
                "current_step",
                "next_dispatch_at",
                "updated_at",
            ]
        )
    return enrollment


def reapontar_apos_apagar_passo(step):
    """
    Quem estava PARADO no passo apagado passa ao próximo vivo (RF-SEQ-2.3).

    Sem isto a inscrição fica apontando para um passo morto: o FK sobrevive ao
    soft delete, a varredura ainda o resolveria, e a clínica apagaria um passo
    achando que ele parou de existir. Quem não tem próximo, conclui.
    """
    parados = SequenceEnrollment.objects.filter(
        current_step=step, status=SequenceEnrollmentStatus.ACTIVE
    )
    quantos = 0
    for enrollment in parados:
        _agendar(enrollment, _proximo_passo(enrollment, depois_de=step))
        quantos += 1
    return quantos


def recalcular(enrollment, *, anchor_at=None):
    """
    Reancora e recalcula o que ainda não saiu (RF-SEQ-3.4).

    O que já foi resolvido é histórico e não se mexe. O passo atual pode passar
    a estar no passado: isso é o RF-SEQ-3.5, e quem resolve é a varredura,
    pulando-o por validade em vez de despejar mensagem fora de hora.
    """
    if anchor_at is not None:
        enrollment.anchor_at = anchor_at
        enrollment.save(update_fields=["anchor_at", "updated_at"])
    if enrollment.status != SequenceEnrollmentStatus.ACTIVE:
        return enrollment
    return _agendar(enrollment, enrollment.current_step)


# --------------------------------------------------------------------- #
# Portas de entrada e de saída
# --------------------------------------------------------------------- #


def contato_do_paciente(patient):
    """
    Por qual número esta sequência fala com o paciente (RF-SEQ-3.6).

    Com mais de um, vence o da conversa mais recente; sem conversa nenhuma,
    o vínculo mais novo. Sem número, `SemContato` - a porta que chamou avisa
    quem está na tela, porque silêncio aqui vira paciente que nunca recebe e
    ninguém sabe por quê.
    """
    from apps.inbox.models import Conversation
    from apps.patients.models import PatientContact

    ids = list(
        PatientContact.objects.filter(patient=patient)
        .order_by("-is_primary", "-pk")
        .values_list("contact_id", flat=True)
    )
    if not ids:
        raise SemContato("Paciente sem número de WhatsApp vinculado.")

    recente = (
        Conversation.objects.filter(contact_id__in=ids, last_message_at__isnull=False)
        .order_by("-last_message_at")
        .values_list("contact_id", flat=True)
        .first()
    )
    from apps.patients.models import Contact

    return Contact.objects.get(pk=recente or ids[0])


def contatos_dos_pacientes(patients) -> dict:
    """
    A mesma escolha do `contato_do_paciente`, para MUITOS pacientes de uma vez.

    Existe por causa do lote (RF-SEQ-3.3): resolver um por um custaria duas
    consultas por paciente, e a fila de resgate real tem 1.891. Aqui são duas
    consultas no total, e a regra de desempate é a mesma - vence o número da
    conversa mais recente, senão o vínculo mais novo.

    Devolve `{patient_id: contact}`, e quem não tem número simplesmente não
    aparece no mapa: é o chamador que conta e explica (RF-SEQ-3.6).
    """
    from apps.inbox.models import Conversation
    from apps.patients.models import Contact, PatientContact

    ids = [p.pk for p in patients]
    if not ids:
        return {}

    # Os vínculos de todos, já na ordem de desempate do singular.
    candidatos: dict[int, list[int]] = {}
    for patient_id, contact_id in (
        PatientContact.objects.filter(patient_id__in=ids)
        .order_by("-is_primary", "-pk")
        .values_list("patient_id", "contact_id")
    ):
        candidatos.setdefault(patient_id, []).append(contact_id)

    todos = {cid for lista in candidatos.values() for cid in lista}
    # Quando o contato falou pela última vez. ⚠️ A conversa entra pelo
    # CONTATO, e não pelo `patient` dela: esse campo é nulo em conversa que
    # ninguém vinculou ainda, e filtrar por ele faria o desempate sumir
    # justamente em quem nunca foi identificado.
    ultima: dict[int, object] = {}
    for contact_id, quando in (
        Conversation.objects.filter(contact_id__in=todos, last_message_at__isnull=False)
        .order_by("last_message_at")
        .values_list("contact_id", "last_message_at")
    ):
        ultima[contact_id] = quando

    escolhidos = {}
    for patient_id, lista in candidatos.items():
        com_conversa = [cid for cid in lista if cid in ultima]
        escolhidos[patient_id] = (
            max(com_conversa, key=lambda cid: ultima[cid]) if com_conversa else lista[0]
        )

    objetos = {c.pk: c for c in Contact.objects.filter(pk__in=set(escolhidos.values()))}
    return {pid: objetos[cid] for pid, cid in escolhidos.items() if cid in objetos}


def inscrever_em_lote(sequence, patients, *, source=None) -> dict:
    """
    Põe uma seleção inteira na trilha (RF-SEQ-3.3, RF-REA-2.5).

    Devolve a prestação de contas, e ela é o ponto: quem seleciona 1.891
    pacientes na fila de resgate precisa saber que 140 ficaram de fora por não
    ter número. Recusa em lote que não se explica é pior do que recusa
    nenhuma - a pessoa fica achando que inscreveu todo mundo.

    Os disparos DESLIZAM (RF-SEQ-9): o primeiro passo de um lote grande não
    pode sair todo no mesmo minuto, porque o teto diário de conversas da Meta
    é finito e a régua de qualidade derruba o canal inteiro.
    """
    from apps.automation.choices import EnrollmentSource

    source = source or EnrollmentSource.BATCH
    contatos = contatos_dos_pacientes(patients)

    contas = {"inscritos": 0, "sem_numero": 0, "opt_out": 0, "ja_inscritos": 0}
    for patient in patients:
        contact = contatos.get(patient.pk)
        if contact is None:
            contas["sem_numero"] += 1
            continue
        if sequence.is_marketing and contact.marketing_opt_out:
            contas["opt_out"] += 1
            continue

        enrollment = inscrever(
            sequence,
            contact,
            source=source,
            patient=patient,
            atraso=timedelta(minutes=contas["inscritos"] // INSCRICOES_POR_MINUTO),
        )
        if enrollment is None:
            contas["ja_inscritos"] += 1
        else:
            contas["inscritos"] += 1

    logger.info("inscrever_em_lote(%s): %s", sequence.pk, contas)
    return contas


def inscrever(
    sequence,
    contact,
    *,
    source,
    patient=None,
    appointment=None,
    anchor_at=None,
    atraso=None,
):
    """
    Coloca um contato na trilha (RF-SEQ-3). Devolve a inscrição, ou None
    quando ela não deve existir.

    None é resposta legítima e acontece em três casos: já havia inscrição
    ativa (a trava do banco recusa a segunda), o contato pediu para não
    receber e a sequência é de marketing, ou não sobrou passo nenhum pela
    frente. Quem chama trata os três como no-op.

    `atraso` é o deslizamento do lote (RF-SEQ-9): o primeiro disparo de 1.891
    inscrições não pode sair todo no mesmo minuto.
    """
    if sequence.is_marketing and contact.marketing_opt_out:
        return None

    anchor_at = anchor_at or timezone.now()
    primeiro = _proximo_passo_de(sequence)
    if primeiro is None:
        return None

    try:
        with transaction.atomic():
            enrollment = SequenceEnrollment.objects.create(
                clinic=sequence.clinic,
                sequence=sequence,
                contact=contact,
                patient=patient,
                appointment=appointment,
                anchor_at=anchor_at,
                source=source,
                current_step=primeiro,
                next_dispatch_at=horario_do_passo(primeiro, anchor_at, sequence.clinic),
            )
    except IntegrityError:
        # A trava do RF-SEQ-4: duas portas inscreveram no mesmo instante. Quem
        # chegou primeiro vale, e isto é no-op de propósito.
        return None

    if atraso:
        enrollment.next_dispatch_at = enrollment.next_dispatch_at + atraso
        enrollment.save(update_fields=["next_dispatch_at", "updated_at"])
    return enrollment


def _proximo_passo_de(sequence):
    passos = _passos_vivos(sequence)
    return passos[0] if passos else None


def remover(enrollment, *, reason=EnrollmentEndReason.MANUAL):
    """Tira alguém da trilha antes do fim (RF-SEQ-6). Idempotente."""
    if enrollment.status != SequenceEnrollmentStatus.ACTIVE:
        return enrollment
    enrollment.status = SequenceEnrollmentStatus.CANCELED
    enrollment.end_reason = reason
    enrollment.next_dispatch_at = None
    enrollment.save(
        update_fields=["status", "end_reason", "next_dispatch_at", "updated_at"]
    )
    return enrollment


def aplicar_opt_out(contact):
    """
    O contato pediu silêncio (RF-SEQ-8.2): cancela as inscrições de marketing.

    As operacionais seguem: quem pediu para parar promoção não pediu para
    parar de confirmar consulta.
    """
    ativas = SequenceEnrollment.objects.filter(
        contact=contact,
        status=SequenceEnrollmentStatus.ACTIVE,
        sequence__is_marketing=True,
    )
    for enrollment in ativas:
        remover(enrollment, reason=EnrollmentEndReason.OPTED_OUT)
    return ativas.count()


# --------------------------------------------------------------------- #
# O disparo
# --------------------------------------------------------------------- #


def primeiros_nos_que_falam(graph: FlowGraph) -> list:
    """
    Com que fala este fluxo ABRE, por ramo (RF-SEQ-5.3).

    Caminha do início atravessando o que não fala (condição, etiqueta,
    inscrever) e para no primeiro nó de fala de cada ramo. Quando o fluxo
    ramifica antes de abrir a boca, devolve um por ramo - e a regra de fora da
    janela é conservadora: TODOS precisam ser de modelo aprovado, porque não
    dá para saber por qual ramo aquele paciente vai passar.
    """
    entrada = graph.entry_node
    if not entrada:
        return []

    encontrados, vistos, fila = [], set(), [entrada]
    while fila:
        node_id = fila.pop()
        if node_id in vistos:
            continue
        vistos.add(node_id)
        node = graph.node(node_id)
        if node is None:
            continue
        if node.type in NOS_QUE_FALAM:
            encontrados.append(node)
            continue
        fila.extend(edge.to_id for edge in graph.outgoing(node_id))
    return encontrados


def abre_com_modelo(flow) -> bool:
    """
    Este fluxo pode ABRIR conversa fora da janela de 24h? (RF-SEQ-5.3)

    Público porque a resposta aparece em três lugares: no motor (que segura o
    disparo), no checklist do painel e no drawer de público, onde a campanha
    nasce. Toda conversa que a sequência inicia sai de template.
    """
    return _abre_fora_da_janela(flow)


def _abre_fora_da_janela(flow) -> bool:
    version = flow.current_version
    if version is None:
        return False
    falas = primeiros_nos_que_falam(FlowGraph(version.graph))
    if not falas:
        # Fluxo que não fala não esbarra na janela (só marca etiqueta, por
        # exemplo). Deixa passar: a Meta não é chamada.
        return True
    return all(node.type == FlowNodeType.SEND_TEMPLATE for node in falas)


def _tem_no_de_entrada(flow) -> bool:
    """O grafo publicado tem por onde começar? Sem isso o `start_run` só diz não."""
    version = flow.current_version
    return bool(version and FlowGraph(version.graph).entry_node)


def _e_pos_consulta(previsto, enrollment) -> bool:
    """
    Este passo cai DEPOIS da consulta? (RF-SEQ-7.0)

    ⚠️ Não é o mesmo que "deslocamento positivo", e a diferença tem caso real:
    um passo de D-0 às 18h numa consulta das 14h é pós-consulta, e a definição
    por deslocamento o deixaria passar. Vale para as duas regras que dependem
    disso, a da consulta futura e a da falta.
    """
    return enrollment.appointment_id is not None and previsto > enrollment.anchor_at


def _paciente_faltou(enrollment) -> bool:
    """RF-SEQ-7.1: quem não apareceu não recebe 'como foi o seu atendimento?'."""
    from apps.scheduling.choices import AppointmentStatus

    return (
        enrollment.appointment_id is not None
        and enrollment.appointment.status == AppointmentStatus.NO_SHOW
    )


def _tem_consulta_futura(enrollment) -> bool:
    """RF-SEQ-7: o paciente de retorno não recebe convite de volta já tendo volta."""
    if enrollment.patient_id is None:
        return False

    from apps.scheduling.choices import PRE_ATTENDANCE_STATUSES
    from apps.scheduling.models import Appointment

    qs = Appointment.objects.filter(
        patient_id=enrollment.patient_id,
        starts_at__gt=timezone.now(),
        status__in=PRE_ATTENDANCE_STATUSES,
    )
    if enrollment.appointment_id:
        qs = qs.exclude(pk=enrollment.appointment_id)
    return qs.exists()


def _segurar(enrollment, motivo) -> str:
    """
    Anota POR QUE o disparo não sai agora (RF-SEQ-11), sem consumir o passo.

    O relógio já foi empurrado pela reserva; isto aqui é só a explicação que o
    painel mostra ao lado de "segurando há X". `held_since` é preservado entre
    tentativas de propósito: o que interessa é desde quando está preso, não
    desde a última tentativa.
    """
    campos = ["hold_reason", "updated_at"]
    enrollment.hold_reason = motivo
    if enrollment.held_since is None:
        enrollment.held_since = timezone.now()
        campos.append("held_since")
    enrollment.save(update_fields=campos)
    return f"segurado_{motivo}"


def _soltar(enrollment) -> None:
    """Nada mais segura este disparo. Limpa a anotação se havia uma."""
    if enrollment.held_since is None and not enrollment.hold_reason:
        return
    enrollment.held_since = None
    enrollment.hold_reason = ""
    enrollment.save(update_fields=["held_since", "hold_reason", "updated_at"])


def _resolver(enrollment, step, previsto, *, status, skip_reason="", flow_run=None):
    """Grava o histórico do passo e anda o calendário, numa transação só."""
    _soltar(enrollment)
    with transaction.atomic():
        SequenceDispatch.objects.create(
            enrollment=enrollment,
            step=step,
            scheduled_for=previsto,
            resolved_at=timezone.now(),
            status=status,
            skip_reason=skip_reason,
            flow_run=flow_run,
        )
        _agendar(enrollment, _proximo_passo(enrollment, depois_de=step))


def resolver_disparo(enrollment_id) -> str:
    """
    Resolve UM passo vencido. Devolve o que aconteceu, para o log da varredura.

    A ordem das checagens é do barato ao caro, e cada uma tem efeito
    diferente: SEGURAR não grava linha e o relógio volta depois; PULAR consome
    o passo e anda o calendário; CANCELAR encerra a inscrição.
    """
    agora = timezone.now()

    # A TRAVA e o adiamento são a MESMA escrita: empurrar o relógio reserva a
    # linha para este worker (varreduras sobrepostas não disparam duas vezes) e
    # já deixa o retry pronto se algo falhar no meio. UPDATE condicionado ao
    # estado esperado, como o `_claim_for_bot` do motor de fluxos.
    reservou = SequenceEnrollment.objects.filter(
        pk=enrollment_id,
        status=SequenceEnrollmentStatus.ACTIVE,
        next_dispatch_at__lte=agora,
    ).update(
        next_dispatch_at=agora + timedelta(minutes=DISPATCH_RETRY_MINUTES),
        updated_at=agora,
    )
    if not reservou:
        return "corrida"

    enrollment = (
        SequenceEnrollment.objects.select_related(
            "sequence", "contact", "clinic", "current_step", "current_step__flow"
        )
        .filter(pk=enrollment_id)
        .first()
    )
    if enrollment is None or enrollment.current_step is None:
        return "sumiu"

    step = enrollment.current_step
    previsto = horario_do_passo(step, enrollment.anchor_at, enrollment.clinic)

    # 1. Idempotência: um worker morto entre o disparo e a gravação faria o
    #    mesmo passo sair duas vezes. A unicidade do banco é a rede; esta
    #    consulta evita chegar até ela.
    ja_feito = SequenceDispatch.objects.filter(enrollment=enrollment, step=step).first()
    if ja_feito is not None:
        _agendar(enrollment, _proximo_passo(enrollment, depois_de=step))
        return "ja_resolvido"

    # 2. Sequência APAGADA encerra a inscrição, e não segura.
    #
    # ⚠️ Isto não é zelo: o `delete()` do projeto é SOFT, e uma sequência
    # apagada continua com `is_active=True` no banco. Sem esta guarda, apagar a
    # trilha deixava as inscrições vivas com disparo agendado, e o paciente
    # voltava a receber mensagem de uma sequência que a clínica já tinha
    # aposentado. O viewset cancela as ativas ao aposentar (RF-SEQ-6), mas
    # quem apaga por outro caminho - admin, comando, cascata - não passava por
    # lá. Aqui é a rede que pega todos.
    if enrollment.sequence.deleted_at is not None:
        remover(enrollment, reason=EnrollmentEndReason.SEQUENCE_RETIRED)
        return "cancelado_sequencia_apagada"

    # 3. Desligada ou passo inativo: SEGURA, sem gravar linha. Religar retoma o
    #    calendário de onde parou (RF-SEQ-6.1).
    if not enrollment.sequence.is_active or not step.is_active:
        return _segurar(enrollment, HoldReason.SEQUENCE_OFF)

    # 4. Fluxo que não tem o que disparar: pula.
    #
    # ⚠️ Duas situações, e a segunda é traiçoeira: versão nenhuma publicada, ou
    # versão publicada com grafo SEM NÓ DE ENTRADA. A segunda faz o `start_run`
    # devolver `None` igualzinho a "a caneta está ocupada", e sem esta guarda o
    # disparo ficaria adiando a cada cinco minutos até a validade vencer,
    # anotando "ocupado" o tempo todo. O painel diria a coisa errada sobre por
    # que o paciente não recebeu.
    if step.flow.current_version_id is None or not _tem_no_de_entrada(step.flow):
        _resolver(
            enrollment,
            step,
            previsto,
            status=SequenceDispatchStatus.SKIPPED,
            skip_reason=DispatchSkipReason.FLOW_UNPUBLISHED,
        )
        return "pulado_sem_fluxo"

    # 5. Opt-out: cancela a inscrição inteira, não só este passo.
    if enrollment.sequence.is_marketing and enrollment.contact.marketing_opt_out:
        remover(enrollment, reason=EnrollmentEndReason.OPTED_OUT)
        return "cancelado_opt_out"

    # 6. Validade (RF-SEQ-5.1). O passo de deslocamento NEGATIVO expira na
    #    âncora: confirmação de véspera que não saiu até a hora da consulta
    #    seria atrasada ou absurda (RF-SEQ-5.2).
    limite = previsto + timedelta(hours=step.expire_hours)
    if step.offset_days < 0:
        limite = min(limite, enrollment.anchor_at)
    if agora > limite:
        _resolver(
            enrollment,
            step,
            previsto,
            status=SequenceDispatchStatus.SKIPPED,
            skip_reason=DispatchSkipReason.EXPIRED,
        )
        return "pulado_vencido"

    # 7. As duas regras que só valem para passo PÓS-CONSULTA (RF-SEQ-7.0).
    #
    #    Nenhuma das duas encerra a inscrição: elas pulam UM passo. Faltar não
    #    é o mesmo que cancelar a consulta - quem cancelou não vai ser
    #    atendido, e a inscrição inteira morre (RF-SEQ-3.4); quem faltou já
    #    recebeu o que era de antes e só não deve receber o de depois.
    if _e_pos_consulta(previsto, enrollment):
        if _paciente_faltou(enrollment):
            _resolver(
                enrollment,
                step,
                previsto,
                status=SequenceDispatchStatus.SKIPPED,
                skip_reason=DispatchSkipReason.PATIENT_NO_SHOW,
            )
            return "pulado_paciente_faltou"

        if _tem_consulta_futura(enrollment):
            _resolver(
                enrollment,
                step,
                previsto,
                status=SequenceDispatchStatus.SKIPPED,
                skip_reason=DispatchSkipReason.HAS_APPOINTMENT,
            )
            return "pulado_ja_tem_consulta"

    # 8. e 9. A conversa nasce DEPOIS de tudo isso e dentro da mesma transação
    #    da tomada: disparo adiado não pode deixar conversa vazia na fila da
    #    recepção.
    return _disparar(enrollment, step, previsto)


def _disparar(enrollment, step, previsto) -> str:
    from apps.automation.engine import start_run
    from apps.inbox.services import ConversaSemDestino, conversa_para_disparo

    try:
        with transaction.atomic():
            conversation = conversa_para_disparo(enrollment.clinic, enrollment.contact)

            # 8. Janela de 24h contra o que o fluxo abre (RF-SEQ-5.3).
            if not conversation.window_open and not _abre_fora_da_janela(step.flow):
                raise _AdiarDisparo(HoldReason.NO_WINDOW)

            # 9. A tomada. `None` quer dizer "agora não": a caneta está com um
            #    atendente ou com outro fluxo. Não é erro, e o relógio já foi
            #    empurrado lá em cima.
            run = start_run(step.flow, conversation)
            if run is None:
                raise _AdiarDisparo(HoldReason.BUSY)

            SequenceDispatch.objects.create(
                enrollment=enrollment,
                step=step,
                scheduled_for=previsto,
                resolved_at=timezone.now(),
                status=SequenceDispatchStatus.STARTED,
                flow_run=run,
            )
            _agendar(enrollment, _proximo_passo(enrollment, depois_de=step))
    except ConversaSemDestino:
        _resolver(
            enrollment,
            step,
            previsto,
            status=SequenceDispatchStatus.SKIPPED,
            skip_reason=DispatchSkipReason.NO_CHANNEL,
        )
        return "pulado_sem_canal"
    except _AdiarDisparo as adiar:
        # O rollback leva junto a conversa recém-criada, se foi este disparo
        # que a criou. Tenta de novo na próxima varredura, até a validade.
        #
        # ⚠️ A anotação do "segurando" acontece FORA do bloco que foi desfeito:
        # ela precisa sobreviver ao rollback, senão o painel não teria o que
        # mostrar ao lado da hora que já passou.
        return _segurar(enrollment, adiar.motivo)
    _soltar(enrollment)
    return "disparado"


class _AdiarDisparo(Exception):
    """Desfaz o que este disparo criou e devolve a inscrição para o relógio."""

    def __init__(self, motivo):
        self.motivo = motivo
        super().__init__(motivo)


# --------------------------------------------------------------------- #
# A medição de resultado (RF-SEQ-11.3)
# --------------------------------------------------------------------- #


def resultado_por_passo(sequence, *, dias: int) -> dict:
    """
    Entregues, lidas, respondidas e agendadas, POR PASSO.

    ⚠️ A unidade é o DISPARO, nunca a mensagem. Uma execução manda várias falas
    (o modelo e depois os botões), e contar todas daria dois "entregues" para
    um paciente só. Conta a PRIMEIRA fala da execução, que é a que representa o
    disparo ter chegado - assim os quatro números são comparáveis entre si e
    com o total de disparos que os gerou.

    Devolve `{step_id: {disparos, entregues, lidas, responderam, agendaram}}`.
    Sempre com o denominador junto: "138 entregues" sozinho pode ser lido
    errado, "138 de 144 disparos" não.

    São SEIS consultas, independentemente do tamanho da trilha. Um laço por
    disparo custaria quatro consultas por paciente, e a fila de resgate real
    tem 1.891.

    ⚠️ "Respondeu" tem DUAS fontes, e a segunda existe por causa da campanha
    (18/08): o fluxo de um nó termina no instante do envio, a resposta chega
    minutos depois, e o evento `replied` só nasce em execução ATIVA - o CTR
    de toda campanha leria zero para sempre. Então também conta resposta a
    MENSAGEM RECEBIDA na conversa depois do disparo, dentro da janela de
    conversão e CORTADA no disparo seguinte da mesma conversa, senão uma
    resposta ao passo 2 contaria também para o passo 1.
    """
    from apps.automation.choices import FlowRunEventType
    from apps.automation.models import FlowRunEvent
    from apps.inbox.choices import MessageStatus
    from apps.inbox.models import Message
    from apps.scheduling.models import Appointment

    desde = timezone.now() - timedelta(days=dias)
    disparos = list(
        SequenceDispatch.objects.filter(
            enrollment__sequence=sequence,
            status=SequenceDispatchStatus.STARTED,
            resolved_at__gte=desde,
            flow_run__isnull=False,
        ).values(
            "step_id",
            "flow_run_id",
            "resolved_at",
            "enrollment__patient_id",
            "flow_run__conversation_id",
        )
    )
    if not disparos:
        return {}

    runs = {d["flow_run_id"] for d in disparos}

    # 1. A PRIMEIRA fala de cada execução. Os eventos vêm em ordem, então o
    #    primeiro que aparece por execução é o que representa o disparo.
    primeira_fala: dict[int, int] = {}
    for evento in (
        FlowRunEvent.objects.filter(run_id__in=runs, event_type=FlowRunEventType.SENT)
        .order_by("created_at", "pk")
        .values("run_id", "data")
    ):
        run_id = evento["run_id"]
        message_id = (evento["data"] or {}).get("message_id")
        if message_id and run_id not in primeira_fala:
            primeira_fala[run_id] = message_id

    # 2. O que aconteceu com essas falas. A escala do status é progressiva
    #    (§9.5), então "lida" implica entregue.
    status_da_fala = dict(
        Message.objects.filter(pk__in=primeira_fala.values()).values_list("pk", "status")
    )

    # 3. Quem respondeu. Por EXECUÇÃO: responder três vezes é um paciente que
    #    respondeu, não três.
    responderam = set(
        FlowRunEvent.objects.filter(
            run_id__in=runs, event_type=FlowRunEventType.REPLIED
        ).values_list("run_id", flat=True)
    )

    # 3b. Resposta que chegou DEPOIS de a execução terminar (a da campanha).
    from apps.inbox.choices import SenderKind

    conversas = {d["flow_run__conversation_id"] for d in disparos} - {None}
    recebidas: dict[int, list] = {}
    if conversas:
        for conv_id, quando in Message.objects.filter(
            conversation_id__in=conversas,
            sender_kind=SenderKind.CONTACT,
            created_at__gte=desde,
        ).values_list("conversation_id", "created_at"):
            recebidas.setdefault(conv_id, []).append(quando)

    # O corte de atribuição: cada disparo responde até o disparo SEGUINTE da
    # mesma conversa.
    disparos_da_conversa: dict[int, list] = {}
    for d in disparos:
        if d["flow_run__conversation_id"]:
            disparos_da_conversa.setdefault(
                d["flow_run__conversation_id"], []
            ).append(d["resolved_at"])
    for tempos in disparos_da_conversa.values():
        tempos.sort()

    # 4. As consultas criadas depois, para casar com a janela da sequência.
    pacientes = {d["enrollment__patient_id"] for d in disparos if d["enrollment__patient_id"]}
    marcadas: dict[int, list] = {}
    if pacientes:
        for pk, quando in Appointment.objects.filter(
            patient_id__in=pacientes, created_at__gte=desde
        ).values_list("patient_id", "created_at"):
            marcadas.setdefault(pk, []).append(quando)

    janela = timedelta(days=sequence.conversion_days)
    contas: dict[int, dict] = {}
    for d in disparos:
        conta = contas.setdefault(
            d["step_id"],
            {"disparos": 0, "entregues": 0, "lidas": 0, "responderam": 0, "agendaram": 0},
        )
        conta["disparos"] += 1

        status = status_da_fala.get(primeira_fala.get(d["flow_run_id"], -1), "")
        if status in (MessageStatus.DELIVERED, MessageStatus.READ):
            conta["entregues"] += 1
        if status == MessageStatus.READ:
            conta["lidas"] += 1

        respondeu = d["flow_run_id"] in responderam
        if not respondeu and d["flow_run__conversation_id"]:
            saiu_em = d["resolved_at"]
            tempos = disparos_da_conversa.get(d["flow_run__conversation_id"], ())
            proximos = [t for t in tempos if t > saiu_em]
            limite = min(
                [saiu_em + janela] + proximos
            )
            respondeu = any(
                saiu_em < quando <= limite
                for quando in recebidas.get(d["flow_run__conversation_id"], ())
            )
        if respondeu:
            conta["responderam"] += 1

        saiu_em = d["resolved_at"]
        if saiu_em and any(
            saiu_em <= quando <= saiu_em + janela
            for quando in marcadas.get(d["enrollment__patient_id"], ())
        ):
            conta["agendaram"] += 1

    return contas
