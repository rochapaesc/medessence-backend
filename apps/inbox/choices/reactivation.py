from django.db.models import TextChoices


class VariableSource(TextChoices):
    """
    De onde sai o valor de cada `{{n}}` do template (RF-REA-2.3).

    O conjunto é FECHADO de propósito: a alternativa seria aceitar um caminho
    de atributo digitado ("patient.insurance_name"), e aí a mensagem que sai
    para 1.891 pessoas passa a depender de texto livre que ninguém valida.
    """

    PATIENT_FIRST_NAME = "patient_first_name", "Primeiro nome do paciente"
    PATIENT_FULL_NAME = "patient_full_name", "Nome completo do paciente"
    PATIENT_CITY = "patient_city", "Cidade do paciente"
    #: O nome que a pessoa usa no WhatsApp.
    #:
    #: ⚠️ Existe porque **17 das 25 conversas da clínica real não têm paciente
    #: vinculado**: ali as fontes de paciente não resolvem nada, e sem esta a
    #: mensagem sairia com um buraco no meio da frase. Vem como a pessoa
    #: escolheu, com emoji e apelido - quem manda decide se serve.
    CONTACT_NAME = "contact_name", "Nome do contato no WhatsApp"
    CLINIC_NAME = "clinic_name", "Nome da clínica"
    #: O que o FLUXO coletou na conversa (`value` guarda a chave da variável).
    #: Só existe no nó de template: nos outros contextos não há execução.
    FLOW_VAR = "flow_var", "O que o fluxo coletou"
    FIXED = "fixed", "Texto fixo"

    @classmethod
    def para_contexto(cls, contexto: str) -> list["VariableSource"]:
        """
        As fontes que fazem sentido em cada lugar (mockup de 12/08/2026).

        Oferecer uma fonte que o contexto não tem é prometer um dado que não
        vai estar lá na hora do envio: no fluxo não há paciente quando alguém
        monta o nó, no Inbox não há mil pessoas, e na campanha não há ninguém
        digitando.
        """
        do_paciente = [cls.PATIENT_FIRST_NAME, cls.PATIENT_FULL_NAME, cls.PATIENT_CITY]
        if contexto == "reactivation":
            return [*do_paciente, cls.CLINIC_NAME, cls.FIXED]
        if contexto == "inbox":
            return [*do_paciente, cls.CONTACT_NAME, cls.CLINIC_NAME, cls.FIXED]
        if contexto == "flow":
            return [
                cls.FLOW_VAR,
                *do_paciente,
                cls.CONTACT_NAME,
                cls.CLINIC_NAME,
                cls.FIXED,
            ]
        return list(cls)
