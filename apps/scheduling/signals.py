from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.scheduling.models import Appointment
from apps.scheduling.services import recalculate_last_appointment


@receiver(post_save, sender=Appointment)
def on_appointment_saved(sender, instance, **kwargs):
    recalculate_last_appointment(instance.patient)


@receiver(post_delete, sender=Appointment)
def on_appointment_deleted(sender, instance, **kwargs):
    recalculate_last_appointment(instance.patient)
