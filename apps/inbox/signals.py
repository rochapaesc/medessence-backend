from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.inbox.models import Message, Team
from apps.inbox.services import apply_message_to_conversation
from apps.tenants.models import Clinic

# Setor que toda clínica tem por existir (§4.3.1, RF-ATD-5).
DEFAULT_TEAM_NAME = "Recepção"


@receiver(post_save, sender=Message)
def on_message_saved(sender, instance, created, **kwargs):
    apply_message_to_conversation(instance, created=created)


@receiver(post_save, sender=Clinic)
def on_clinic_created(sender, instance, created, **kwargs):
    """
    Clínica nasce com o setor que ela de fato tem.

    A migration semeia as que já existiam; sem isto, só elas teriam Recepção e
    o B2 herdaria um invariante meia-boca - "toda clínica tem um setor" valendo
    para as antigas e não para as novas é pior do que não valer para nenhuma.
    """
    if created:
        Team.objects.get_or_create(clinic=instance, name=DEFAULT_TEAM_NAME)
