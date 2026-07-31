"""
O que o papel `partner` alcança de paciente (RF-PAR-6).

A cerca de PERMISSÃO (`partner_allowed`) diz quais views ele abre; esta diz
sobre QUEM. São coisas diferentes, e faltar a segunda é vazamento: com só a
primeira, um usuário externo abria a ficha de qualquer paciente da clínica
trocando o id na URL, e baixava o prontuário inteiro numa listagem paginada.

A régua sai da própria tela: Parceiros mostra quem teve **atendimento
realizado**. Quem nunca foi atendido não aparece lá e não deve responder aqui.
"""

from apps.accounts.choices import MembershipRole
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import Appointment


def eh_parceiro(membership) -> bool:
    return bool(membership) and membership.role == MembershipRole.PARTNER


def pacientes_do_parceiro(clinic):
    """Ids dos pacientes com atendimento REALIZADO na clínica."""
    return Appointment.objects.filter(
        clinic=clinic,
        status=AppointmentStatus.COMPLETED,
        patient__isnull=False,
    ).values_list("patient_id", flat=True)
