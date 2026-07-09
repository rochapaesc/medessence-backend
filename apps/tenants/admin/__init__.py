from django.contrib.admin import site

from apps.tenants.admin.clinic import ClinicAdmin
from apps.tenants.models import Clinic

site.register(Clinic, ClinicAdmin)
