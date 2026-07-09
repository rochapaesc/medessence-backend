from django.contrib.admin import site

from apps.accounts.admin.membership import MembershipAdmin
from apps.accounts.admin.user import UserAdmin
from apps.accounts.models import Membership, User

site.register(User, UserAdmin)
site.register(Membership, MembershipAdmin)
