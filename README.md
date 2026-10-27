# AI Scrum Flow

Time Scrum de agentes de IA. O **Product Owner é o orquestrador**; ele delega ao **Scrum Master**, que coordena o **Dev Team**
(arquiteto, fullstack, UI/UX e QA). Agentes usam ferramentas via **MCP** e conversam entre si via **A2A**. Tudo é **auditado**.

Junta os dois projetos: o dashboard Scrum (HTML) virou a UI real, e o backend de MCP/auditoria (Django) virou este serviço Flask.

**Stack:** Python 3.12+ · **uv** · **Flask** · SQLAlchemy · **PostgreSQL** (banco **e** fila de jobs, via **procrastinate**) · **Ollama** · **Tailwind CSS v4** · **TOML** (`pyproject.toml`, `flow.toml`, `team.toml`).

```
humano ─▶ PO ──A2A──▶ SM ──A2A──▶ Developers ──▶ QA ──▶ PO aceita (Sprint Review) ──▶ Retro (SM)
           │ ▲            tools (MCP / registro interno) · memória · skills · contexto
           └─┴──────────────────  auditoria com hash encadeado  ───────────────────
```

## Rodando

**Somente PostgreSQL** (não há suporte a SQLite: o app recusa outro banco ao subir).

```bash
# 1) PostgreSQL 14+ instalado e rodando (veja “Banco” abaixo) e o banco `flow` criado
uv sync
ollama pull gemma3:4b                         # ou outro modelo; `ollama list` aparece no seletor da UI
uv run flask --app flow seed                  # cria o time a partir de team.toml (e o esquema da fila)
uv run flask --app flow run                   # http://localhost:5000 — o servidor já sobe um worker embutido
```
Sem Ollama (demo offline, determinístico): `uv run flask --app flow seed --provider mock`.
No dashboard: **+ Pedir ao PO** → ele cria as user stories → **Executar Sprint**. Linha de comando: `uv run flask --app flow run-sprint "Tela de login; API de relatórios"`.

### Banco (PostgreSQL nativo, sem Docker)

Instale o PostgreSQL 14+ (Windows: instalador oficial em postgresql.org ou `winget install PostgreSQL.PostgreSQL.16`; macOS: `brew install postgresql@16`; Debian/Ubuntu: `sudo apt install postgresql`). Depois crie o usuário e os bancos (`psql -U postgres`):

```sql
CREATE USER flow WITH PASSWORD 'flow' CREATEDB;
CREATE DATABASE flow OWNER flow;
CREATE DATABASE flow_test OWNER flow;   -- só para os testes
```
O padrão do app é `postgresql://flow:flow@localhost:5432/flow`; para outro usuário/host/porta defina `DATABASE_URL` (ou `[database] url` no `flow.toml`). As tabelas e o esquema da fila são criados sozinhos na primeira subida.

### Fila de jobs (procrastinate sobre o PostgreSQL)

Refinamento, sprint, CI e deploy **não rodam no processo web**: a API só enfileira (`202`) e um **worker** executa. Vantagens: o job **sobrevive a reinício** do servidor, tem **retry com espera**, `lock` garante **uma sprint por vez** (o workspace é um repositório git), e job de worker morto volta para a fila.

| Job | Regras |
|---|---|
| `flow.refine` | 2 tentativas, espera 15 s |
| `flow.run_sprint` | `lock="workspace"` (um por vez), `queueing_lock` por sprint (clique duplo = 409), **abortável** (botão *Pausar* → o runner para entre histórias), 2 tentativas; ao repetir/retomar, histórias “no meio” voltam ao Sprint Backlog |
| `flow.cd_ci` · `cd_deploy` · `cd_stop` | CI, subida e encerramento da aplicação (executados pelo worker que é dono do processo) |
| `flow.recover_stalled` | periódico (1/min): job `doing` de worker sem heartbeat volta à fila |

- **Worker embutido** (padrão em dev, `[worker] embedded = true`): o 1º request do `flask run` sobe um worker em thread. Para separar: `FLOW_EMBEDDED_WORKER=false` no servidor e `uv run flask --app flow worker` noutro terminal. **Rode um worker por máquina**: quem sobe a aplicação do CI/CD é o processo dono dela.
- Sem worker ativo o dashboard mostra um aviso (e quantos jobs esperam). `uv run flask --app flow queue-status` e a aba **Ao vivo** listam a fila. API: `GET /api/jobs`.
- **Windows:** o psycopg assíncrono não funciona com o loop padrão do Windows; o app força o `WindowsSelectorEventLoopPolicy` automaticamente. Se o worker reclamar de event loop, atualize o Python 3.12+/psycopg.
- Esquema da fila aplicado sozinho na primeira subida (`procrastinate_*`). Atualizações futuras da biblioteca podem exigir migração (`procrastinate schema --migrations-path`).
- Teste de queda real feito aqui: `kill -9` no servidor no meio da sprint → ao subir de novo a sprint foi retomada e as 3 histórias terminaram.

Tailwind: `uv run tailwindcss -i src/flow/static/css/input.css -o src/flow/static/css/app.css --minify` (o `app.css` já vai compilado).
Testes (PostgreSQL; o banco é recriado a cada teste): `TEST_DATABASE_URL=postgresql://flow:flow@localhost:5432/flow_test uv run pytest` (crie o banco `flow_test` antes; sem a variável, usa `127.0.0.1:5433/flow_test`).

## Acompanhamento no dashboard

- **Linha do tempo da sprint:** Refinamento → Planning → Execução → Sprint Review → Retrospectiva → Release & Deploy, com ✓ concluído, ● em andamento (pulsando) e pendente.
- **Linha do tempo de cada história** (clique no card): Refinamento → Planejamento → Desenvolvimento → Verificação QA → Aceite do PO → Concluído, com **quem** fez cada etapa, **quando**, e o detalhe (ex.: nota do QA, retrabalho). Retrabalho reabre as etapas seguintes; o histórico completo continua na auditoria. API: `GET /api/items/<id>/timeline`, `GET /api/sprints/<id>/timeline`.
- **Quem está fazendo o quê, ao vivo:** cada agente mostra *trabalhando/ocioso*, a cerimônia, o item, a ação atual (“pensando — passo 2/6”, “escrevendo US-001/entrega.md”) e há quanto tempo; aba **Ao vivo** com tabela e feed em linguagem natural. API: `GET /api/activity`. O estado é gravado no banco (vale com vários workers); um agente “trabalhando” sem sinal por `timeout + 60s` aparece como ocioso (ex.: servidor reiniciado no meio).
- **Onde vejo o resultado:** no modal da história, a seção **Entregáveis** lista e mostra os arquivos que os agentes gravaram (`workspace/sprint-N/US-xxx/`). API: `GET /api/items/<id>/files[/<arquivo>]`.
- **Sprint encerrada:** o botão vira **Nova sprint com o backlog** (o que não ficou pronto volta ao Product Backlog).
- Horários exibidos no fuso do seu navegador (o banco guarda UTC). Demo mais lenta/visível com o mock: `FLOW_MOCK_DELAY=1.2`.

## Git por etapa (o workspace é um repositório)

O runner — não o LLM — versiona cada etapa Scrum em `workspace/` (um repositório git criado sozinho; requer `git` instalado, `[git] enabled = true`):

```
main ◀── release (merge --no-ff + tag sprint-N) ── sprint/N ◀── aceite do PO (merge --no-ff + tag story/US-xxx) ── feature/US-xxx
                                                      ▲                                   commits: implement · verify · (retrabalho…) · accept
                                    backlog ──────────┘  (cada história refinada = 1 commit `refine(US-xxx)`)
```

| Etapa | Commit | Autor |
|---|---|---|
| Refinamento | `refine(US-xxx)` em `backlog/US-xxx.md` (branch `backlog`, worktree próprio: refina-se durante uma sprint sem trocar o checkout) | Product Owner |
| Planning | `plan(Sprint N)` + `sprint-N/PLANNING.md` na branch `sprint/N` | Scrum Master |
| Desenvolvimento | `implement(US-xxx)` em `feature/US-xxx` (1 por retrabalho — nada é sobrescrito) | o dev atribuído |
| Verificação | `verify(US-xxx)` com o veredito do QA no corpo (commit vazio se não houve arquivo) | QA |
| Aceite | `accept` + `merge(US-xxx)` na sprint + tag `story/US-xxx` | Product Owner |
| Rejeição | `reject(US-xxx)`; a branch **não** é mergeada e fica como evidência | PO/QA |
| Review/Retro | `review(Sprint N)` + `REVIEW.md`, depois `release(Sprint N)` em `main` + tag anotada `sprint-N` | SM / PO |

Cada commit tem o agente como autor (`Nome <slug@agents.flow.local>`), trailers `Flow-Sprint/Story/Stage/Agent/Result` e **um evento `git_commit` na auditoria** (com o SHA). Os agentes não têm comandos git: só leitura via `git_log`. Se o git falhar (disco, permissão), vira evento `git_error` e **a sprint continua**.
Dashboard: aba **Git** (branches mergeadas/abertas, tags, commits por etapa, diff ao clicar) e, no modal da história, os commits da `feature/US-xxx`. Entregáveis de histórias rejeitadas continuam visíveis (lidos da branch). API: `GET /api/git/status|log|branches`, `/api/git/commits/<sha>`, `/api/items/<id>/git`.
Como o repositório é único, **roda-se uma sprint por vez** (`lock` na fila; `409` se outra estiver na fila/rodando). Quer abrir o repositório: `cd workspace && git log --graph --all --oneline`.

## CI/CD local (o agente 🚀 DevOps sobe a aplicação na sua máquina)

Ao fim da sprint, a tag `sprint-N` vira uma *release* (aplicação com `flow-app.toml` ou site estático com `index.html`). O agente **devops** (via A2A, com as tools `ci_run`, `deploy_app`, `deploy_status`, `deploy_stop`, `deploy_logs`):

1. **CI** — exporta a versão exata da tag para `workspace/.deploy/…` (não mexe no working tree), cria um venv se houver `requirements.txt`, instala e roda os passos do manifesto (padrão: byte-compile dos `.py`).
2. **Aprovação humana** — aba **Deploy** → *Aprovar e subir*. (`[cd] auto_deploy = true` dispensa, não recomendado.)
3. **Deploy** — o agente chama `deploy_app`: sobe o processo em `127.0.0.1:<porta>`, espera ficar saudável, devolve a URL (aparece em **NO AR** no topo). A nova release substitui a anterior do mesmo app; *Parar* encerra; *Logs* mostra a saída.

A história que entrega uma aplicação inclui um `flow-app.toml` (os devs têm a skill `app-manifest`):

```toml
name = "login"
[ci]
steps = [["python", "-m", "pytest", "-q"]]    # opcional
[run]
command = ["python", "app.py"]                 # o app lê PORT e escuta em 127.0.0.1
port    = 8101                                 # dentro de [cd].port_range (8100–8199)
health  = "/"                                  # opcional
```

**Segurança — o código vem de um LLM e roda na SUA máquina com as SUAS permissões:** nada sobe sem o seu clique; sem shell; só executáveis de `[cd] allowed_commands` (python, pip, uv, pytest, node, npm, flask, gunicorn, uvicorn…); porta só da faixa configurada; ambiente sem `FLOW_*`, `*TOKEN*`, `*KEY*`, `*SECRET*`, `DATABASE_URL`; só encerra processos que o próprio servidor iniciou. Isso **não é um sandbox**: um `app.py` aprovado pode fazer o que o seu usuário faz. Revise os commits na aba Git antes de aprovar; para código não confiável use container/VM. Funciona em Windows (usa `taskkill` e `CREATE_NEW_PROCESS_GROUP`) e Linux/macOS.
**Site estático (estilo Live Server):** se a história entrega só HTML/CSS/JS (um `index.html`), **não precisa de manifesto**: o CD detecta a pasta com `index.html` e serve com `python -m http.server` em `127.0.0.1:<porta livre da faixa>` (ou declare `[run] static = true` + `port` no `flow-app.toml`). É o mesmo efeito do Live Server do VS Code (que é uma extensão do editor e não dá para acionar por script); sem recarga automática, porque cada release nova substitui a anterior.
Demo com o mock: `#app` na descrição/pedido faz o dev entregar um app de exemplo (porta 8120). *Parar* é um job: o worker dono do processo o encerra.

## Cada agente é configurável (dashboard → clique no agente, ou `PATCH /api/agents/<slug>`, ou `team.toml`)

| Campo | O que controla |
|---|---|
| `provider` / `model` / `base_url` / `temperature` | LLM por agente: `ollama`, `openai` (qualquer API compatível: LM Studio, vLLM…), `anthropic`, `mock` |
| `tools` | allowlist de ferramentas internas (+ `servidor.tool` / `servidor.*` de MCP externos em `mcp_servers`) |
| `skills_path` | pastas de skills (`;`‑separadas), cada skill = `<pasta>/<nome>/SKILL.md`; o prompt só leva nome+descrição, o agente lê o resto com `load_skill` |
| `context` | `max_tokens` (orçamento do prompt), `history_messages`, `memory_k`, `include_shared_memory`, `context_paths` (docs injetados) |
| `can_delegate_to` | com quem o agente pode falar via A2A — a **hierarquia Scrum** (PO→SM→Devs; devs↔devs e devs→PO) |
| `a2a_url` | agente **remoto**: aponte para outro servidor A2A e ele entra no time |

**Memória:** tabela `agent_memory` no mesmo banco (PostgreSQL), escopo privado (`agent`) ou do time (`shared`), recuperação por relevância+importância+recência.

## Protocolos

- **A2A 1.0** (JSON-RPC 2.0): cada agente em `/a2a/<slug>/`, Agent Card em `/a2a/<slug>/.well-known/agent-card.json`; `/.well-known/agent-card.json` é a porta de entrada do time (o PO). Métodos: `SendMessage`, `GetTask`, `ListTasks`, `CancelTask` (+ aliases 0.3). Payload estruturado viaja em `data` parts.
- **MCP**: `uv run flow-mcp` (stdio, p/ Claude Desktop) ou `uv run flow-mcp --transport http --port 9000`. Expõe as mesmas tools do registro, **sob as permissões do agente** `FLOW_MCP_AGENT` (padrão `product-owner`). Agentes também consomem servidores MCP externos (`mcp_servers`).
- Endpoint legado `/api/mcp/execute/` e as tools `ajustar_permissao_colaborador` / `consultar_auditoria` do projeto original continuam funcionando (RBAC de `domain/permissoes.py`).

## Anti-alucinação (aprendido em execução real com `gemma3:4b`)

- Cerimônias **exigem agir** (`requires`): refinamento precisa de `backlog_create`, planning de `sprint_add_item`, dev de `write_workspace_file`, QA de `work_item_verdict`, PO de `work_item_accept`. “Pronto!” em texto sem chamar a tool é rejeitado e reenviado; se persistir, a tarefa **falha visivelmente** (nunca “concluída” sem efeito). Sprint com Sprint Backlog vazio não avança.
- **`sprint_status`** (somente leitura, disponível a todo agente): estado real da sprint, itens por etapa, quem está trabalhando e onde ver os entregáveis. Os agentes são instruídos a NUNCA responder status “de cabeça”.
- **Anúncio sem execução:** modelos pequenos às vezes escrevem “Chamando work_item_accept para US-001.” sem emitir o JSON (visto em log real). Para o aceite do PO, o runner executa a chamada padrão da cerimônia **e audita como `tool_recovered`** — exceto se o texto indicar recusa (“rejeito/reprovo/recuso…”).
- **Anti-loop:** a mesma ferramenta de leitura repetida sem nada ter mudado é recusada; se a ação exigida já foi feita, a tarefa encerra com sucesso em vez de gastar todos os passos (caso do SM relistando o backlog).
- **Sem duplicatas:** `backlog_create` recusa título ≥80% igual a uma história existente, remove o prefixo `US-xxx` que o modelo inventa no título e aceita no máximo 6 critérios de aceite.
- `uv run flask --app flow seed --sync` aplica no time já criado mudanças do `team.toml` (prompts, tools, delegação) **sem** tocar em provedor/modelo/temperatura que você configurou.

## Governança e auditoria

Toda chamada de LLM, tool, mensagem A2A, mudança de configuração e cerimônia vira um `audit_event` encadeado por SHA‑256.
`uv run flask --app flow verify-audit` (ou `GET /api/audit/verify`) detecta adulteração. Travas Scrum nas tools: só o PO cria/prioriza/aceita
backlog; só aceita com aprovação do QA; quem desenvolveu não verifica o próprio item; devs só mexem nos itens atribuídos a eles.

## Limites conhecidos (leia antes de usar em produção)

- **Ollama não foi exercitado com modelo real neste ambiente** — o provedor foi testado com respostas simuladas; o fluxo completo roda de ponta a ponta com `mock`. Modelos pequenos podem errar o protocolo de tools (JSON `{"tool":…}`/`{"final":…}`); ajuste `max_steps`, `temperature` e o prompt.
- Sem streaming SSE nem push notifications A2A (o card declara `false`). Sem Alembic: o schema é criado com `create_all`; a fila usa o esquema próprio do procrastinate.
- `/api/*` só tem autenticação se `FLOW_API_TOKEN` estiver definido; `/a2a/*` só com `A2A_BEARER_TOKEN`. Os metadados A2A (`userEmail`, `workflowId`) são confiados ao chamador — exponha apenas atrás de token/proxy.
- Agentes escrevem só em `workspace/<sprint>/`; só o agente devops executa código (CI/CD local, com aprovação humana — veja acima). MCP via `command` (processo local) exige `FLOW_ALLOW_STDIO_MCP=1`.
- O mock tem botões de demo na descrição da história: `#qa-fail-once` (QA reprova uma vez → retrabalho), `#qa-fail-always` (reprovada após os retrabalhos → branch rejeitada sem merge), `#app` (entrega um app executável para o CI/CD).
- Git/CD/fila foram testados com o mock em PostgreSQL 16; **não** foram exercitados aqui com Ollama real nem no Windows (o código usa só `subprocess` sem shell e `pathlib`, com ramos próprios para Windows).

## QA com pytest em container (sandbox)

Antes do veredito, o runner roda o `pytest` da história num container descartável (`docker run --rm`): **sem rede**, raiz e pasta de trabalho somente leitura, usuário 1000 sem capabilities, 512 MB / 1 CPU / 128 processos, timeout de 120 s. O QA também pode chamar `qa_run_tests`.

- **Regras automáticas:** história só com `entrega.md` volta ao dev sem acionar o QA; código Python sem `tests/test_*.py` é reprovado (pytest exit 5); pytest vermelho reprova com a saída no feedback; o QA **não consegue aprovar** sem pytest verde na rodada (`work_item_verdict`). Entrega só HTML/CSS/JS não tem pytest: o QA lê os arquivos.
- **Dependências:** a única etapa com rede é o `uv pip install` do `requirements.txt` da história, e só pacotes de `[sandbox].allowed_packages` (URLs, `-r`, `-e`, `git+` são recusados). Pacote fora da lista bloqueia a execução (impedimento, não retrabalho) até você incluí-lo em `flow.toml`.
- **Sem Docker?** `mode = "auto"` cai para subprocesso local **sem isolamento** e o relatório diz isso. Use `mode = "docker"` para exigir o container.
- A imagem `flow-sandbox:1` (python:3.12-slim + uv + flask + pytest + httpx) é construída sozinha na 1ª execução (precisa de rede para baixar `python:3.12-slim`).
- Auditoria: evento `sandbox_run` com resumo, isolamento e saída.

### Ao vivo: o que o dev está fazendo

A coluna **Fazendo** (aba Ao vivo) e o feed classificam cada arquivo que um agente escreve: 🐍 Python (classes, funções, rotas, modelos SQLAlchemy e tabelas, framework), 🗄️ script SQL (`CREATE TABLE…`), 🗄️ migração Alembic (`create_table users`), 🌐 HTML (formulário, campos, TailwindCSS, Jinja), 📦 dependências (`requirements.txt` / `pyproject.toml`), 🧪 testes (pytest ou Playwright), 🚀 manifesto de deploy. Erro de sintaxe em `.py` aparece na hora e volta como aviso ao agente. O dev **não instala** pacotes: ele os declara em `requirements.txt` e quem instala é o sandbox, também ao vivo (`📦 instalando dependências no sandbox: …`, `🧪 rodando pytest no container…`).

### Testes: pytest + Playwright

Se a história tem código Python e o dev não trouxe testes, o QA escreve `tests/test_qa.py` (skill `qa-test-writing`) a partir dos critérios. Para telas, o teste pode usar **Playwright** (`page` do pytest-playwright) e o fixture `start_app("app.py")`, que sobe o app e devolve a URL. Em container, o Playwright usa a imagem `flow-sandbox-e2e:1` (Chromium; construída na 1ª vez que aparece um teste de navegador, ~1 GB e alguns minutos), com 1 GB / 2 CPUs / 512 processos e sem rede (o app roda dentro do próprio container, em 127.0.0.1). Sem Docker (modo local), é preciso `playwright install chromium` na sua máquina.

### Detalhe da tarefa (painel ao vivo)

Em cada agente (card da esquerda ou aba Ao vivo) há o botão **🔍 ver tarefa**. Abre o passo a passo da execução, atualizado a cada 1 s: a tarefa recebida, cada passo do modelo (com o texto que ele **está gerando agora**, em streaming no Ollama, tokens e tempo), cada ferramenta (arquivo com **nome e extensão**, escreveu/leu, tamanho, o **conteúdo escrito** e o resultado), as fases do sandbox (instalando dependências, rodando pytest) e a saída completa dos testes. À esquerda: contadores, a lista de arquivos tocados e o estado do sandbox; clique para saltar até o passo. O seletor do topo escolhe uma das últimas 25 execuções do agente. Os dados ficam na tabela `agent_trace` (PostgreSQL), então o painel funciona mesmo com o worker em outro processo. Execuções com sandbox aparecem como uma execução própria ("sandbox · testes de US-xxx") do QA.
#   0 0 1 _ a i - s c r u m - f l o w  
 