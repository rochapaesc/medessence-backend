"""
Consumer do inbox (§12) - papel único: empurrar eventos do servidor para a
tela. Nenhuma regra de negócio; a fonte da verdade é a API REST (o cliente faz
catch-up via REST ao reconectar).
"""

import time

from channels.generic.websocket import AsyncJsonWebsocketConsumer


class InboxConsumer(AsyncJsonWebsocketConsumer):
    #: Renova a inscrição no máximo uma vez por este intervalo (segundos).
    #: Quem dita o ritmo do ping é o CLIENTE, e o custo no Redis não pode
    #: depender disso: abaixo dos 25s do ping legítimo, toda batida renova; um
    #: cliente em laço não vira escrita em rajada.
    INTERVALO_DE_RENOVACAO = 15

    #: Quando a inscrição foi gravada pela última vez. Relógio MONOTÔNICO:
    #: acerto de hora no servidor não pode fazer a renovação parar.
    _inscrito_em = 0.0

    async def connect(self):
        membership = self.scope.get("membership")
        if membership is None:
            await self.close(code=4401)
            return
        self.group = f"inbox_clinic_{membership.clinic_id}"
        await self._inscrever()
        await self.accept()
        # "hello" é o SINAL DE VIDA: o handshake do WebSocket no navegador é
        # preguiçoso e uma recusa (4401) só aparece no onDone - sem um primeiro
        # frame, o cliente não tem como saber se a conexão vingou. Marcar
        # online "no otimismo" foi o que fazia a tela piscar online/offline.
        await self.send_json({"event": "hello"})

    async def _inscrever(self):
        """Entra (ou PERMANECE) na lista de quem recebe os eventos da clínica.

        ⚠️ `group_add` não é só "entrar": ele REGRAVA a hora da inscrição, e é
        por isso que serve de renovação. O channels_redis guarda cada inscrito
        num conjunto ordenado pontuado pelo instante da inscrição, e toda
        publicação começa APAGANDO quem passou de `group_expiry`.

        Sem renovar, a aba aberta desde ontem saía da lista com o socket VIVO,
        e nada avisava: o ping é respondido aqui mesmo, em `receive_json`, sem
        tocar no grupo nem no Redis, então o cliente seguia "online", sem faixa
        de aviso, e simplesmente não recebia mais nada até alguém recarregar a
        página. Achado em produção em 19/08/2026, com a aba da recepção aberta
        desde a véspera.
        """
        await self.channel_layer.group_add(self.group, self.channel_name)
        self._inscrito_em = time.monotonic()

    async def _renovar_se_preciso(self):
        """Renova no máximo uma vez por [INTERVALO_DE_RENOVACAO].

        Chamada nos DOIS sinais de que a conexão está viva, e é preciso os dois:
        o cliente só pinga depois de 25s SEM RECEBER NADA (cada frame rearma o
        relógio dele), então uma conexão movimentada o tempo todo jamais
        pingaria - e é justamente a movimentada que não pode parar de receber.
        """
        if time.monotonic() - self._inscrito_em >= self.INTERVALO_DE_RENOVACAO:
            await self._inscrever()

    async def receive_json(self, content, **kwargs):
        """Ping/pong do watchdog do cliente. Queda SILENCIOSA (Wi-Fi trocando,
        túnel morto) não fecha o socket - sem isto, a tela fica "online" para
        sempre num socket que não recebe nada.

        O ping também RENOVA a inscrição (ver `_inscrever`): é a mesma batida
        que já chega a cada 25s, e é ela que impede a aba de envelhecer fora da
        lista. ⚠️ Falha ao renovar derruba a conexão DE PROPÓSITO, sem try: o
        cliente reconecta com espera crescente, mostra a faixa de sem tempo
        real e refaz o catch-up pelo REST. Engolir o erro aqui reproduziria
        exatamente o defeito que a renovação veio consertar - tela dizendo
        conectada, e nada chegando.
        """
        if content.get("event") == "ping":
            await self._renovar_se_preciso()
            await self.send_json({"event": "pong"})

    async def disconnect(self, code):
        group = getattr(self, "group", None)
        if group is not None:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def inbox_event(self, event):
        """Handler do `group_send` (type="inbox.event") → envia o payload ao cliente."""
        await self.send_json(event["data"])
        # Depois de entregar, nunca antes: renovar não pode atrasar a mensagem
        # de quem está com o paciente no telefone.
        await self._renovar_se_preciso()
