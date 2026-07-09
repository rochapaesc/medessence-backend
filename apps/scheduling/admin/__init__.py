from django.contrib.admin import site

from apps.scheduling.admin.appointment import AppointmentAdmin
from apps.scheduling.admin.catalogs import (
    CareUnitAdmin,
    EHRStatusMapAdmin,
    InsuranceCompanyAdmin,
    PractitionerAdmin,
    ProcedureAdmin,
)
from apps.scheduling.models import (
    Appointment,
    CareUnit,
    EHRStatusMap,
    InsuranceCompany,
    Practitioner,
    Procedure,
)

site.register(Appointment, AppointmentAdmin)
site.register(Practitioner, PractitionerAdmin)
site.register(CareUnit, CareUnitAdmin)
site.register(Procedure, ProcedureAdmin)
site.register(InsuranceCompany, InsuranceCompanyAdmin)
site.register(EHRStatusMap, EHRStatusMapAdmin)
