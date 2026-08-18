# nginx na frente da API (produção)

O que este documento existe para evitar: **o Inbox subir sem tempo real e o
sintoma parecer problema do backend.** Aconteceu em 18/08/2026, no primeiro
deploy de produção.

## O sintoma

No log do backend, a cada tentativa do app:

```
[WARNING] django.request Not Found: /ws/inbox/
10.32.0.1:50062 - "GET /ws/inbox/?token=...&clinic_id=1 HTTP/1.0" 404
```

E na tela: as conversas carregam, mas **nada atualiza sozinho**. Mensagem que
chega só aparece ao recarregar a página, e é fácil ler isso como "não recebe".

## A causa

O handshake do WebSocket chegou ao uvicorn **sem os cabeçalhos de upgrade**.
Sem eles, o ASGI trata como requisição HTTP comum, manda para o ramo `http`,
o Django não tem essa rota e devolve 404.

O `HTTP/1.0` na linha de acesso é a assinatura: o nginx faz `proxy_pass` em
HTTP/1.0 por padrão, e **WebSocket exige HTTP/1.1** (RFC 6455).

⚠️ Não é o gunicorn e não é o `worker_class`. Quem fala HTTP e WebSocket é o
uvicorn dentro do worker (ver `config/gunicorn.conf.py`), e o `websockets` vem
no `uvicorn[standard]` do `requirements.txt`. Para provar em 10 segundos, os
dois comandos abaixo, **de dentro do servidor**:

```bash
# Direto no gunicorn, com os cabeçalhos de upgrade: espera-se 403
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  "http://127.0.0.1:8000/ws/inbox/?token=x&clinic_id=1"

# O mesmo pela URL pública: 404 = o nginx comeu os cabeçalhos
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  "https://api.institutomedessence.com.br/ws/inbox/?token=x&clinic_id=1"
```

O **403** é o resultado bom: significa que a requisição chegou ao ramo
WebSocket e o token falso foi recusado pelo middleware. O que não pode
aparecer é **404**.

## A correção

No `server` da API (`api.institutomedessence.com.br`), acrescentar um
`location` só para o WebSocket. Ele não substitui o `location /`: no nginx o
prefixo mais longo vence, independentemente da ordem.

```nginx
    # WebSocket do Inbox (tempo real, §12).
    #
    # ⚠️ As três primeiras linhas são o ponto inteiro: sem `proxy_http_version
    # 1.1` o nginx fala HTTP/1.0 com o upstream e o handshake é impossível;
    # sem repassar `Upgrade`/`Connection` o pedido chega como HTTP comum e o
    # Django devolve 404.
    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Abrir a conexão com o upstream local é instantâneo; este é o padrão
        # do nginx, repetido só para ficar explícito. ⚠️ O valor do
        # `location /` NÃO chega aqui: no nginx a herança vem do nível
        # `http`/`server`, nunca de outra `location`.
        proxy_connect_timeout 60s;

        # ⚠️ ESTE é o que decide. No `location /` ele significa "a API tem 60s
        # para responder"; aqui a conexão fica ABERTA e calada entre uma
        # mensagem e outra, e 60s sem tráfego a derrubam. Numa clínica parada
        # por um minuto o Inbox cairia e reconectaria em looping.
        #
        # O uvicorn manda ping a cada 20s, o que na prática já a seguraria,
        # mas depender do intervalo de ping de uma dependência é frágil:
        # trocar a versão do uvicorn derrubaria o Inbox inteiro sem nada no
        # log da aplicação.
        proxy_read_timeout 3600s;

        # Vale para a escrita EM DIREÇÃO ao upstream e raramente é o que
        # derruba. Vai junto por simetria, não por ser igualmente crítico.
        proxy_send_timeout 3600s;

        # Sem `add_header` aqui de propósito: um `add_header` na location
        # DESCARTA todos os do nível server, e os de segurança se perderiam.
    }
```

E no `location /` da mesma API, acrescentar `proxy_http_version 1.1;` (não é
obrigatório, mas mantém conexão viva com o upstream em vez de abrir uma por
requisição).

Aplicar:

```bash
nginx -t && systemctl reload nginx
```

Não precisa reiniciar o backend: a mudança é toda do proxy.

## Depois de aplicar

O log de acesso do backend passa a mostrar `HTTP/1.1` e o `/ws/inbox/` some
dos 404. Na tela, mensagem nova aparece sem recarregar.

## O resto do deploy que morde junto

Estas duas são independentes do nginx e apareceram no mesmo dia:

- **Envio bloqueado** quando o canal está marcado como caído
  (`Channel.disconnected_at`). Trocar o token **não cura sozinho**: o canal só
  se cura com uma chamada bem-sucedida à Meta. A porta de saída está na tela,
  no aviso do canal: **"Já reconectei · verificar"**
  (`POST /conversations/check-channel/`).
- **Recebimento** exige o webhook da Meta apontando para
  `https://api.institutomedessence.com.br/webhooks/whatsapp/meta/`, com
  `WHATSAPP_VERIFY_TOKEN` e `WHATSAPP_APP_SECRET` do `.env` batendo com os do
  app. Assinatura errada vira descarte silencioso, sem erro na tela.
