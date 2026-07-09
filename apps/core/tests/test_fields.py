"""EncryptedJSONField — round-trip e cifra em repouso."""

from django.db import connection

from apps.tenants.models import Clinic


def test_round_trip_de_credenciais(db):
    clinic = Clinic.objects.create(
        name="Clínica Cripto",
        slug="clinica-cripto",
        ehr_credentials={"api_key": "super-secreta-123", "extra": {"n": 1}},
    )
    clinic.refresh_from_db()
    assert clinic.ehr_credentials == {"api_key": "super-secreta-123", "extra": {"n": 1}}


def test_valor_no_banco_nao_e_texto_plano(db):
    Clinic.objects.create(
        name="Clínica Cripto 2",
        slug="clinica-cripto-2",
        ehr_credentials={"api_key": "nao-pode-vazar"},
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT ehr_credentials FROM tenants_clinic WHERE slug = %s",
            ["clinica-cripto-2"],
        )
        raw = cursor.fetchone()[0]
    assert "nao-pode-vazar" not in raw
    assert "api_key" not in raw


def test_default_e_dict_vazio(db):
    clinic = Clinic.objects.create(name="Clínica Sem EHR", slug="clinica-sem-ehr")
    clinic.refresh_from_db()
    assert clinic.ehr_credentials == {}
