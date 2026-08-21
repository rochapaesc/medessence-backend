from django.urls import path

from apps.accounts.api.views import (
    AuditedTokenBlacklistView,
    AuditedTokenObtainPairView,
    TokenRefreshView,
)

urlpatterns = [
    path("token/", AuditedTokenObtainPairView.as_view(), name="token-obtain"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("token/blacklist/", AuditedTokenBlacklistView.as_view(), name="token-blacklist"),
]
