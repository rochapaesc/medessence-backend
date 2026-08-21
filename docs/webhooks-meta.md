# Os webhooks que a Meta entrega (levantamento de 21/08/2026)

> Nasceu de um erro meu: afirmei ao usuário que "a Meta não avisa quando o
> paciente apaga uma mensagem". **Ela avisa** — existe um webhook `revoke`
> dedicado, com o identificador da mensagem apagada, e nós o estávamos
> jogando no balde de "não suportado". Este documento existe para a próxima
> pergunta sobre o que a Meta entrega ser respondida com a lista na mão, em
> vez de com memória.
>
> Fonte: `developers.facebook.com/documentation/business-messaging/whatsapp/`
> `webhooks/reference/messages/`

## Como ler a coluna "aqui"

- **✅** tratado no `apps/integrations/whatsapp/events.py`
- **⬜** chega e cai no `else` (vira `unsupported`, ou é ignorado)
- **n/a** não se aplica ao produto hoje

| Tipo | O que é | Aqui |
|---|---|---|
| `text` | Mensagem de texto | ✅ |
| `image` | Imagem, com `caption` | ✅ |
| `audio` | Áudio e nota de voz | ✅ |
| `video` | Vídeo, com `caption` | ✅ |
| `document` | Arquivo, com `filename` | ✅ |
| `sticker` | Figurinha | ✅ |
| `location` | Localização | ✅ |
| `contacts` | Cartão de contato (vCard) | ✅ |
| `interactive` | Resposta de botão ou lista | ✅ |
| `button` | Resposta de botão de TEMPLATE | ✅ |
| `reaction` | Emoji colado numa mensagem | ✅ |
| `status` | Entrega: sent/delivered/read/failed | ✅ |
| **`revoke`** | **O usuário APAGOU uma mensagem** | ✅ *(21/08/2026)* |
| **`edit`** | **O usuário EDITOU uma mensagem** | ✅ *(21/08/2026)* |
| **`system`** | **O usuário TROCOU DE NÚMERO** | ✅ *(21/08/2026)* |
| `order` | Pedido do catálogo | n/a |
| `group` | Mensagem em grupo (Groups API) | n/a |
| `errors` | Falha de sistema ou de conta | ignorado de propósito |
| `unsupported` | Tipo que a Cloud API não entende | ✅ |

## ⚠️ Os DOIS laços: mensagens e ecos

O `revoke`/`edit` chega em **duas listas diferentes**, e tratar só uma foi um
defeito de 21/08/2026 (visto em produção no mesmo dia da correção):

| Lista | Quem agiu |
|---|---|
| `messages[]` | o **paciente**, no WhatsApp dele |
| `message_echoes[]` | a **clínica**, no app do celular |

Hoje um `_kind_do_evento()` só decide nos dois laços, justamente para não
divergirem de novo. Some com ele e os ecos voltam a virar balão vazio.

O terceiro caminho é o **CRM**: `perform_destroy` do `MessageViewSet` marca
`revoked_at` + `revoked_by`. Nos três, o conteúdo FICA.

## O que resta em aberto

- **`history`** (coexistência, até 180 dias em 3 fases): NÃO tratado, e
  **medido em produção em 21/08: ZERO webhooks arquivados**. A clínica não
  compartilhou o histórico no onboarding (é uma escolha feita na hora de
  conectar, e a Meta só envia naquele momento) — não há o que importar. Se um
  dia outra clínica conectar aceitando compartilhar, o comando de medição
  abaixo acusa, e só então vale desenhar a fatia. Todo
  webhook é arquivado em `WebhookEvent` ANTES de processar, então se a clínica
  compartilhou o histórico no onboarding, os payloads estão guardados em
  produção esperando reprocessamento. Medir lá antes de desenhar:

      docker exec medessence_django python manage.py shell -c "
      import json
      from apps.inbox.models import WebhookEvent
      n = sum(1 for w in WebhookEvent.objects.all().iterator()
              if '\"history\"' in json.dumps(w.payload))
      print('webhooks de history arquivados:', n)"

  Regras já decididas para quando entrar: mensagem histórica NÃO gera não
  lida, NÃO notifica e NÃO dispara fluxo (o robô não pode responder a uma
  mensagem de março); conversa criada por histórico nasce Resolvida.
- **`request_welcome`**: ignorado de propósito hoje; anotado como possível
  sinal de "clicou no anúncio" para a F4 de campanhas.

## Como os dois de 21/08 foram fechados (edit e system)

### `edit` — o paciente corrigiu o que escreveu

Gatilho: o usuário edita uma mensagem **em até 15 minutos**. Vale para texto e
para legenda de mídia.

    "type": "edit",
    "edit": {
      "original_message_id": "wamid.…",     ← QUAL mensagem
      "message": { "type": "image", "image": { "caption": "…" } }   ← o novo conteúdo
    }

**Só em coexistência**, como o `revoke`. ⚠️ A própria doc avisa que "edit
messages are currently delivered as an unsupported message type webhook
instead of an edit webhook" — ou seja, hoje pode chegar como `unsupported` de
qualquer jeito, e a Meta diz que vai restaurar. Vale tratar os dois caminhos.

**Por que importa aqui:** a recepção responde ao que leu. Se o paciente
corrigiu "posso ir dia 12" para "dia 21" e a tela continua mostrando o 12, o
erro vira consulta marcada no dia errado. O modelo já tem `edited_at`.

### `system` — o paciente trocou de número

    "type": "system",
    "system": {
      "type": "user_changed_number",
      "body": "…",          ← frase pronta
      "wa_id": "5589…"      ← o NOVO número
    }

⚠️ Não vem `contacts[]` neste evento.

**Por que importa aqui, e muito:** o número é a chave do contato e o vínculo
com a ficha do paciente (`PatientContact`). Sem tratar isto, o paciente que
troca de chip vira um contato NOVO, sem histórico e sem ficha — e a conversa
antiga fica órfã de alguém que ainda é cliente da clínica. É o mesmo problema
que o nono dígito já nos deu.

## Coexistência: os três webhooks extras

A clínica usa app do celular + API no mesmo número, então recebe também:

| Campo | O que traz |
|---|---|
| `history` | Até 180 dias de conversas anteriores, em três fases |
| `smb_app_state_sync` | Os CONTATOS da agenda do aparelho (RF-CON-5.3) |
| `smb_message_echoes` | O que a clínica manda PELO CELULAR (RF-CON-5.2) |

⚠️ **`revoke` e `edit` são exclusivos de coexistência.** Uma conta só-API não
os recebe — o que explica por que quase nenhum CRM do mercado os trata, e por
que as três referências deste projeto (Chatwoot, wacrm, whatomate) também não.

## A armadilha que custou caro

O parser faz `KIND_MAP.get(meta_type, MessageKind.UNSUPPORTED)`. **Todo tipo
que não está no mapa vira "não suportado" em silêncio** — e, pior, vira um
BALÃO VAZIO na conversa, porque o caminho do `unsupported` também não casa
(o `type` é outro) e o `content_data` sai `{}`.

Foi assim que o `revoke` passou meses despercebido: a tela mostrava "Esta
mensagem não está disponível aqui" e ninguém suspeitou de que a Meta tinha
mandado exatamente o que precisávamos.

**A regra que fica:** ao ver `kind=unsupported` com `content_data` VAZIO,
desconfie do parser antes de culpar a Meta — `unsupported` de verdade sempre
traz `{"unsupported_type": ...}`.
