from django.contrib.admin import site

from apps.automation.admin.flow import FlowAdmin, FlowRunAdmin, FlowVersionAdmin
from apps.automation.models import Flow, FlowRun, FlowVersion

site.register(Flow, FlowAdmin)
site.register(FlowVersion, FlowVersionAdmin)
site.register(FlowRun, FlowRunAdmin)
