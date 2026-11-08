# A Ponte: Central de Salas (MCP por dentro, A2A por fora)

Dois processos Python:

- **`servidor-mcp/`**: servidor MCP em Streamable HTTP (revisão `2026-07-28`) na porta `7301`, endpoint `/mcp`. Tem três tools (`listar_salas`, `consultar_disponibilidade`, `reservar_sala`), o resource `politica://uso` e o ciclo de MRTR na reserva.
- **`agente/`**: agente A2A v1.0 (binding JSON-RPC) na porta `7300`, com o endpoint em `/a2a` e o card em `/.well-known/agent-card.json`. Por dentro, ele é um host MCP do servidor acima.

Stack: SDK oficial `mcp==2.3.0` (pacote `mcp` do Python, v2), `a2a-sdk[http-server]==1.3.0` e `uvicorn==0.54.0`. As versões diretas estão travadas em cada `pyproject.toml`, e as transitivas em `requirements.lock`. Sete pacotes (`httpcore2`, `httpx2`, `pycparser`, `python-multipart`, `rpds-py`, `sse-starlette`, `starlette`) exigem Python 3.11 nas versões mais novas. Para esses, o lock tem uma linha por faixa de Python, separada por marcador (`python_version < "3.11"`), e assim o mesmo arquivo serve de 3.10 a 3.14. Não há LLM nem SDK de provedor de LLM: o agente lê um formato fixo de texto e decide por regra.

## Como rodar

Pré-requisitos: Python 3.10+ (o validador passou em 3.10, 3.12 e 3.14), `git` e `curl`.

### 1. Clonar e instalar

```bash
git clone https://github.com/lucasbarbosa87/desafio-a2a-com-mcp.git
```

```bash
cd desafio-a2a-com-mcp
```

```bash
./instalar.sh
```

`instalar.sh` cria `.venv/` na raiz e instala os dois projetos com as versões de `requirements.lock`. Para usar outro interpretador, rode por exemplo `PYTHON=python3.10 ./instalar.sh`.

### 2. Gerar e exportar o segredo do `requestState`

A chave de integridade vem **somente** da variável de ambiente `REQUEST_STATE_SECRET` e precisa ter pelo menos 32 bytes. Sem ela, o servidor MCP se recusa a subir. Gere uma chave sua (não existe valor padrão, e nenhum valor vai para o repositório):

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

Use **o mesmo valor** sempre que reiniciar o servidor MCP. É isso que mantém um `requestState` emitido antes do restart válido depois dele. Se quiser guardar o valor num `.env` local, ele já está no `.gitignore`.

### 3. Subir os dois processos (um terminal para cada)

Terminal 1, servidor MCP (o log de requests sai no stderr deste terminal):

```bash
./subir-servidor-mcp.sh
```

Terminal 2, agente A2A:

```bash
./subir-agente.sh
```

A ordem não importa: o agente só fala com o MCP quando recebe a primeira mensagem. Variáveis opcionais, com os valores padrão: `MCP_HOST=127.0.0.1`, `MCP_PORT=7301`, `AGENTE_HOST=127.0.0.1`, `AGENTE_PORT=7300`, `MCP_URL=http://127.0.0.1:7301/mcp` e `AGENTE_URL_PUBLICA=http://127.0.0.1:7300` (a URL que vai para o card).

### 4. Rodar o validador

Num terceiro terminal, com os dois processos **recém-iniciados**:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

### Conferências manuais do avaliador

O validador cria reservas, e elas mudam as alternativas oferecidas depois. Por exemplo, ao final dele a `sala-fusca` e a `sala-mirante` já estão ocupadas das 14h às 15h. Para os passos manuais 7 a 10, **reinicie os dois processos antes** (Ctrl+C e suba de novo, com o mesmo `REQUEST_STATE_SECRET`).

Card:

```bash
curl -s http://localhost:7300/.well-known/agent-card.json
```

Pausa (passo 7): deve devolver `TASK_STATE_INPUT_REQUIRED` com `alternativas: sala-fusca, sala-mirante`.

```bash
python3 -c "import json; print(json.dumps(json.load(open('exemplos/wire/08-a2a-send-message.json'))['request']['body']))" | curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' -d @-
```

Continuação (passo 8): troque `<TASK_ID>` pelo `result.task.id` da resposta acima.

```bash
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":2,"method":"SendMessage","params":{"message":{"messageId":"msg-continuacao","role":"ROLE_USER","parts":[{"text":"escolha=sala-mirante"}],"taskId":"<TASK_ID>"}}}'
```

```bash
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":3,"method":"GetTask","params":{"id":"<TASK_ID>"}}'
```

Recusa (passo 9): repita a pausa e responda `escolha=recusar`. A Task termina em `TASK_STATE_CANCELED`. Sala inexistente (passo 10):

```bash
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":4,"method":"SendMessage","params":{"message":{"messageId":"msg-inexistente","role":"ROLE_USER","parts":[{"text":"reservar sala=sala-inexistente inicio=2026-11-03T09:00:00-03:00 fim=2026-11-03T10:00:00-03:00 responsavel=Doc"}]}}}'
```

Os passos 11 a 13 (adulterar o `requestState`, reiniciar o MCP entre a pausa e o retry, ou omitir a capability de elicitation) funcionam direto com os corpos e headers de `exemplos/wire/03`, `04` e `06`. O servidor não exige `initialize` nem nenhum request anterior.

### Como ler o log do servidor MCP

Cada request gera uma linha na chegada (`<-`) e uma na saída (`->`):

```
<- method=tools/list id=1 client=agente-central-de-salas traceparent=00-<trace-id>-<span-id>-01 caps={"elicitation":{"form":{}}}
<- method=tools/call id=6 name=reservar_sala client=agente-central-de-salas traceparent=00-<trace-id>-...-01 caps={...}
-> method=tools/call id=6 http=200 resultType=input_required
<- method=tools/call id=7 name=reservar_sala retry=sim client=agente-central-de-salas traceparent=00-<trace-id>-...-01 caps={...}
-> method=tools/call id=7 http=200 resultType=complete
```

O `tools/list` vem antes do primeiro `tools/call` de cada pedido do agente. O trace-id impresso pelo validador aparece no `traceparent`. O retry é marcado com `retry=sim` e tem id diferente do request inicial. Os requests do validador aparecem sem `client=`, porque ele não manda `clientInfo`. O log fica na borda HTTP (`servidor-mcp/logs.py`), por fora do SDK, então até os requests recusados antes de chegar a um handler são registrados.

## Onde a ponte acontece

A ponte inteira fica em `ExecutorDeReservas`, em [agente/agente.py](agente/agente.py). O cliente MCP ([agente/cliente_mcp.py](agente/cliente_mcp.py)) faz `tools/call` com `allow_input_required=True` e não registra callback de elicitation. Assim, o `InputRequiredResult` chega cru ao executor e o SDK não responde a pergunta por conta própria. Em [`_concluir_ou_pausar`](agente/agente.py:224), o `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`: o executor lê a chave e o `enum` (ou `const`) da elicitation, guarda uma `Pausa` com tool, argumentos, chave e o `requestState` opaco em `self._pausas[task_id]` ([linha 242](agente/agente.py:242)), e publica a linha `alternativas: …` como mensagem da Task. O `requestState` volta ao servidor em [`_retomar`](agente/agente.py:291). Ele é chamado quando um `SendMessage` com o mesmo `taskId` traz `escolha=<id>` (ou `escolha=recusar`, que vira `action: decline`), e repete o `tools/call` com os mesmos argumentos, `inputResponses` na chave que o servidor escolheu e o `requestState` reenviado byte a byte ([linha 303](agente/agente.py:303)). O `ClientSession` do SDK gera um id JSON-RPC novo para esse request. Do lado do servidor, quem produz a pergunta é o resolver [`escolha_de_sala`](servidor-mcp/servidor.py:127). Ele devolve um `Elicit` quando há conflito com alternativas, e o SDK transforma isso em `resultType: input_required`, sem nenhum request do servidor para o cliente.

## Decisões técnicas

**Proteção do `requestState`.** Usei o utilitário do próprio SDK, `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600)` ([servidor-mcp/servidor.py:92](servidor-mcp/servidor.py:92)), que instala o `RequestStateBoundary`. O token é AES-256-GCM (AEAD, então assinado e cifrado) com chave derivada por HKDF-SHA256 do segredo. O envelope selado carrega `iat`, `exp`, o método, o nome da tool, um digest dos argumentos e a audiência (nome do servidor). Qualquer caractere trocado falha na tag do GCM, e o servidor responde `-32602` (`Invalid or expired requestState`), nunca um erro interno. Um retry com argumentos diferentes dos selados falha no "request binding" e também recebe `-32602`, então os argumentos adulterados nunca chegam a produzir efeito. O servidor não guarda nada entre o `input_required` e o retry: o estado das respostas viaja no token, os argumentos são conferidos contra o digest selado e o resolver recalcula a pergunta. Por isso o retry funciona depois de um restart, desde que o `REQUEST_STATE_SECRET` seja o mesmo. O segredo com menos de 32 bytes, ou ausente, impede o servidor de subir.

**Validade.** São 10 minutos (`TTL_REQUEST_STATE = 600`), dentro da faixa de 5 a 30 minutos. O valor é por rodada: cada `input_required` sela um token novo.

**Onde fica o estado das Tasks.** No agente, em memória. As Tasks A2A (id, contextId, status, history, artifacts) ficam no `InMemoryTaskStore` do a2a-sdk. A pausa de cada Task (`Pausa`, com o `requestState` opaco) e o trace-id da Task ficam em dicionários do `ExecutorDeReservas`, indexados pelo id da Task. Por isso duas Tasks pausadas ao mesmo tempo nunca trocam de estado. O `requestState` não sai do agente: não aparece no card, nos artifacts nem nas mensagens. As reservas ficam em memória no servidor MCP (as de `dados/reservas.json` e mais as criadas), e nenhum dos dois estados sobrevive a um restart. O único estado que sobrevive é o que viaja no `requestState`.

**Regras de domínio só no servidor.** Validação (sala, intervalo, janela de 08:00 a 20:00 em -03:00, no máximo 2 h), conflito e alternativas estão em [servidor-mcp/dominio.py](servidor-mcp/dominio.py). O agente não sabe o que é uma sala: descobre a tool por `tools/list` em cada pedido, monta os argumentos a partir do `inputSchema` publicado, lê a versão da política em `politica://uso` e só traduz protocolo. Ele confere a escolha contra o `enum` da elicitation que recebeu, que é o contrato da pergunta, e não regra de sala.

**Trace context.** O agente lê o header `traceparent` do request A2A, guarda o trace-id com a Task e manda um `traceparent` com o mesmo trace-id e um span-id novo no `_meta` de todo request MCP daquela Task, inclusive no retry, que chega num `SendMessage` posterior. Sem header, a Task ganha um trace-id próprio.

**Pontos em que o SDK foi ajustado, com o motivo.**

- *Capability exata.* Quando recebe um callback de elicitation, a `ClientSession` do SDK declara `{"elicitation": {"form": {}, "url": {}}}`. Sem callback, não declara nada. Este agente só responde form mode e não usa callback (ele precisa ver o `input_required` cru), então [`_SessaoSoFormMode`](agente/cliente_mcp.py:37) sobrescreve a construção das capabilities para declarar exatamente `{"elicitation": {"form": {}}}`. É o único ponto em que toco um método interno do SDK.
- *Sem handshake.* O cliente adota a revisão `2026-07-28` localmente (`ClientSession.adopt`, sem tráfego) em vez de sondar `server/discover`. Assim, todo request que o agente emite pertence a uma Task e carrega o `traceparent` dela. O SDK continua carimbando `_meta` (protocolVersion, clientInfo, clientCapabilities) e os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` em cada request. O servidor recusa header divergente do corpo com `-32020`.
- *Header `A2A-Version`.* O a2a-sdk segue a spec e interpreta a ausência do header como `0.3`, e recusa esse request no handler v1.0. O validador e os exemplos de wire não mandam o header. Como este endpoint só anuncia `protocolVersion: 1.0`, o [`ContextoV1PorPadrao`](agente/agente.py:315), que é o ponto de extensão público `context_builder`, trata a ausência do header como `1.0`. Um cliente que declare outra versão continua recusado.
- *Mensagem final no `history`.* O a2a-sdk só move `status.message` para o `history` quando um status novo a substitui, e não existe status depois de um estado terminal. [`_terminar`](agente/agente.py:163) publica a mensagem final num `WORKING` e logo depois no estado terminal. Com isso, a mensagem (por exemplo `Sala inexistente: sala-inexistente`) fica no `history` uma única vez e também em `status.message`, como em `exemplos/wire/10`.
- *Telemetria embutida do a2a-sdk desligada* (`OTEL_INSTRUMENTATION_A2A_SDK_ENABLED=false`). Spans de OpenTelemetry estão fora do escopo, e o wrapper dele emitia avisos de contexto no stderr.
- *Aviso conhecido do a2a-sdk.* Com Python 3.10 e 3.12, o stderr do agente pode mostrar `Task was destroyed but it is pending!` com `EventQueueSource._dispatch_loop`. É o laço interno de eventos do a2a-sdk 1.3.0 de uma Task que já terminou, coletado sem ter sido cancelado. O aviso não muda nenhuma resposta, e não o contornei.
- *Prefixo das mensagens de erro.* O SDK MCP prefixa o erro de execução com `Error executing tool <nome>: `, o que o enunciado aceita. O agente repassa o texto da tool sem alteração para a mensagem e o history da Task em `FAILED`.

## Saída do validador

Última execução, a partir de um `git clone` deste fork numa pasta limpa, seguindo só este README, com `PYTHON=python3.10 ./instalar.sh` e os dois processos recém-iniciados:

```
trace-id desta execucao: 21fc80f287aeca93b4b0f06fcf8f199b
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
