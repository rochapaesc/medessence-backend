"""
Consumer do inbox (§12) - papel único: empurrar eventos do servidor para a
tela. Nenhuma regra de negócio; a fonte da verdade é a API REST (o cliente faz
catch-up via REST ao reconectar).
"""

from channels.generic.websocket import AsyncJsonWebsocketConsumer


class InboxConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        membership = self.scope.get("membership")
        if membership is None:
            await self.close(code=4401)
            return
        self.group = f"inbox_clinic_{membership.clinic_id}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        # "hello" é o SINAL DE VIDA: o handshake do WebSocket no navegador é
        # preguiçoso e uma recusa (4401) só aparece no onDone - sem um primeiro
        # frame, o cliente não tem como saber se a conexão vingou. Marcar
        # online "no otimismo" foi o que fazia a tela piscar online/offline.
        await self.send_json({"event": "hello"})

    async def receive_json(self, content, **kwargs):
        """Ping/pong do watchdog do cliente. Queda SILENCIOSA (Wi-Fi trocando,
        túnel morto) não fecha o socket - sem isto, a tela fica "online" para
        sempre num socket que não recebe nada."""
        if content.get("event") == "ping":
            await self.send_json({"event": "pong"})

    async def disconnect(self, code):
        group = getattr(self, "group", None)
        if group is not None:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def inbox_event(self, event):
        """Handler do `group_send` (type="inbox.event") → envia o payload ao cliente."""
        await self.send_json(event["data"])
