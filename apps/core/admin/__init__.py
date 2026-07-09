from django.contrib.admin import site
from django.contrib.auth.models import Group
from django_celery_beat.models import (
    ClockedSchedule,
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
    SolarSchedule,
)
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from apps.core.admin.audit_log import AuditLogAdmin
from apps.core.models.audit_log import AuditLog

site.unregister(Group)
site.unregister(PeriodicTask)
site.unregister(IntervalSchedule)
site.unregister(ClockedSchedule)
site.unregister(SolarSchedule)
site.unregister(CrontabSchedule)
site.unregister(BlacklistedToken)
site.unregister(OutstandingToken)

site.register(AuditLog, AuditLogAdmin)
