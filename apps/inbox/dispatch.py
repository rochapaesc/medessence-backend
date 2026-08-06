"""
Sinais que o Inbox emite para quem quiser reagir.

Vive num módulo próprio, e não em `signals.py`, porque aquele importa
`services.py` e `services.py` precisa emitir daqui - juntos dariam import
circular.

Por que sinal e não chamada direta: o motor de fluxos (F2.6) precisa saber
que o paciente falou, mas o Inbox não pode passar a depender do app de
automação. Com o sinal, a dependência fica num sentido só - `automation`
conhece `inbox`, e o `inbox` não sabe quem escuta.
"""

from django.dispatch import Signal

# kwargs: conversation, message. Emitido no fim da ingestão de uma mensagem
# DO CONTATO, com a conversa já atualizada.
inbound_ingested = Signal()
