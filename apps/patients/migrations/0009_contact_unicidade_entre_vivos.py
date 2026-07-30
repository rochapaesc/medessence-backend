from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """
    `uniq_contact_wa_id` era a ÚNICA constraint do projeto sem a condição de
    soft delete: um contato apagado segurava o wa_id para sempre e recriar o
    mesmo número estourava IntegrityError. Passa a valer só entre vivos, como
    PatientContact/Conversation/Channel.
    """

    dependencies = [
        ("patients", "0008_contactnote"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="contact",
            name="uniq_contact_wa_id",
        ),
        migrations.AddConstraint(
            model_name="contact",
            constraint=models.UniqueConstraint(
                fields=["clinic", "wa_id"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_contact_wa_id",
            ),
        ),
    ]
