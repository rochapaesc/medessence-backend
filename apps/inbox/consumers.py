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

    async def disconnect(self, code):
        group = getattr(self, "group", None)
        if group is not None:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def inbox_event(self, event):
        """Handler do `group_send` (type="inbox.event") → envia o payload ao cliente."""
        await self.send_json(event["data"])
