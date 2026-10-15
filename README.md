# LLM Memory

Serviço local-first para guardar, buscar, revisar, compactar e compartilhar memórias usadas por aplicações com LLMs. O modo local usa um repositório Git com **um arquivo Markdown por sessão**, sem banco nem índice persistente. O backend legado SQLite/PostgreSQL continua disponível para migração e uso compartilhado.

## Instalação para desenvolvimento

Requer Python 3.13 ou superior, Git e Linux/macOS (bloqueio local via `flock`).

```sh
uv tool install .
memory init
memory serve
```

A API local escuta `127.0.0.1:8765`. A documentação OpenAPI fica em `/docs`.

## Repositório de sessões configurável

Neste projeto, `memory.toml` seleciona Markdown e aponta para `data/sessao-memory`.
Esse diretório é um clone separado, ignorado pelo Git da aplicação. O remoto escolhido
fica no `origin` do clone; nenhuma URL é fixa no código.

```toml
[storage]
backend = "markdown"
repository = "data/sessao-memory"
```

O caminho relativo é resolvido a partir de `memory.toml`. Para executar de outro
 diretório, use `MEMORY_CONFIG=/caminho/absoluto/memory.toml`. Variáveis de ambiente
 têm precedência:

```sh
export MEMORY_STORAGE_BACKEND=markdown
export MEMORY_REPOSITORY=/caminho/para/sessao-memory
# Para um destino ainda vazio:
memory init --remote git@github.com:n8nfelipe/sessao-memory.git
# Para um clone existente ou um repositório local novo:
memory init
```

Sem configuração, o destino é `~/.llm-memory/sessions`. Uma URL de banco explicitada
por `MEMORY_DATABASE_URL` seleciona o backend legado apenas quando não há seleção
explícita em `memory.toml` ou `MEMORY_STORAGE_BACKEND`. Arquivos `.env` são modelos;
a CLI não os carrega automaticamente.

```text
sessao-memory/
└── sessions/
    ├── <uuid-da-sessao-a>.md
    └── <uuid-da-sessao-b>.md
```

`memory_session_start` define a sessão atual da conexão MCP. Passe `session_id`
explicitamente ao intercalar agentes. Chamadas sem sessão criam uma sessão implícita;
na API HTTP, reutilize o ID retornado para agrupar as gravações. Correções, exclusões,
compactações e reivindicações de handoff são eventos no arquivo da sessão que escreve;
o arquivo de uma sessão encerrada não é reaberto. Os metadados ficam em comentários
HTML e os textos ficam em Markdown legível. Preserve os marcadores ao editar arquivos.

A busca lexical lê as sessões e seus metadados, com filtros de projeto e workspace.
Embeddings, tokens e grants por usuário continuam exclusivos do backend de banco.
O modo Markdown é local: todos que leem o repositório conseguem ler as sessões.
A exclusão e a expiração ocultam registros da busca, mas **não apagam o conteúdo dos
arquivos nem do histórico Git**. `purge-expired` registra essas exclusões lógicas.

Gravações usam substituição atômica e um bloqueio no diretório Git. O bloqueio coordena
processos no mesmo checkout; não coordena clones em máquinas diferentes. Revisões
concorrentes da mesma memória e claims concorrentes de handoffs são detectados ao
reler os arquivos e exigem revisão do merge. Não execute pull/merge enquanto houver
agentes escrevendo. Evite compartilhar a escrita de uma mesma sessão entre agentes.

Commits e push são explícitos. O comando abaixo inclui somente o arquivo indicado,
sem levar alterações de outras sessões ou arquivos já staged:

```sh
memory commit --session-id UUID --message "Concluir sessão"
git -C /caminho/para/sessao-memory push origin HEAD
```

Configure a identidade Git normalmente antes do primeiro commit. A aplicação não
faz push, pull ou commits automáticos.

### Migrar o banco existente

```sh
memory migrate-database --database-url sqlite:////caminho/absoluto/memory.db
memory inspect
```

A migração lê o banco sem alterá-lo e preserva IDs, sessões, memórias, versões,
relações, propostas de compactação e handoffs do proprietário local. Registros antigos
sem sessão recebem um arquivo de sessão legado. Tokens, grants e embeddings não são
copiados. Reexecutar a migração não duplica arquivos; sessões já alteradas no destino
são recusadas. Mantenha os escritores do banco parados durante a migração. O comando
não publica os dados no remoto. O JSON de `memory export` continua disponível; importar
esse JSON cria um diário de importação, enquanto `migrate-database` preserva a divisão
original em sessões.

## Uso rápido

```sh
curl -X POST http://127.0.0.1:8765/v1/memories \
  -H 'content-type: application/json' \
  -d '{"content":"Prefere respostas em português.","kind":"preference","source":{"type":"user"},"confidence":"asserted"}'

curl -X POST http://127.0.0.1:8765/v1/search \
  -H 'content-type: application/json' \
  -d '{"query":"idioma de resposta"}'
```

## Integração Python para agentes

O cliente Python usa a API REST existente e não depende de um framework de agentes:

```python
from llm_memory.client import MemoryClient

memory = MemoryClient(token="...")  # omita token no modo local sem autenticação
session = memory.start_session("codex", title="Adicionar busca semântica")
memory.remember(
    "Prefere respostas em português.",
    kind="preference",
    confidence="asserted",
    source_type="conversation",
    source_ref="session-123",
)

# Inclua o contexto recuperado nas instruções da próxima chamada ao seu LLM.
context = memory.context("idioma preferido", limit=5, max_chars=4_000)
handoff = memory.create_handoff(
    "Busca semântica implementada; falta validar com corpus maior.",
    decisions=["Embeddings são opt-in"],
    next_steps=["Executar o benchmark de recuperação"],
)
```

O cliente resolve automaticamente o nome do projeto pelo repositório Git atual (ou pelo diretório atual fora de um repositório). `start_session` cria uma sessão; `create_handoff` a encerra e guarda resumo, decisões, bloqueios e próximos passos. Outro agente do mesmo projeto pode chamar `start_session` e `claim_next_handoff` para receber o handoff pendente. As ferramentas MCP não capturam transcrições ou chamadas de ferramenta por padrão.

`remember` grava uma memória explícita. `context` busca resultados e limita o texto para controlar o orçamento de contexto. Configure `base_url` para apontar para uma instância remota autenticada.

### Captura automática de conversas do Codex

O arquivo `.codex/hooks.json` configura hooks locais para iniciar uma sessão,
registrar cada prompt e resposta final e guardar um handoff ao encerrar. No backend
Markdown, cada turno é persistido imediatamente no arquivo da sessão, e retries com
o mesmo `turn_id` não duplicam mensagens. A captura não inclui chamadas ou resultados
de ferramentas. O backend de banco mantém o comportamento anterior com spool temporário.

Os hooks respeitam `memory.toml`, `MEMORY_CONFIG` e `MEMORY_REPOSITORY`. Revise e confie
os hooks no Codex quando solicitado. A captura guarda o texto da conversa; ela não faz
commit ou push automático.

### Captura automática de conversas do Opencode

O plugin `.opencode/plugins/llm-memory-auto.js` (também instalado globalmente em
`~/.config/opencode/plugins/`) escuta `session.created`, `session.idle` e
`session.deleted` e chama `memory capture-opencode --input <json>` com a transcrição
em texto já resolvida via SDK. Cada turno `user`/`assistant` vira uma memória
`conversation` com `source_type: "opencode_hook"`; chamadas de ferramenta são
ignoradas. `session.deleted` registra o handoff e encerra a sessão. Reinicie o
Opencode após instalar/atualizar o plugin, pois plugins são carregados no startup.

## Integração MCP

Configure o host MCP para iniciar `memory mcp`. O servidor usa `stdio` e o mesmo backend configurado para a API e a CLI.

Ferramentas de leitura: `memory_search`, `memory_get`, `memory_history`, `memory_compaction_preview`. Escrita: `memory_create`, `memory_update`, `memory_session_start`, `memory_session_handoff`. `memory_handoff_claim` reivindica atomicamente no mesmo checkout o handoff mais antigo do projeto; `memory_handoff_complete` encerra o handoff reivindicado. Administrativas (exigem `confirm=true`): `memory_delete`, `memory_compaction_apply`, `memory_share` (somente banco), `memory_resolve_conflict`. No backend Markdown, `memory_compaction_preview` registra a proposta no diário sem arquivar as fontes.

## Uso compartilhado

Para o backend de banco, configure `MEMORY_STORAGE_BACKEND=database`. Copie `.env.shared.example` para `.env`, defina uma senha aleatória com letras, números, `_` e `-`, e inicie:

```sh
docker compose up -d --build
docker compose exec memory memory token create --owner-id alice --label alice-laptop
```

O token é mostrado uma única vez. A porta do exemplo fica restrita ao próprio host; para acesso pela rede, configure TLS e exponha a API por um proxy confiável. Crie um token para cada identidade. Para compartilhar uma coleção, use `POST /v1/memories/{id}/grants`; o grant se aplica ao escopo da memória.

## Exportação e importação

`GET /v1/export` exporta as memórias próprias e suas relações em JSON `schema_version: "1"`. `POST /v1/import` importa o arquivo, preserva IDs e relações, remapeia escopos privados para o proprietário atual e recusa IDs já existentes. Grants e tokens não são exportados.

CLI equivalente (dono local): `memory export --output backup.json`, `memory import --input backup.json`, `memory policy`, `memory compact-preview --memory-id ID --memory-id ID --content "..."`, `memory compact-apply --proposal-id ID`.

## Métricas

`GET /v1/metrics` retorna contagens por status, bytes totais, grants ativos e compactações pendentes, sem expor conteúdo.

## Configuração do backend de banco

`MEMORY_DATABASE_URL` aceita URL SQLite ou PostgreSQL. `MEMORY_AUTH_REQUIRED=true` exige token bearer nas chamadas HTTP. Tokens são criados com `memory token create --owner-id <id>`; o valor do token só é exibido na criação.

Políticas opcionais: `MEMORY_MAX_ENTRIES_PER_OWNER`, `MEMORY_MAX_BYTES_PER_OWNER`, `MEMORY_DEFAULT_TTL_DAYS` e `MEMORY_ALLOWED_KINDS` (lista separada por vírgulas). Limites `0` desabilitam o limite de contagem/tamanho; TTL vazio mantém memórias sem expiração padrão. Para busca vetorial opcional, configure `MEMORY_EMBEDDING_URL`, `MEMORY_EMBEDDING_MODEL` e, quando exigido pelo endpoint, `MEMORY_EMBEDDING_API_KEY`. O conteúdo e a consulta são enviados a esse endpoint; deixe a URL vazia para manter o processamento local. `memory purge-expired` remove vencidas e resumos derivados; a API também executa essa limpeza periodicamente.

Não exponha a API em rede sem autenticação e TLS. O modo local não exige credenciais e deve permanecer vinculado a loopback.

## Estado

Implementação: armazenamento, ciclo de vida, busca lexical, sessões e handoffs estruturados, escopo contextual por workspace/projeto/agente/sessão, permissões básicas, API, cliente Python, CLI e servidor MCP. A busca semântica é opcional e requer configurar `MEMORY_EMBEDDING_URL`; sem essa configuração, nenhum conteúdo é enviado a um provedor. Extração automática de memórias e sincronização offline não fazem parte desta versão.

Para ativar embeddings com um endpoint compatível com OpenAI, configure `MEMORY_EMBEDDING_URL` (por exemplo, `https://api.openai.com/v1/embeddings`), `MEMORY_EMBEDDING_MODEL` e `MEMORY_EMBEDDING_API_KEY`. O índice é preenchido ao gravar novas memórias e sob demanda na busca para memórias existentes. A busca combina os rankings lexical e vetorial; se o endpoint falhar, usa a busca lexical.

## Benchmark de recuperação

Com a API iniciada, execute o corpus fixo em português. O script cria e remove as memórias de benchmark em um escopo isolado e informa Recall@K, MRR e latência:

```sh
uv run python benchmarks/retrieval.py --k 5
```

Para uma instância autenticada, use `--url` e `--token` ou defina `MEMORY_API_URL` e `MEMORY_API_TOKEN`.

## Testes

Instale as dependências de desenvolvimento e execute a suíte:

```sh
uv sync --extra dev
uv run pytest
```

## CI/CD no GitHub

O GitHub Actions roda Ruff, mypy, testes com relatório de cobertura, auditoria de vulnerabilidades das dependências e build dos pacotes em pushes, pull requests e execuções manuais, usando Python 3.13 e 3.14. Após cada push aprovado à branch padrão, gera automaticamente a próxima tag `vMAJOR.MINOR.PATCH`, incrementando o patch da última tag existente (ou começando em `v0.1.0`), e cria um GitHub Release com os pacotes e notas geradas. A versão da tag vem do histórico de tags Git, sem ler ou atualizar um arquivo de versão. Tags SemVer enviadas manualmente também podem publicar uma release.

O workflow requer permissão `contents: write` para criar commit/tag/release. A cobertura é reportada nos logs sem limite mínimo até estabelecermos uma linha de base. A publicação em PyPI não está configurada.
