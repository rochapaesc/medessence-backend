from rest_framework.serializers import CharField, ModelSerializer, ValidationError

from apps.core.api.serializers import ClinicalContentGateMixin
from apps.core.html import sanitize_html
from apps.patients.models import ClinicalEntry, ClinicalOrigin, PrescriptionModel


class ClinicalEntrySerializer(ClinicalContentGateMixin, ModelSerializer):
    """Linha do tempo clínica (leitura) - espelho EHR + registros locais."""

    practitioner_name = CharField(source="practitioner.name", read_only=True, default=None)

    # P10: some para o atendente. `title` entra porque já entrega o teor
    # ("Receita comum - 3 itens") e `document_url` porque é o PDF inteiro.
    clinical_content_fields = (
        "text",
        "title",
        "description",
        "document_url",
        "form_answers",
    )

    class Meta:
        model = ClinicalEntry
        fields = [
            "id",
            "patient",
            "practitioner",
            "practitioner_name",
            "kind",
            "origin",
            "date",
            "text",
            "title",
            "description",
            "document_url",
            "form_answers",
            "creator_name",
            "creator_license",
            "source",
            "external_id",
            "created_at",
        ]


class ClinicalEntryWriteSerializer(ModelSerializer):
    """
    Registro clínico LOCAL (nota/exame/prescrição da clínica standalone ou
    complementar ao EHR). `origin` é sempre LOCAL aqui; espelhos do EHR são
    read-only - o viewset bloqueia mutação.
    """

    class Meta:
        model = ClinicalEntry
        fields = [
            "id",
            "patient",
            "practitioner",
            "kind",
            "date",
            "text",
            "title",
            "description",
        ]

    def validate_text(self, value):
        return sanitize_html(value)

    def validate_description(self, value):
        return sanitize_html(value)

    def validate(self, attrs):
        kind = attrs.get("kind") or (self.instance and self.instance.kind)
        text = attrs.get("text", self.instance.text if self.instance else "")
        title = attrs.get("title", self.instance.title if self.instance else "")
        if kind == "note" and not text:
            raise ValidationError({"text": "A nota precisa de conteúdo."})
        if kind == "exam" and not title:
            raise ValidationError({"title": "Informe o exame solicitado."})
        return attrs

    def create(self, validated_data):
        validated_data["origin"] = ClinicalOrigin.LOCAL
        return super().create(validated_data)


class PrescriptionModelSerializer(ModelSerializer):
    class Meta:
        model = PrescriptionModel
        fields = [
            "id",
            "name",
            "content",
            "hint",
            "medications",
            "smart",
            "special_prescription",
            "origin",
            "external_id",
        ]
        read_only_fields = ["origin", "external_id"]

    def validate_content(self, value):
        return sanitize_html(value)
