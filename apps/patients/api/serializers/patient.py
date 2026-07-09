from rest_framework.serializers import (
    ModelSerializer,
    PrimaryKeyRelatedField,
    SerializerMethodField,
)

from apps.patients.api.serializers.tag import TagSummarySerializer
from apps.patients.choices import TagOrigin
from apps.patients.models import Patient, PatientTag, Tag


class PatientReadSerializer(ModelSerializer):
    """Linha da listagem (RF-PAC-1) — enxuto, com status calculado e tags."""

    status = SerializerMethodField()
    tags = SerializerMethodField()

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
            "tags",
            "source",
            "sync_status",
        ]

    def get_status(self, obj):
        # Janela da clínica ativa vem do contexto (viewset escopado) —
        # evita uma query de clinic por linha da listagem.
        clinic = self.context.get("clinic")
        if clinic is not None:
            return obj.status_for_window(clinic.active_window_days)
        return obj.status

    def get_tags(self, obj):
        assignments = obj.patient_tags.all()  # prefetch do viewset (só vivos)
        return TagSummarySerializer([a.tag for a in assignments], many=True).data


class PatientDetailSerializer(PatientReadSerializer):
    """Ficha do paciente (RF-PAC-6) — campos completos."""

    class Meta(PatientReadSerializer.Meta):
        fields = [
            *PatientReadSerializer.Meta.fields,
            "birth_date",
            "gender",
            "email",
            "address",
            "profession",
            "comments_html",
            "insurance_name",
            "external_id",
            "created_at",
            "updated_at",
        ]


class PatientWriteSerializer(ModelSerializer):
    """
    Criação/edição local (RF-PAC-3/4). `clinic` é injetado do contexto ativo
    pelo viewset escopado; o write-through para o EHR entra na fase do
    adapter (a mutação passará a enfileirar SyncOperation).

    `tag_ids` substitui o conjunto de tags LOCAIS do paciente — atribuições
    espelhadas do EHR (origin=EHR) não são tocadas por aqui.
    """

    tag_ids = PrimaryKeyRelatedField(
        many=True,
        required=False,
        write_only=True,
        queryset=Tag.objects.all(),
    )

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
        current = {
            assignment.tag_id: assignment
            for assignment in patient.patient_tags.filter(origin=TagOrigin.LOCAL)
        }
        for tag_id, assignment in current.items():
            if tag_id not in wanted_ids:
                assignment.delete()  # soft — a unicidade parcial permite recriar
        for tag in tags:
            if tag.pk not in current:
                PatientTag.objects.create(patient=patient, tag=tag, origin=TagOrigin.LOCAL)
