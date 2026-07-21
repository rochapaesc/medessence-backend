class ClinicalContentGateMixin:
    """
    P10 / RF-PRO-4: conteúdo clínico é de médico e gestor; atendente vê apenas
    metadados (tipo, data, profissional).

    Declare em `clinical_content_fields` o que é CONTEÚDO. Para o atendente
    esses campos são removidos da resposta - não vão como `null`, somem: um
    `null` sugeriria que o registro está vazio, e não que existe e é vedado.

    Falha fechada de propósito: request cujo contexto de clínica não foi
    resolvido é tratado como sem permissão. Sem request no contexto (uso
    interno - sync do EHR, shell, serialização em task) não há leitura de
    usuário para gatear, e nada é removido.
    """

    clinical_content_fields: tuple[str, ...] = ()

    def _viewer_is_attendant(self) -> bool:
        request = self.context.get("request")
        if request is None:
            return False

        membership = getattr(request, "active_membership", None)
        if membership is None:
            return True

        from apps.accounts.choices import MembershipRole

        return membership.role == MembershipRole.ATTENDANT

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if self.clinical_content_fields and self._viewer_is_attendant():
            for field in self.clinical_content_fields:
                data.pop(field, None)
        return data
