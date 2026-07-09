from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.inbox.models import Message
from apps.inbox.services import apply_message_to_conversation


@receiver(post_save, sender=Message)
def on_message_saved(sender, instance, created, **kwargs):
    apply_message_to_conversation(instance, created=created)
