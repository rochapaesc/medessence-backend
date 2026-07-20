from apps.patients.api.viewsets.clinical import (
    ClinicalEntryViewSet,
    PrescriptionModelViewSet,
)
from apps.patients.api.viewsets.patient import PatientViewSet
from apps.patients.api.viewsets.tag import TagViewSet

__all__ = [
    "ClinicalEntryViewSet",
    "PatientViewSet",
    "PrescriptionModelViewSet",
    "TagViewSet",
]
