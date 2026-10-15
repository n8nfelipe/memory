# LLM Memory

Serviço local-first para guardar, buscar, revisar, compactar e compartilhar memórias usadas por aplicações com LLMs. O modo local usa SQLite; o modo compartilhado pode usar PostgreSQL.

## Instalação para desenvolvimento

Requer Python 3.13 ou superior.

```sh
uv tool install .
memory init
memory serve
```

A API local escuta `127.0.0.1:8765`. A documentação OpenAPI fica em `/docs`.

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

O cliente resolve automaticamente o nome do projeto pelo repositório Git atual (ou pelo diretório atual fora de um repositório). `start_session` cria uma sessão; `create_handoff` a encerra e guarda resumo, decisões, bloqueios e próximos passos. Outro agente do mesmo projeto pode chamar `start_session` e `claim_next_handoff` para receber o handoff pendente. O serviço guarda apenas os campos estruturados enviados; não captura transcrições ou chamadas de ferramenta.

`remember` grava uma memória explícita. `context` busca resultados e limita o texto para controlar o orçamento de contexto. Configure `base_url` para apontar para uma instância remota autenticada.

## Integração MCP

Configure o host MCP para iniciar `memory mcp`. O servidor usa `stdio` e aponta para o mesmo banco local da API e da CLI.

Ferramentas de leitura: `memory_search`, `memory_get`, `memory_history`, `memory_compaction_preview`. Escrita: `memory_create`, `memory_update`, `memory_session_start`, `memory_session_handoff`. `memory_handoff_claim` reivindica atomicamente o handoff mais antigo do projeto; `memory_handoff_complete` encerra o handoff reivindicado. Administrativas (exigem `confirm=true`): `memory_delete`, `memory_compaction_apply`, `memory_share`, `memory_resolve_conflict`.

## Uso compartilhado

Copie `.env.shared.example` para `.env`, defina uma senha aleatória com letras, números, `_` e `-`, e inicie:

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

## Configuração

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
