from rest_framework.serializers import (
    ModelSerializer,
    PrimaryKeyRelatedField,
    SerializerMethodField,
    ValidationError,
)

from apps.core.api.serializers import ClinicalContentGateMixin, viewer_is_attendant
from apps.core.masking import is_masked, mask_cpf
from apps.patients.api.serializers.tag import TagSummarySerializer
from apps.patients.choices import TagOrigin
from apps.patients.models import Patient, PatientTag, Tag
from apps.patients.phone import canonizar_telefone


class PatientReadSerializer(ModelSerializer):
    """Linha da listagem (RF-PAC-1) - enxuto, com status calculado e tags."""

    status = SerializerMethodField()
    tags = SerializerMethodField()
    # Na LISTAGEM o CPF sai mascarado para todo mundo: a tela não mostra o
    # documento, e devolver 20 CPFs por página seria expor em massa o que só se
    # justifica um a um. O valor completo é da ficha (§15) - ver
    # `PatientDetailSerializer`, onde o acesso ainda é auditado.
    cpf = SerializerMethodField()

    def get_cpf(self, obj):
        return mask_cpf(obj.cpf)

    last_practitioner = SerializerMethodField()

    def get_last_practitioner(self, obj):
        """
        Com quem foi a última consulta (RF-REA-1, a linha da fila de resgate).

        Sai da anotação que o segmento de resgate acrescenta, e vem VAZIO nas
        outras listagens - elas não pagam a subquery. `null` aqui significa
        "não foi pedido", não "não tem profissional".
        """
        return getattr(obj, "last_practitioner_name", None)

    class Meta:
        model = Patient
        fields = [
            "id",
            "name",
            "cpf",
            "phone",
            "city",
            "state",
            "status",
            "last_appointment_at",
            "last_practitioner",
            "tags",
            "source",
            "sync_status",
        ]

    def get_status(self, obj):
        # Janela da clínica ativa vem do contexto (viewset escopado) -
        # evita uma query de clinic por linha da listagem.
        clinic = self.context.get("clinic")
        if clinic is not None:
            return obj.status_for_window(clinic.active_window_days)
        return obj.status

    def get_tags(self, obj):
        assignments = obj.patient_tags.all()  # prefetch do viewset (só vivos)
        return TagSummarySerializer([a.tag for a in assignments], many=True).data


class PatientDetailSerializer(ClinicalContentGateMixin, PatientReadSerializer):
    """Ficha do paciente (RF-PAC-6) - campos completos."""

    # P10: `comments_html` é texto livre de quem atende - conteúdo clínico
    # fora da linha do tempo. O atendente segue podendo ESCREVER (o write
    # serializer aplica o mesmo gate na resposta), mas não lê de volta.
    clinical_content_fields = ("comments_html",)

    def get_cpf(self, obj):
        """
        Aqui, sim, o documento inteiro - para médico e gestor (§15). É o único
        lugar que o revela, e o viewset audita cada acesso (`READ_CPF`).
        """
        if viewer_is_attendant(self.context):
            return mask_cpf(obj.cpf)
        return obj.cpf

    class Meta(PatientReadSerializer.Meta):
        fields = [
            *PatientReadSerializer.Meta.fields,
            "birth_date",
            "gender",
            "email",
            "address",
            "profession",
            "blood_type",
            "weight_kg",
            "height_cm",
            "guardians",
            "emergency_contacts",
            "birth_info",
            "comments_html",
            "insurance_name",
            "external_id",
            "created_at",
            "updated_at",
        ]


class PatientWriteSerializer(ClinicalContentGateMixin, ModelSerializer):
    """
    Criação/edição local (RF-PAC-3/4). `clinic` é injetado do contexto ativo
    pelo viewset escopado; a mutação enfileira `SyncOperation` quando a clínica
    tem EHR com escrita ligada (§10.2).

    `tag_ids` substitui o conjunto de tags LOCAIS do paciente - atribuições
    espelhadas do EHR (origin=EHR) não são tocadas por aqui.

    O atendente CADASTRA e EDITA, mas não lê de volta o que não pode ver: o
    gate de conteúdo clínico vale também para a resposta da escrita (antes,
    salvar qualquer campo devolvia `comments_html` inteiro).
    """

    clinical_content_fields = ("comments_html",)

    tag_ids = PrimaryKeyRelatedField(
        many=True,
        required=False,
        write_only=True,
        queryset=Tag.objects.all(),
    )

    def validate_phone(self, value):
        # Regra do nono dígito (§6.2): guarda a forma canônica — dígitos com
        # DDI, celular BR com o 9 — para o número casar com o wa_id da Meta.
        return canonizar_telefone(value)

    def validate_cpf(self, value):
        """
        Recusa a MÁSCARA como valor: a tela mostra `123.***.***-00` para quem
        não pode ver o documento, e reenviar isso no salvar apagaria o CPF real
        (e o empurraria truncado ao EHR). Mascarado = "não mudou".
        """
        if is_masked(value):
            if self.instance is None:
                raise ValidationError(
                    "CPF inválido: o valor recebido está mascarado."
                )
            return self.instance.cpf
        return value

    def to_representation(self, instance):
        # A resposta do create/update segue a mesma régua da leitura: quem não
        # pode ver o documento recebe mascarado.
        data = super().to_representation(instance)
        if "cpf" in data and viewer_is_attendant(self.context):
            data["cpf"] = mask_cpf(data["cpf"])
        return data

    class Meta:
        model = Patient
        fields = [
            "id",
            "name",
            "cpf",
            "birth_date",
            "gender",
            "email",
            "phone",
            "city",
            "state",
            "address",
            "profession",
            "blood_type",
            "weight_kg",
            "height_cm",
            "guardians",
            "emergency_contacts",
            "birth_info",
            "comments_html",
            "insurance_name",
            "tag_ids",
        ]

    def create(self, validated_data):
        tags = validated_data.pop("tag_ids", None)
        patient = super().create(validated_data)
        if tags is not None:
            self._sync_local_tags(patient, tags)
        return patient

    def update(self, instance, validated_data):
        tags = validated_data.pop("tag_ids", None)
        patient = super().update(instance, validated_data)
        if tags is not None:
            self._sync_local_tags(patient, tags)
        return patient

    def _sync_local_tags(self, patient, tags):
        wanted_ids = {tag.pk for tag in tags}
        # Todas as atribuições VIVAS (a unicidade `uniq_patient_tag` é por
        # (patient, tag) SEM olhar origem): usamos isso para não colidir com um
        # espelho do EHR ao (re)criar uma LOCAL do mesmo par.
        existing = {a.tag_id: a for a in patient.patient_tags.all()}
        # Remove só as LOCAIS que saíram do conjunto desejado (EHR não é tocada).
        for tag_id, assignment in existing.items():
            if assignment.origin == TagOrigin.LOCAL and tag_id not in wanted_ids:
                assignment.delete()  # soft - a unicidade parcial permite recriar
        # Cria só as que não têm NENHUMA atribuição viva (evita a IntegrityError
        # quando a tag já veio espelhada do EHR).
        for tag in tags:
            if tag.pk not in existing:
                PatientTag.objects.create(patient=patient, tag=tag, origin=TagOrigin.LOCAL)
