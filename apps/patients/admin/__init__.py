from django.contrib.admin import site

from apps.patients.admin.contact import ContactAdmin
from apps.patients.admin.patient import PatientAdmin
from apps.patients.admin.tag import TagAdmin
from apps.patients.models import Contact, Patient, Tag

site.register(Patient, PatientAdmin)
site.register(Contact, ContactAdmin)
site.register(Tag, TagAdmin)
