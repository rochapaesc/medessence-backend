from django.urls import path
from rest_framework_simplejwt.views import TokenBlacklistView

from apps.accounts.api.views import AuditedTokenObtainPairView, TokenRefreshView

urlpatterns = [
    path("token/", AuditedTokenObtainPairView.as_view(), name="token-obtain"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("token/blacklist/", TokenBlacklistView.as_view(), name="token-blacklist"),
]
