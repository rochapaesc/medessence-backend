def viewer_is_attendant(context: dict) -> bool:
    """
    Quem está lendo é atendente? Decide o gate de conteúdo clínico (P10) e o
    mascaramento de dado pessoal (§15).

    Falha fechada de propósito: request cujo contexto de clínica não foi
    resolvido conta como atendente. Sem request no contexto (uso interno - sync
    do EHR, shell, serialização em task) não há leitura de usuário para gatear.
    """
    request = context.get("request")
    if request is None:
        return False

    membership = getattr(request, "active_membership", None)
    if membership is None:
        return True

    from apps.accounts.choices import MembershipRole

    return membership.role == MembershipRole.ATTENDANT


class ClinicalContentGateMixin:
    """
    P10 / RF-PRO-4: conteúdo clínico é de médico e gestor; atendente vê apenas
    metadados (tipo, data, profissional).

    Declare em `clinical_content_fields` o que é CONTEÚDO. Para o atendente
    esses campos são removidos da resposta - não vão como `null`, somem: um
    `null` sugeriria que o registro está vazio, e não que existe e é vedado.

    Vale para leitura E para a resposta de create/update: sem isso, bastava ao
    atendente salvar qualquer campo para receber o conteúdo de volta no corpo
    da resposta (era o caso até 21/07/2026).
    """

    clinical_content_fields: tuple[str, ...] = ()

    def _viewer_is_attendant(self) -> bool:
        return viewer_is_attendant(self.context)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if self.clinical_content_fields and self._viewer_is_attendant():
            for field in self.clinical_content_fields:
                data.pop(field, None)
        return data
