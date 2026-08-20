from django.db.models import Q
from django_filters.rest_framework import (
    CharFilter,
    ChoiceFilter,
    FilterSet,
    NumberFilter,
)

from apps.patients.api.windows import parse_practitioner_ids
from apps.patients.choices import Gender, PatientStatus
from apps.patients.models import Patient
from apps.patients.models.patient import ABSENCE_RANGES
from apps.patients.phone import grafias_de_busca, so_digitos


class PatientFilterset(FilterSet):
    """Filtros do CRM (RF-PAC-1): busca server-side, tag, cidade, status e profissional."""

    search = CharFilter(method="filter_search")
    tag = CharFilter(method="filter_tag")
    city = CharFilter(method="filter_city")
    gender = CharFilter(method="filter_gender")
    status = ChoiceFilter(choices=PatientStatus.choices, method="filter_status")
    # ⚠️ `CharFilter`, e NÃO `NumberFilter`: a tela de Reativação manda vários
    # ids separados por vírgula, e o `NumberFilter` recusava com 400 antes de
    # o método abaixo (que sabe dividir desde 11/08/2026) chegar a rodar. O
    # método mudou e o tipo do campo não.
    practitioner = CharFilter(method="filter_practitioner")
    # RF-REA-1.1/1.2: o recorte da fila de resgate e as faixas de ausência.
    segment = ChoiceFilter(
        choices=[("to_reactivate", "Fila de resgate")],
        method="filter_segment",
    )
    absence = ChoiceFilter(
        choices=[(faixa, faixa) for faixa in ABSENCE_RANGES],
        method="filter_absence",
    )

    class Meta:
        model = Patient
        fields = [
            "search",
            "tag",
            "city",
            "status",
            "practitioner",
            "source",
            "state",
            "segment",
            "absence",
            "gender",
        ]

    def filter_search(self, queryset, name, value):
        filtro = Q(name__icontains=value) | Q(cpf__icontains=value) | Q(phone__icontains=value)
        digitos = so_digitos(value)
        if len(digitos) >= 8:
            # Busca por telefone de verdade (§6.2): o mesmo número vive em
            # várias grafias no banco (EHR com 55, form sem, wa_id com/sem o
            # nono 9) — o icontains cru acima só acha coincidência de formato.
            filtro |= Q(phone__in=grafias_de_busca(digitos))
        return queryset.filter(filtro)

    def filter_tag(self, queryset, name, value):
        """
        Uma etiqueta ou várias, separadas por vírgula (RF-REA-1.3).

        `?tag=3,7` traz quem tem QUALQUER uma delas, no mesmo formato do
        `?label=` do Inbox: o uso esperado é juntar cidades vizinhas numa
        campanha só, e cruzar as duas devolveria quase ninguém. Valor não
        numérico é ignorado em vez de estourar 500 - a querystring vem da URL
        e qualquer um pode digitar.
        """
        ids = [parte for parte in str(value).split(",") if parte.strip().isdigit()]
        if not ids:
            return queryset
        return queryset.filter(
            patient_tags__tag_id__in=ids,
            patient_tags__deleted_at__isnull=True,
        ).distinct()

    def filter_city(self, queryset, name, value):
        """
        Uma cidade ou várias, separadas por vírgula (RF-REA-1.8).

        ⚠️ Compara o texto CRU do cadastro, sem normalizar caixa nem acento.
        Decisão de 11/08/2026: se a clínica digitou assim, fica assim, e
        `São Raimundo Nonato` e `SAO RAIMUNDO NONATO PIAUI` são duas entradas.
        Tentar juntá-las por regra automática mutila nome de verdade: a
        primeira tentativa aqui transformou `São João do Piauí` em
        `São João do`, porque o "Piauí" que parecia sufixo de estado é parte
        do nome da cidade.

        Vírgula é o separador em todo o resto da tela, e por isso cidade com
        vírgula no nome não é endereçável por este filtro - nenhuma das 141 da
        clínica real tem.
        """
        cidades = [parte.strip() for parte in str(value).split(",") if parte.strip()]
        if not cidades:
            return queryset
        filtro = Q()
        for cidade in cidades:
            filtro |= Q(city__iexact=cidade)
        return queryset.filter(filtro)

    def filter_gender(self, queryset, name, value):
        """Um gênero ou vários, por vírgula. Valor desconhecido é ignorado."""
        validos = {opcao for opcao, _ in Gender.choices}
        escolhidos = [
            parte.strip()
            for parte in str(value).split(",")
            if parte.strip() in validos
        ]
        if not escolhidos:
            return queryset
        return queryset.filter(gender__in=escolhidos)

    def filter_segment(self, queryset, name, value):
        """
        A fila de resgate (RF-REA-1.1), com a mesma janela do `status`.

        Traz junto o profissional da última consulta, que a linha da fila
        mostra e as outras listagens de paciente não precisam.
        """
        if value != "to_reactivate":
            return queryset
        window_days, practitioner = self._resolve_window()
        return queryset.to_reactivate(
            window_days=window_days, practitioner=practitioner
        ).com_ultimo_profissional()

    def filter_absence(self, queryset, name, value):
        """Faixa por tempo de ausência (RF-REA-1.2)."""
        _, practitioner = self._resolve_window()
        return queryset.by_absence(value, practitioner=practitioner)

    def filter_status(self, queryset, name, value):
        """
        Janela configurável (RF-PAC-2): padrão da clínica; com ?practitioner=
        junto, usa a janela efetiva DELE e a atividade relativa à carteira.
        """
        window_days, practitioner = self._resolve_window()
        return queryset.by_status(value, window_days=window_days, practitioner=practitioner)

    def filter_practitioner(self, queryset, name, value):
        """
        Um profissional ou vários, por vírgula.

        ⚠️ Era `NumberFilter` de valor único até 11/08/2026, e a mudança é
        compatível: `?practitioner=3` continua funcionando igual. O filtro da
        Reativação é multi-valor como os outros da coluna.
        """
        ids = parse_practitioner_ids(value)
        if not ids:
            return queryset
        return queryset.filter(appointments__practitioner_id__in=ids).distinct()

    def _resolve_window(self):
        from apps.core.context import resolve_active_membership
        from apps.patients.api.windows import parse_window
        from apps.scheduling.models import Practitioner

        clinic = resolve_active_membership(self.request).clinic
        override = parse_window(self.request)
        ids = parse_practitioner_ids(self.data.get("practitioner"))

        # ⚠️ A janela do profissional só vale com UM escolhido. Com vários, "a
        # janela de qual deles" não tem resposta, e a atividade relativa à
        # carteira também não: cair na da clínica é a única leitura honesta.
        practitioner = None
        if len(ids) == 1:
            practitioner = Practitioner.objects.filter(clinic=clinic, pk=ids[0]).first()
        if practitioner is not None:
            return override or practitioner.effective_active_window_days, practitioner
        return override or clinic.active_window_days, None
