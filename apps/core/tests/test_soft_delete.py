"""Soft delete do BaseModel - managers, delete e restore."""

from apps.tenants.models import Clinic


def test_delete_e_soft_e_restore_reverte(db):
    clinic = Clinic.objects.create(name="Clínica Efêmera", slug="clinica-efemera")

    clinic.delete()
    assert not Clinic.objects.filter(pk=clinic.pk).exists()
    assert Clinic.all_objects.filter(pk=clinic.pk).exists()

    clinic.refresh_from_db()
    clinic.restore()
    assert Clinic.objects.filter(pk=clinic.pk).exists()


def test_delete_em_massa_tambem_e_soft(db):
    Clinic.objects.create(name="Massa 1", slug="massa-1")
    Clinic.objects.create(name="Massa 2", slug="massa-2")

    Clinic.objects.filter(slug__startswith="massa-").delete()

    assert Clinic.objects.filter(slug__startswith="massa-").count() == 0
    assert Clinic.all_objects.filter(slug__startswith="massa-").count() == 2
