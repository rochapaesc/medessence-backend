"""
`POST /wa-templates/` — criar template e mandar para a revisão da Meta.

⚠️ O que estes testes protegem: o template criado aqui vai para a conta da
clínica na Meta e passa por revisão HUMANA. Uma chamada errada não é um 400
que se conserta em seguida: é um template recusado, com o nome já ocupado na
conta, e uma espera de horas para descobrir.
"""

import pytest

from apps.inbox.models import WhatsAppTemplate

URL = "/api/v1/wa-templates/"


def _corpo(**mudancas) -> dict:
    base = {
        "name": "retorno_paciente",
        "category": "MARKETING",
        "language": "pt_BR",
        "body": "Olá, {{1}}! Sentimos sua falta em {{2}}.",
        "examples": {"body": ["Ivanita", "Oeiras"]},
    }
    base.update(mudancas)
    return base


@pytest.mark.django_db
def test_criar_manda_para_a_meta_e_guarda_o_id(
    api_client, manager_single_clinic, inbox_a
):
    """
    ⚠️ O `meta_template_id` é o que permitirá editar e apagar ESTA variante de
    idioma sozinha: a Meta apaga pelo nome, e sem ele remove todas as línguas
    de uma vez.
    """
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(URL, _corpo(), format="json")

    assert resposta.status_code == 201
    template = WhatsAppTemplate.objects.get(name="retorno_paciente")
    assert template.meta_template_id
    # PENDING, e não APPROVED: a revisão é humana e leva de minutos a horas.
    assert template.status == "PENDING"
    assert template.clinic_id == inbox_a["conversation"].clinic_id


@pytest.mark.django_db
def test_o_que_foi_para_a_meta_e_o_payload_montado(
    api_client, manager_single_clinic, inbox_a, monkeypatch
):
    enviados = []

    class _Espiao:
        def create_template(self, payload):
            from apps.integrations.whatsapp.base import TemplateCriado

            enviados.append(payload)
            return TemplateCriado(id="tpl-1", status="PENDING")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Espiao(),
    )
    api_client.force_authenticate(manager_single_clinic)
    api_client.post(URL, _corpo(footer="MedEssence"), format="json")

    payload = enviados[0]
    assert [c["type"] for c in payload["components"]] == ["BODY", "FOOTER"]
    # Lista DE LISTAS no corpo: trocar pela lista simples é recusa na hora.
    corpo = payload["components"][0]
    assert corpo["example"] == {"body_text": [["Ivanita", "Oeiras"]]}


@pytest.mark.django_db
def test_erro_de_regra_da_meta_vem_ANTES_da_chamada(
    api_client, manager_single_clinic, inbox_a, monkeypatch
):
    """A alternativa é gastar a chamada e receber um 400 opaco horas depois."""
    chamou = []
    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: chamou.append(1),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(URL, _corpo(footer="Equipe {{1}}"), format="json")

    assert resposta.status_code == 400
    assert "rodapé não aceita variável" in str(resposta.data)
    assert chamou == [], "não pode ter falado com a Meta"


@pytest.mark.django_db
def test_template_recusado_pela_meta_NAO_some(
    api_client, manager_single_clinic, inbox_a, monkeypatch
):
    """
    ⚠️ Ele fica como rascunho local com o motivo (RF-INB-3.2.5). Sumir faria a
    clínica reescrever do zero um texto que ela acabou de digitar, sem saber o
    que estava errado.
    """
    from apps.integrations.whatsapp.exceptions import WhatsAppError

    class _Recusa:
        def create_template(self, payload):
            raise WhatsAppError("Conteúdo promocional não permitido nesta categoria.")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Recusa(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(URL, _corpo(), format="json")

    assert resposta.status_code == 201
    template = WhatsAppTemplate.objects.get(name="retorno_paciente")
    assert template.status == "REJECTED"
    assert "promocional" in template.rejection_reason
    assert template.components, "o texto digitado tem que continuar lá"


@pytest.mark.django_db
def test_nome_repetido_no_mesmo_idioma_e_recusado(
    api_client, manager_single_clinic, inbox_a, clinic_a
):
    WhatsAppTemplate.objects.create(
        clinic=clinic_a, name="retorno_paciente", language="pt_BR", status="APPROVED"
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(URL, _corpo(), format="json")

    assert resposta.status_code == 400
    assert "Já existe" in str(resposta.data)


@pytest.mark.django_db
def test_atendente_nao_cria_template(api_client, attendant_a, inbox_a):
    """
    O que se cria aqui vai para a conta da clínica na Meta e fica no nome
    dela. Ler é da recepção; criar é do gestor.
    """
    api_client.force_authenticate(attendant_a)
    resposta = api_client.post(URL, _corpo(), format="json")
    assert resposta.status_code == 403


@pytest.mark.django_db
def test_atendente_continua_LENDO_os_templates(api_client, attendant_a, inbox_a, clinic_a):
    WhatsAppTemplate.objects.create(
        clinic=clinic_a, name="confirmacao", language="pt_BR", status="APPROVED"
    )
    api_client.force_authenticate(attendant_a)
    resposta = api_client.get(URL)
    assert resposta.status_code == 200


@pytest.mark.django_db
def test_template_de_outra_clinica_nao_aparece(
    api_client, manager_single_clinic, inbox_a, clinic_b
):
    WhatsAppTemplate.objects.create(
        clinic=clinic_b, name="de_outra", language="pt_BR", status="APPROVED"
    )
    api_client.force_authenticate(manager_single_clinic)
    nomes = [t["name"] for t in api_client.get(URL).data["results"]]
    assert "de_outra" not in nomes


# --------------------------- editar e apagar --------------------------- #


@pytest.fixture
def na_meta(clinic_a):
    """Um template que JÁ existe na conta da Meta, como os criados por aqui."""
    return WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="retorno_paciente",
        language="pt_BR",
        category="MARKETING",
        status="APPROVED",
        meta_template_id="tpl-existente",
        components=[{"type": "BODY", "text": "Olá, {{1}}!"}],
    )


def _url(template) -> str:
    return f"{URL}{template.pk}/"


@pytest.mark.django_db
def test_editar_reescreve_na_meta_e_volta_para_revisao(
    api_client, manager_single_clinic, inbox_a, na_meta, monkeypatch
):
    """
    ⚠️ A Meta SUBSTITUI os componentes inteiros e devolve o template à fila de
    revisão. Deixar como APPROVED aqui faria a recepção mandar, achando que o
    texto novo já vale.
    """
    editados = []

    class _Espiao:
        def update_template(self, meta_template_id, payload):
            editados.append((meta_template_id, payload))

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Espiao(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        _url(na_meta),
        _corpo(body="Olá, {{1}}! Temos horário nesta semana.", examples={"body": ["Ivanita"]}),
        format="json",
    )

    assert resposta.status_code == 200
    na_meta.refresh_from_db()
    assert na_meta.status == "PENDING"
    assert "nesta semana" in na_meta.components[0]["text"]
    # O id da variante vai junto: é por ele que a Meta acha o template.
    assert editados[0][0] == "tpl-existente"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status,esperado",
    [
        ("PENDING", "em revisão"),
        # ⚠️ Estes dois caíam no `else` e a Meta os escrevia CRUS na tela, em
        # inglês, para quem só queria corrigir um texto.
        ("IN_APPEAL", "em recurso"),
        ("PENDING_DELETION", "sendo apagado"),
    ],
)
def test_template_que_a_meta_ainda_resolve_nao_se_edita(
    api_client, manager_single_clinic, inbox_a, na_meta, status, esperado
):
    na_meta.status = status
    na_meta.save(update_fields=["status"])
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(_url(na_meta), _corpo(examples={"body": ["a", "b"]}), format="json")

    assert resposta.status_code == 400
    frase = str(resposta.data)
    assert esperado in frase
    assert status not in frase, "o status cru da Meta não vai para a tela"


@pytest.mark.django_db
def test_sem_o_id_guardado_ele_e_DESCOBERTO_pelo_nome(
    api_client, manager_single_clinic, inbox_a, clinic_a, monkeypatch
):
    """
    ⚠️ Todo template anterior a 13/08/2026 está sem `meta_template_id`: a
    sincronização recebia o id e o descartava. Recusar a edição por causa
    disso travava a clínica justamente nos templates que ela JÁ tem — que são
    todos, até ela criar o primeiro por aqui.
    """
    from apps.integrations.whatsapp.base import Template

    sem_id = WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="ja_existia",
        language="pt_BR",
        category="MARKETING",
        status="APPROVED",
    )
    editados = []

    class _Espiao:
        def list_templates(self):
            return [
                # Mesmo nome em OUTRO idioma vem antes de propósito: casar só
                # pelo nome editaria a variante errada.
                Template(name="ja_existia", language="en_US", meta_id="ERRADO"),
                Template(name="ja_existia", language="pt_BR", meta_id="CERTO"),
            ]

        def update_template(self, meta_template_id, payload):
            editados.append(meta_template_id)

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Espiao(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        _url(sem_id),
        _corpo(name="ja_existia", body="Olá, {{1}}!", examples={"body": ["Ana"]}),
        format="json",
    )

    assert resposta.status_code == 200
    assert editados == ["CERTO"]
    sem_id.refresh_from_db()
    # E o id fica guardado, para a próxima edição não precisar procurar.
    assert sem_id.meta_template_id == "CERTO"


@pytest.mark.django_db
def test_recusado_na_CRIACAO_e_reenviado_como_criacao(
    api_client, manager_single_clinic, inbox_a, clinic_a, monkeypatch
):
    """
    ⚠️ O beco sem saída de 13/08/2026: a Meta recusou o template na criação,
    ele ficou salvo como REJECTED e SEM id, a clínica corrigiu o texto, clicou
    em "editar e reenviar" — e o sistema respondeu que o template não existe
    na Meta. Nunca existiu MESMO: reenviar aqui é CRIAR, não editar. O nome
    continua livre na conta, justamente porque a criação falhou.
    """
    from apps.integrations.whatsapp.base import TemplateCriado

    recusado = WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="teste",
        language="pt_BR",
        category="MARKETING",
        status="REJECTED",
        rejection_reason="O formato do corpo da mensagem está incorreto.",
        components=[{"type": "BODY", "text": "{{1}}"}],
    )
    criados = []

    class _Aceita:
        def list_templates(self):
            return []

        def create_template(self, payload):
            criados.append(payload)
            return TemplateCriado(id="tpl-novo", status="PENDING")

        def update_template(self, meta_template_id, payload):
            raise AssertionError("não devia EDITAR o que não existe lá")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Aceita(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        _url(recusado),
        _corpo(name="teste", body="Olá, {{1}}!", examples={"body": ["Ana"]}),
        format="json",
    )

    assert resposta.status_code == 200
    recusado.refresh_from_db()
    assert recusado.status == "PENDING"
    assert recusado.meta_template_id == "tpl-novo"
    # O motivo antigo deixa de valer: ele era do texto que acabou de mudar.
    assert recusado.rejection_reason == ""
    assert criados[0]["components"][0]["text"] == "Olá, {{1}}!"


@pytest.mark.django_db
def test_recusado_DE_NOVO_guarda_o_motivo_novo(
    api_client, manager_single_clinic, inbox_a, clinic_a, monkeypatch
):
    """Mesma regra do create: o texto fica salvo para corrigir mais uma vez."""
    from apps.integrations.whatsapp.exceptions import WhatsAppError

    recusado = WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="teste",
        language="pt_BR",
        category="MARKETING",
        status="REJECTED",
        rejection_reason="motivo velho",
    )

    class _RecusaDeNovo:
        def list_templates(self):
            return []

        def create_template(self, payload):
            raise WhatsAppError("O nome do template já está em uso.")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _RecusaDeNovo(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        _url(recusado),
        _corpo(name="teste", body="Olá, {{1}}!", examples={"body": ["Ana"]}),
        format="json",
    )

    assert resposta.status_code == 200
    recusado.refresh_from_db()
    assert recusado.status == "REJECTED"
    assert recusado.rejection_reason == "O nome do template já está em uso."
    assert recusado.components, "o texto corrigido continua salvo"


@pytest.mark.django_db
def test_meta_FORA_DO_AR_nao_cria_por_engano(
    api_client, manager_single_clinic, inbox_a, clinic_a, monkeypatch
):
    """
    ⚠️ Sem conseguir listar, não dá para saber se o template existe lá. Criar
    no escuro duplicaria o nome quando a Meta voltasse - e nome duplicado ela
    recusa, deixando a clínica presa.
    """
    from apps.integrations.whatsapp.exceptions import WhatsAppError

    sem_id = WhatsAppTemplate.objects.create(
        clinic=clinic_a, name="teste", language="pt_BR", status="APPROVED"
    )

    class _Fora:
        def list_templates(self):
            raise WhatsAppError("Canal do WhatsApp desconectado.")

        def create_template(self, payload):
            raise AssertionError("não devia criar sem saber se já existe")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Fora(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        _url(sem_id),
        _corpo(name="teste", body="Olá, {{1}}!", examples={"body": ["Ana"]}),
        format="json",
    )

    assert resposta.status_code == 400
    assert "desconectado" in str(resposta.data)


@pytest.mark.django_db
def test_nome_e_idioma_nao_mudam(api_client, manager_single_clinic, inbox_a, na_meta):
    """A Meta não deixa renomear nem trocar o idioma de um template existente."""
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        _url(na_meta), _corpo(name="outro_nome", examples={"body": ["a", "b"]}), format="json"
    )

    assert resposta.status_code == 400
    assert "nome de um template não pode mudar" in str(resposta.data)


@pytest.mark.django_db
def test_meta_recusando_a_edicao_MANTEM_o_que_esta_no_ar(
    api_client, manager_single_clinic, inbox_a, na_meta, monkeypatch
):
    """
    ⚠️ Guardar a versão nova aqui faria a tela mostrar um texto que o paciente
    não vai receber: na Meta continua valendo o de antes.
    """
    from apps.integrations.whatsapp.exceptions import WhatsAppError

    class _Recusa:
        def update_template(self, meta_template_id, payload):
            raise WhatsAppError("Template em uso por uma campanha ativa.")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Recusa(),
    )
    antes = na_meta.components
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        _url(na_meta), _corpo(body="Texto novo {{1}}", examples={"body": ["a"]}), format="json"
    )

    assert resposta.status_code == 400
    na_meta.refresh_from_db()
    assert na_meta.components == antes
    assert na_meta.status == "APPROVED"
    assert "campanha ativa" in na_meta.rejection_reason


@pytest.mark.django_db
def test_apagar_manda_o_ID_junto_para_nao_levar_os_outros_idiomas(
    api_client, manager_single_clinic, inbox_a, na_meta, monkeypatch
):
    """
    ⚠️ Sem o `meta_template_id`, a Meta apaga TODAS as variantes de idioma com
    aquele nome — inclusive as que ninguém pediu para apagar.
    """
    apagados = []

    class _Espiao:
        def delete_template(self, name, meta_template_id=""):
            apagados.append((name, meta_template_id))

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Espiao(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.delete(_url(na_meta))

    assert resposta.status_code == 204
    assert apagados == [("retorno_paciente", "tpl-existente")]
    assert not WhatsAppTemplate.objects.filter(pk=na_meta.pk).exists()


@pytest.mark.django_db
def test_meta_recusando_a_exclusao_NAO_apaga_aqui(
    api_client, manager_single_clinic, inbox_a, na_meta, monkeypatch
):
    """
    ⚠️ Apagar o nosso e falhar lá deixaria um template órfão na conta da
    clínica: nome ocupado, e nada por aqui apontando para ele.
    """
    from apps.integrations.whatsapp.exceptions import WhatsAppError

    class _Recusa:
        def delete_template(self, name, meta_template_id=""):
            raise WhatsAppError("Template em uso.")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Recusa(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.delete(_url(na_meta))

    assert resposta.status_code == 400
    assert WhatsAppTemplate.objects.filter(pk=na_meta.pk).exists()


@pytest.mark.django_db
def test_apagar_o_que_so_existe_aqui_nao_chama_a_meta(
    api_client, manager_single_clinic, inbox_a, clinic_a, monkeypatch
):
    local = WhatsAppTemplate.objects.create(
        clinic=clinic_a, name="rascunho", language="pt_BR", status="REJECTED"
    )
    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: pytest.fail("não devia falar com a Meta"),
    )
    api_client.force_authenticate(manager_single_clinic)

    assert api_client.delete(_url(local)).status_code == 204


@pytest.mark.django_db
def test_atendente_nao_edita_nem_apaga(api_client, attendant_a, inbox_a, na_meta):
    api_client.force_authenticate(attendant_a)
    assert api_client.delete(_url(na_meta)).status_code == 403
    assert (
        api_client.put(_url(na_meta), _corpo(examples={"body": ["a", "b"]}), format="json").status_code
        == 403
    )


@pytest.mark.django_db
def test_sincronizacao_GUARDA_o_id_da_meta(clinic_a, inbox_a, monkeypatch):
    """
    ⚠️ O id vem no `get_templates` e era descartado. Sem ele, o template
    sincronizado não pode ser editado nem apagado pela tela - o que deixava a
    clínica sem mexer justamente nos templates que ela JÁ tem, que são todos
    os que existem antes de ela criar o primeiro por aqui.
    """
    from apps.integrations.whatsapp.base import Template
    from apps.inbox.tasks import refresh_channel_templates

    class _Provedor:
        def list_templates(self):
            return [
                Template(
                    name="ja_existia",
                    language="pt_BR",
                    category="UTILITY",
                    status="APPROVED",
                    components=[{"type": "BODY", "text": "Oi"}],
                    meta_id="1234567890",
                )
            ]

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Provedor(),
    )
    refresh_channel_templates(inbox_a["channel"].pk)

    template = WhatsAppTemplate.objects.get(clinic=clinic_a, name="ja_existia")
    assert template.meta_template_id == "1234567890"


@pytest.mark.django_db
def test_sincronizacao_NORMALIZA_o_status(clinic_a, inbox_a, monkeypatch):
    """
    ⚠️ A Meta devolve `PENDING_REVIEW` onde a documentação dela diz `PENDING`,
    e a sincronização era o ÚNICO caminho que gravava o status cru - o webhook
    e a criação já normalizavam. Cru, o termo em inglês ia parar na tela, o
    template ficava fora de "em revisão" e o botão de editar era decidido por
    acaso.

    O que ela inventar de novo vira `PENDING`: a linha continua visível, em
    vez de sumir da lista de quem acabou de criar.
    """
    from apps.integrations.whatsapp.base import Template
    from apps.inbox.tasks import refresh_channel_templates

    class _Provedor:
        def list_templates(self):
            return [
                Template(
                    name=nome,
                    language="pt_BR",
                    category="UTILITY",
                    status=status,
                    components=[{"type": "BODY", "text": "Oi"}],
                    meta_id=f"id-{nome}",
                )
                for nome, status in [
                    ("em_revisao", "PENDING_REVIEW"),
                    ("coisa_nova", "ALGO_QUE_A_META_INVENTOU"),
                    ("aprovado", "approved"),
                ]
            ]

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Provedor(),
    )
    refresh_channel_templates(inbox_a["channel"].pk)

    guardado = {
        t.name: t.status
        for t in WhatsAppTemplate.objects.filter(clinic=clinic_a)
    }
    assert guardado["em_revisao"] == "PENDING"
    assert guardado["coisa_nova"] == "PENDING"
    assert guardado["aprovado"] == "APPROVED"


# ------------------------- atualizar agora ------------------------- #


@pytest.mark.django_db
def test_sincronizar_traz_o_estado_atual_da_meta(
    api_client, manager_single_clinic, inbox_a, clinic_a, monkeypatch
):
    """
    ⚠️ A aprovação é revisão HUMANA e o veredito não chega por evento: o beat
    passa de 6 em 6 horas, então um template aprovado em dois minutos ficava
    "em revisão" na tela por horas — e recarregar a página não adiantava,
    porque ela lê o nosso banco e não a Meta.
    """
    from apps.integrations.whatsapp.base import Template

    esperando = WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="teste_01",
        language="pt_BR",
        category="UTILITY",
        status="PENDING",
        meta_template_id="tpl-1",
    )

    class _JaAprovou:
        def list_templates(self):
            return [
                Template(
                    name="teste_01",
                    language="pt_BR",
                    category="UTILITY",
                    status="APPROVED",
                    components=[{"type": "BODY", "text": "Olá, {{1}}!"}],
                    meta_id="tpl-1",
                )
            ]

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _JaAprovou(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(f"{URL}sincronizar/")

    assert resposta.status_code == 200
    esperando.refresh_from_db()
    assert esperando.status == "APPROVED"


@pytest.mark.django_db
def test_sincronizar_com_a_meta_fora_DIZ_o_motivo(
    api_client, manager_single_clinic, inbox_a, monkeypatch
):
    """
    ⚠️ Diferente do beat, que engole e tenta de novo: aqui alguém está olhando
    a tela. Engolir mostraria a mesma lista e a pessoa concluiria que a Meta
    ainda não respondeu.
    """
    from apps.integrations.whatsapp.exceptions import WhatsAppError

    class _Fora:
        def list_templates(self):
            raise WhatsAppError("Canal do WhatsApp desconectado.")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Fora(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(f"{URL}sincronizar/")

    assert resposta.status_code == 400
    assert "desconectado" in str(resposta.data)


@pytest.mark.django_db
def test_atendente_pode_atualizar_a_lista(api_client, attendant_a, inbox_a, monkeypatch):
    """Ler o estado atual não muda nada na conta da Meta: é leitura."""
    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: type("P", (), {"list_templates": lambda self: []})(),
    )
    api_client.force_authenticate(attendant_a)
    assert api_client.post(f"{URL}sincronizar/").status_code == 200
