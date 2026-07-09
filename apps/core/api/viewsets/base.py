from django.conf import settings
from django.db import IntegrityError
from django.http import Http404
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_cookie
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.exceptions import NotFound
from rest_framework.filters import OrderingFilter
from rest_framework.mixins import (
    CreateModelMixin,
    DestroyModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import GenericViewSet

from apps.core.exceptions import ConflictException


class CachedMixin:
    @method_decorator(cache_page(settings.CACHE_TTL))
    @method_decorator(vary_on_cookie)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)


class BaseGenericViewSet(GenericViewSet):
    parser_classes = [JSONParser]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    model = None
    serializer_class = None  # Default
    action_serializer_classes = {
        # 'list': None,
        # 'retrieve': None,
        # 'create': None,
        # 'update': None,
        # 'partial_update': None,
        # 'destroy': None,
    }
    filterset_class = None
    queryset = None
    select_related = []
    permission_classes = [IsAuthenticated]
    action_permission_classes = {
        # "list": [IsClinicMember],
        # "create": [IsClinicManager],
    }

    def get_queryset(self):
        if self.model:
            queryset = self.model.objects.all()
            if self.select_related:
                queryset = queryset.select_related(*self.select_related)
            return queryset
        return super().get_queryset()

    def get_serializer_class(self):
        if self.action in self.action_serializer_classes:
            if isinstance(self.action_serializer_classes[self.action], dict):
                return self.action_serializer_classes[self.action][self.request.method]
            return self.action_serializer_classes[self.action]
        return self.serializer_class

    @property
    def search_fields(self):
        return self.filterset_class._meta.search_fields if self.filterset_class else []

    def get_permissions(self):
        if self.action in self.action_permission_classes:
            action_permissions = self.action_permission_classes[self.action]
            if isinstance(action_permissions, dict):
                action_permissions = action_permissions[self.request.method]
            return [permission() for permission in action_permissions]
        return [permission() for permission in self.permission_classes]


class ListModelViewSet(BaseGenericViewSet, ListModelMixin):
    pass


class BaseReadOnlyModelViewSet(BaseGenericViewSet, RetrieveModelMixin, ListModelMixin):
    pass


class BaseCreateDestroyViewSet(
    BaseGenericViewSet,
    CreateModelMixin,
    DestroyModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
):
    """
    Criação, leitura e exclusão — sem update.
    Para recursos imutáveis após a criação.
    """

    def get_object(self):
        try:
            return super().get_object()
        except Http404 as exc:
            raise NotFound("O registro que você tentou acessar ou deletar não existe.") from exc

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except IntegrityError as exc:
            raise ConflictException() from exc


class BaseModelViewSet(
    BaseGenericViewSet,
    CreateModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
    ListModelMixin,
):
    """CRUD completo."""

    def get_object(self):
        try:
            return super().get_object()
        except Http404 as exc:
            raise NotFound("O registro que você tentou acessar ou deletar não existe.") from exc

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except IntegrityError as exc:
            raise ConflictException() from exc
