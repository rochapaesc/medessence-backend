"""
Os templates da CONTA que a clínica usa hoje (RF-INB-3.3).

Na Cloud API o template pertence à conta da Meta (a WABA), não ao número. A
clínica que troca de número ou de app passa a usar outra conta, e o catálogo
antigo ficava aqui, misturado com o novo, sem como distinguir um do outro.

Este módulo é o lugar ÚNICO que responde "de qual conta estamos falando", e
existe porque são SEIS os leitores de template: envio pelo Inbox, validação do
envio, montagem dos parâmetros, validador de fluxo, motor de fluxo e fila de
reativação. Cada um com o seu filtro é como um deles fica para trás e volta a
pescar a linha da conta antiga quando há nomes repetidos.
"""

from apps.inbox.models import Channel, WhatsAppTemplate


def conta_da_clinica(clinic_id) -> str:
    """
    O identificador da conta da Meta que esta clínica usa AGORA.

    ⚠️ Nunca o canal de teste (RF-FLW-25.5): ele é o do simulador de fluxo, e
    o envio de verdade jamais sai por ele.

    Devolve string vazia quando a clínica não tem canal ou o canal ainda não
    tem conta configurada. Vazio é um escopo legítimo: é onde vivem os
    templates das clínicas de mentira, que não têm WABA nenhuma.
    """
    channel = (
        Channel.objects.filter(clinic_id=clinic_id, is_test=False)
        .only("waba_id")
        .first()
    )
    return channel.waba_id if channel else ""


def templates_da_clinica(clinic_id):
    """O catálogo que a clínica pode usar hoje. Base de TODA leitura."""
    return WhatsAppTemplate.objects.filter(
        clinic_id=clinic_id, waba_id=conta_da_clinica(clinic_id)
    )


def template_por_nome(clinic_id, nome, idioma=None):
    """
    Um template pelo nome, dentro da conta atual.

    ⚠️ É por aqui que os leitores devem passar. `filter(clinic, name).first()`
    virou perigoso quando a conta entrou na unicidade: o mesmo nome pode existir
    em duas contas, e o `.first()` escolheria por ordem de inserção - montando
    os parâmetros com os componentes da conta errada, que a Meta recusa.
    """
    qs = templates_da_clinica(clinic_id).filter(name=nome)
    if idioma:
        qs = qs.filter(language=idioma)
    return qs.first()
