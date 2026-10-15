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

## Integração MCP

Configure o host MCP para iniciar `memory mcp`. O servidor usa `stdio` e aponta para o mesmo banco local da API e da CLI.

## Uso compartilhado

Copie `.env.shared.example` para `.env`, defina uma senha aleatória com letras, números, `_` e `-`, e inicie:

```sh
docker compose up -d --build
docker compose exec memory memory token create --owner-id alice --label alice-laptop
```

O token é mostrado uma única vez. A porta do exemplo fica restrita ao próprio host; para acesso pela rede, configure TLS e exponha a API por um proxy confiável. Crie um token para cada identidade. Para compartilhar uma coleção, use `POST /v1/memories/{id}/grants`; o grant se aplica ao escopo da memória.

## Exportação e importação

`GET /v1/export` exporta as memórias próprias e suas relações em JSON `schema_version: "1"`. `POST /v1/import` importa o arquivo, preserva IDs e relações, remapeia escopos privados para o proprietário atual e recusa IDs já existentes. Grants e tokens não são exportados.

## Configuração

`MEMORY_DATABASE_URL` aceita URL SQLite ou PostgreSQL. `MEMORY_AUTH_REQUIRED=true` exige token bearer nas chamadas HTTP. Tokens são criados com `memory token create --owner-id <id>`; o valor do token só é exibido na criação.

Políticas opcionais: `MEMORY_MAX_ENTRIES_PER_OWNER`, `MEMORY_MAX_BYTES_PER_OWNER`, `MEMORY_DEFAULT_TTL_DAYS` e `MEMORY_ALLOWED_KINDS` (lista separada por vírgulas). Limites `0` desabilitam o limite de contagem/tamanho; TTL vazio mantém memórias sem expiração padrão. `memory purge-expired` remove vencidas e resumos derivados; a API também executa essa limpeza periodicamente.

Não exponha a API em rede sem autenticação e TLS. O modo local não exige credenciais e deve permanecer vinculado a loopback.

## Estado

Implementação inicial: armazenamento, ciclo de vida, busca lexical, permissões básicas, API, CLI e servidor MCP. A sincronização offline e os embeddings não fazem parte desta versão.

## Testes

Instale as dependências de desenvolvimento e execute a suíte:

```sh
uv sync --extra dev
uv run pytest
```

## CI/CD no GitHub

O GitHub Actions roda Ruff, mypy, testes com relatório de cobertura, auditoria de vulnerabilidades das dependências e build dos pacotes em pushes, pull requests e execuções manuais, usando Python 3.13 e 3.14. Depois de um push aprovado à branch padrão, cria a tag `vMAJOR.MINOR.PATCH` definida em `pyproject.toml` se ela ainda não existir. Para uma nova tag, incremente a versão do pacote; pushes posteriores com a mesma versão não criam tags repetidas.

Também é possível criar uma release SemVer estável manualmente: abra **Actions → CI/CD → Run workflow** na branch padrão e informe `release_version` como `MAJOR.MINOR.PATCH`, por exemplo `0.2.0`. O workflow exige que a versão seja maior que a atual, atualiza `pyproject.toml`, cria o commit e a tag, e publica wheel e sdist no GitHub Release. Tags `vMAJOR.MINOR.PATCH` enviadas manualmente também podem publicar uma release, desde que coincidam com a versão em `pyproject.toml`.

O workflow requer permissão `contents: write` para criar commit/tag/release. A cobertura é reportada nos logs sem limite mínimo até estabelecermos uma linha de base. A publicação em PyPI não está configurada.
