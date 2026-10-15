# Plano técnico — Memória controlável para LLMs

- Especificação de referência: [spec.md](spec.md)
- Versão/data: 0.4 — 2026-10-15
- Estado: evolução implementada no workspace; validação técnica e de produto pendente

## Resumo da proposta

Construir um serviço de memória local-first em Python. Um único núcleo de domínio atende CLI, API REST e MCP. SQLite com FTS5 atende instalação local sem serviço externo. O mesmo contrato de persistência poderá usar PostgreSQL em implantação compartilhada. A busca inicial é lexical, explicável e barata; embeddings são uma extensão opcional posterior. Compactação e resolução de conflitos são explícitas e revisáveis.

## Estado da implementação

Código criado no workspace para pacote Python, migrações Alembic, SQLite/FTS5, API REST, CLI, MCP via `stdio`, armazenamento de versões, proveniência, busca, quotas/TTL configuráveis, grants, tokens bearer, compactação com resumo fornecido pelo operador, importação/exportação, cliente Python e configuração Docker Compose para PostgreSQL. A evolução atual adiciona sessões e handoffs estruturados, escopo contextual e embeddings opcionais compatíveis com OpenAI. Testes unitários foram escritos para regras anteriores; nenhuma validação desta evolução foi executada.

Limites conhecidos da implementação atual:

- Não há teste executado desta evolução, nem validação operacional de sessão, concorrência de handoff, migração ou busca semântica. O serviço e os critérios de aceite continuam sem evidência de execução.
- O detector de conflitos é heurístico e só emite avisos de possível duplicidade/semelhança; não determina contradições em linguagem natural. A resolução disponível é uma decisão explícita do proprietário (manter um, o outro ou ambos).
- Compactação não chama um LLM: a aplicação/operador envia o texto proposto para prévia e confirmação. Não há avaliação automática de fidelidade sem corpus de referência.
- MCP está implementado para processo local `stdio`; MCP remoto por Streamable HTTP ainda não está conectado. A API REST autenticada atende deploy compartilhado.
- PostgreSQL usa busca lexical por tokens em memória; SQLite combina ordem BM25 do FTS5 com cobertura de termos. Embeddings opt-in armazenam vetores em tabela SQL e combinam ranking semântico e lexical; não há reranker baseado em LLM.
- Quotas e tipos permitidos vêm de variáveis de ambiente globais por instância, não de políticas editáveis por coleção/usuário. A verificação de quotas ainda requer tratamento de concorrência antes de uso com alta escrita simultânea.
- Exportação inclui memórias e relações, mas não inclui grants, tokens ou histórico de versões. Isso evita transferir credenciais e compartilhamentos implicitamente.
- Há um cliente Python síncrono para gravação e recuperação de contexto via REST, além de benchmark HTTP com corpus pequeno e métricas Recall@K, MRR e latência. O benchmark ainda não foi executado; seu corpus é uma linha de base inicial, não uma avaliação de qualidade aprovada.
- Sessões registram apenas metadados; handoffs recebem resumo, decisões, bloqueios e próximos passos enviados explicitamente, sem captura automática de transcrições ou chamadas de ferramenta. O cliente deriva `project_id` do nome da raiz Git ou do diretório atual; o cliente/API precisa de contexto explícito fora desse ambiente.
- O modo semântico chama um endpoint externo apenas quando `MEMORY_EMBEDDING_URL` está configurada. Novas memórias tentam indexação no momento da gravação e memórias antigas são indexadas sob demanda durante a busca. Falha do provedor deve degradar para busca lexical.

## Contexto técnico e restrições

O workspace não tinha código, stack ou deploy existentes quando o plano foi elaborado; a implementação inicial agora está descrita acima. O serviço não depende de um provedor de LLM para gravar, buscar ou administrar memórias. A compactação semântica pode chamar um provedor configurado pelo operador; não será incluído um modelo pesado no pacote. SQLite é adequado ao modo local sem processo de banco separado; não se deve compartilhar seu arquivo diretamente por rede. A implantação multiusuário é um serviço HTTP com banco servidor.

## Arquitetura

Arquitetura de monólito modular com adaptadores nas bordas e núcleo independente de framework:

```text
Aplicação LLM / operador
       |                 |                  |
   SDK/REST             MCP                CLI
       +-----------------+------------------+
                         |
                 Casos de uso / domínio
     memória · busca · políticas · conflitos · compactação
                         |
             Portas e adaptadores (interfaces)
                |                         |
        Repositório SQL            Provedor de compactação
       SQLite local / Postgres       opcional, configurado
```

### Módulos

| Módulo | Responsabilidade | Requisitos |
| --- | --- | --- |
| `domain` | Entidades, estados, invariantes, permissões e políticas sem dependência de FastAPI/SDK | RF-002–RF-005, RF-008, RF-010, RF-011 |
| `application` | Casos de uso: salvar, buscar, corrigir, expirar, compactar, compartilhar, importar/exportar | RF-002–RF-013 |
| `ports` | Interfaces de repositório, autenticação, relógio, compactação e eventos | Separação e testabilidade |
| `adapters/sqlite` | Banco local, FTS5, migrações e armazenamento transacional | RF-001–RF-004 |
| `adapters/postgres` | Persistência multiusuário para deploy compartilhado | RF-005, RNF-003 |
| `adapters/http` | API REST versionada, autenticação e OpenAPI | RF-007, RF-011 |
| `adapters/mcp` | Ferramentas MCP para leitura/gravação e operações administrativas | RF-007 |
| `adapters/cli` | Inicialização, servidor, inspeção, exportação e configuração | RF-001–RF-006, RF-012 |
| `adapters/compaction` | Chamada a um provedor selecionado pelo operador, com saída tipada e revisável | RF-004, RF-008 |
| `observability` | Métricas operacionais sem conteúdo de memória ou credenciais | RF-013, RNF-006, RNF-007 |

### Fluxo de gravação

1. Adaptador valida formato e autenticação; o caso de uso aplica autorização e limites.
2. Normalizar e validar metadados; não inferir confiança automaticamente. Sem indicação, usar `unverified`.
3. Detectar candidatos duplicados/conflitantes no mesmo escopo. Criar como `conflict` ou devolver aviso acionável segundo política; nunca sobrescrever silenciosamente.
4. Persistir memória e proveniência em transação. Índice de busca é atualizado na mesma unidade lógica.
5. Responder com ID, estado, versão e aviso de conflito quando houver.

### Fluxo de recuperação

1. Validar o principal e filtrar por proprietário, coleção, permissões, estado e expiração antes de expor resultados.
2. SQLite FTS5 realiza busca lexical no MVP. O ranqueamento retorna pontuação relativa, termos/razão resumida, proveniência e confiança.
3. Aplicar limite de resultados e orçamento de contexto configuráveis. Embeddings e reranker ficam atrás de interfaces para etapa posterior.

### Fluxo de compactação

1. Selecionar memórias ativas dentro de um escopo e janela explicitamente configurados.
2. Gerar proposta via `CompactionProvider` configurado; sem provedor, não há compactação semântica.
3. Validar formato, fontes e tamanho; calcular bytes antes/depois e listar fontes cobertas.
4. Proprietário revisa e aplica. Criar memória derivada com links para fontes; arquivar fontes conforme política e oferecer restauração enquanto retidas.
5. Exclusão ou expiração propaga a derivados e índices conforme a política. Não registrar prompts ou conteúdo em logs.

## Stack recomendada

| Camada | Escolha para MVP | Motivo / limite |
| --- | --- | --- |
| Linguagem | Python 3.13+; CI inicial em 3.13 e 3.14 | Ecossistema de integração LLM, distribuição multiplataforma e biblioteca `sqlite3` disponível no runtime. 3.13 recebe correções de segurança até 2029; adicionar novas versões após validar dependências. |
| Framework HTTP | FastAPI | API tipada com contrato OpenAPI; camada externa, sem lógica de domínio nos endpoints. |
| Modelos de entrada/saída | Pydantic v2 | Validar payloads e manter contratos explícitos; tipos públicos não espelham diretamente tabelas internas. |
| Banco local | SQLite 3 com FTS5 | Sem daemon separado; arquivo local e busca lexical. Validar FTS5 no ambiente de build e falhar com mensagem clara se ausente. |
| Banco compartilhado | PostgreSQL 17+; validar também PostgreSQL 18 | Banco servidor para transações e uso concorrente entre clientes; FTS inicial por busca textual PostgreSQL. Não expor arquivo SQLite em rede. |
| Camada SQL | SQLAlchemy 2 + Alembic | Repositórios atrás de interfaces e migrações reproduzíveis para os dois modos. Usar subconjunto SQL comum; manter diferenças de FTS nos adaptadores. |
| CLI / distribuição | Typer + pacote Python instalado via `uv` ou `pipx` | Um comando para instalar/inicializar e um entry point `memory`; não exigir Docker no modo local. |
| Integração agente | MCP Python SDK, além de REST | `stdio` para servidor local; Streamable HTTP para deploy. Separar ferramentas de leitura das operações destrutivas/administrativas. |
| Empacotamento compartilhado | Docker Compose com API e PostgreSQL | Onboarding de equipe em um comando, dados em volume persistente e configuração por variáveis/arquivo local. |
| Busca semântica | Endpoint OpenAI-compatible opcional e armazenamento vetorial SQL; lexical permanece padrão | Evitar download de modelo e dependência de serviço para a instalação mínima; custo e latência adicionais apenas quando explicitamente configurados. |
| Testes e qualidade | pytest, Ruff, mypy; testes de contrato para os adaptadores | Cobrir regras do domínio, isolamento de permissões e equivalência de comportamento relevante entre SQLite e PostgreSQL. |

Versões acima são baseline proposta, não pins finais. O lockfile e CI devem fixar versões exatas no início da implementação; atualizar os limites após prova de compatibilidade nos sistemas-alvo. Não há interface gráfica no MVP.

## Modelo de dados e persistência

Entidades principais:

- `Memory`: `id` UUID, `owner_id`, `scope_id`, `content`, `kind`, `confidence`, `source_type`, `source_ref`, `status`, `created_at`, `updated_at`, `expires_at`, `version`.
- `MemoryRelation`: `source_memory_id`, `derived_memory_id`, relação (`summarized_from`, `supersedes`, `duplicates`, `conflicts_with`) e data.
- `MemoryVersion`: versão, conteúdo anterior, autor e data; retenção configurável; sem conteúdo mantido após exclusão final.
- `Grant`: escopo/destinatário, papel, validade, revogação e autor da concessão.
- `Policy`: limites de tamanho/contagem, TTL padrão, categorias permitidas e política de derivados.
- `ApiToken`: identificador e hash do segredo, owner, escopos, criação e revogação. Token em texto claro só aparece na criação.
- `AgentSession`: proprietário, identificador externo opcional, workspace/projeto/agente, estado e timestamps; não armazena transcrição.
- `Handoff`: sessão de origem, escopo, agente de origem/destino, resumo estruturado, estado pendente/reivindicado/concluído e agente que reivindicou.
- `MemoryEmbedding`: memória, identidade derivada de modelo/endpoint e vetor serializado; sem conteúdo de credencial do endpoint.

Invariantes: toda memória pertence a exatamente um proprietário e escopo; toda derivação tem pelo menos uma fonte ou registra `source_type=manual`; buscas excluem expiradas/deletadas e aplicam autorização antes de ranquear; versão impede atualizações concorrentes silenciosas; exclusão remove dados de todos os índices síncronos.

SQLite deve usar transações, foreign keys e índices explícitos. Acesso local é um processo servidor por arquivo; não suportar múltiplos processos escrevendo por compartilhamento em rede. PostgreSQL é a implantação de equipe. Migrações têm backup prévio e comando de downgrade apenas quando seguro; exportação JSON versionada é o caminho de migração entre instâncias.

## Contratos externos

### REST `/v1`

- `POST /memories`, `GET /memories/{id}`, `PATCH /memories/{id}`, `DELETE /memories/{id}`
- `POST /search`
- `POST /sessions`, `POST /sessions/{id}/handoff`, `POST /handoffs/claim-next`, `POST /handoffs/{id}/complete`
- `POST /compactions/preview`, `POST /compactions/{id}/apply`
- `POST /memories/{id}/resolve-conflict`, `GET /memories/{id}/history`
- `POST /grants`, `DELETE /grants/{id}`
- `GET /export`, `POST /import`
- Erros: `400` entrada malformada, `401` credencial ausente/inválida, `403` acesso negado, `404` fora de escopo/inexistente, `409` conflito de versão/estado, `413` limite excedido, `422` validação semântica.
- Paginação por cursor; limite máximo documentado; `If-Match`/versão em atualizações concorrentes; exportação inclui `schema_version`.

### MCP

Ferramentas incluem `memory_search`, `memory_get`, `memory_create`, `memory_update`, `memory_session_start`, `memory_session_handoff`, `memory_handoff_claim` e `memory_handoff_complete`, além das operações administrativas atuais. Captura de transcrições fica fora desta etapa. Aplicar compactação, compartilhar, importar/exportar e apagar em lote ficam protegidos por ferramentas administrativas explícitas e devem exigir confirmação do operador no cliente ou política equivalente. Em `stdio`, credenciais vêm de configuração local e o processo atende um usuário. Em Streamable HTTP, aplicar auth e autorização iguais às rotas REST.

### CLI

Comandos conceituais: `memory init`, `memory serve`, `memory mcp`, `memory inspect`, `memory policy`, `memory export`, `memory import`, `memory compact preview` e `memory compact apply`. Nunca imprimir tokens ou conteúdo completo sem pedido específico. `init` cria diretório com permissões restritas e configuração de exemplo.

## Decisões técnicas

| ID | Decisão | Alternativas consideradas | Motivo e impacto | Estado |
| --- | --- | --- | --- | --- |
| DEC-001 | Python 3.13+ e monólito modular | Go binário único; TypeScript | Python reduz atrito de integração com ecossistema LLM; tem maior footprint que binário Go | Proposta recomendada |
| DEC-002 | SQLite/FTS5 para local e PostgreSQL para equipes | SQLite compartilhado em rede; PostgreSQL obrigatório local | Instalação local sem serviço; uso compartilhado confiável via DB servidor | Proposta recomendada |
| DEC-003 | FastAPI + Pydantic; núcleo sem dependência do framework | Framework full-stack; biblioteca apenas | API HTTP e validação claras com separação de domínio | Proposta recomendada |
| DEC-004 | REST e MCP como portas da mesma aplicação | Só MCP; SDK acoplado a um framework de agentes | Abrange clientes genéricos e hosts compatíveis com MCP sem duplicar regras | Proposta recomendada |
| DEC-005 | Busca lexical explicável no MVP; vetores como extensão | Embeddings obrigatórios | Menor instalação e custo; busca semântica fica limitada inicialmente | Proposta recomendada |
| DEC-006 | Compactação sempre revisável no MVP | Compactação autônoma | Preserva controle e permite validar qualidade antes de arquivar fontes | Proposta recomendada |
| DEC-007 | Busca e operações respeitam auth em todos os adaptadores | Filtrar apenas na interface | Evita vazamento por endpoint alternativo ou ferramenta MCP | Requisito de segurança |
| DEC-008 | Configuração por arquivo local/env; segredos armazenados como hash quando verificáveis | Segredos em banco em texto claro | Reduz exposição acidental e simplifica deploy | Proposta recomendada |
| DEC-009 | Sem UI gráfica no MVP | Painel web imediato | Foco em API/CLI/MCP e custo menor; pode ser reconsiderado após feedback | Proposta; confirmar prioridade |

## Instalação e implantação

### Local

1. Instalar pacote pelo gerenciador Python selecionado (`uv tool install` como caminho documentado; `pipx` como alternativa).
2. Executar `memory init`; escolher diretório de dados e criar config com defaults locais.
3. Executar `memory serve` para API ou `memory mcp` para integração por stdio.
4. A API local escuta somente `127.0.0.1` por padrão. Exposição em rede exige configuração explícita de autenticação.

### Compartilhado

1. `docker compose up -d` inicia API e PostgreSQL com volume persistente.
2. Operador gera token inicial, define segredo de assinatura/criptografia e cria usuários/coleções.
3. Publicação externa requer TLS via proxy ou ingress, backups testados, limites de payload e rotação/revogação de tokens.
4. O banco não fica exposto publicamente; somente a API atende clientes.

### Backup e recuperação

Exportação JSON versionada por escopo e backup consistente do banco. Incluir instruções de restauração e teste periódico de restore. Backup criptografado é obrigatório no modo compartilhado; retenção de backups deve respeitar a política declarada e não pode ser apresentada como exclusão imediata.

## Segurança e privacidade

- Local: diretório de dados com acesso restrito ao usuário do sistema; bind loopback por padrão.
- Compartilhado: autenticação em toda requisição, autorização por owner/scope/role, TLS, tokens revogáveis guardados como hash e limites contra abuso.
- Não registrar conteúdo, prompts, tokens, `source_ref` sensível nem payloads de erro. Logs usam request ID, ação, resultado e identificadores não sensíveis.
- Limites de tamanho e contagem evitam gravações ilimitadas. Todas as ferramentas MCP são tratadas como entrada não confiável e aplicam validação/autorização do domínio.
- Remoção percorre conteúdo, versões, FTS, embeddings futuros e derivados; backups seguem janela de retenção divulgada.
- Não enviar memória a provedor externo sem configuração explícita e indicação do provedor usado na prévia da operação.

## Estratégia de validação

Não há evidência de testes executados; esta seção define verificações para implementação futura.

| Requisitos / critérios | Verificação prevista | Resultado esperado |
| --- | --- | --- |
| RF-001 / CA-001, RNF-001 | Instalar em ambiente limpo em Linux/macOS/Windows suportados; cronometrar | Exemplo local funcional em até 5 minutos, sem conta externa |
| RF-002, RF-003 / CA-002 | Testes do domínio e persistência: CRUD, expiração, limites e remoção de derivados | Invariantes aplicadas e sem descarte silencioso |
| RF-004 / CA-003, RNF-002 | Corpus com consultas antes/depois; comparar bytes e Recall@5 | Prévia mostra fontes e redução; compactação não ultrapassa limiar aprovado |
| RF-005, RF-011 / CA-004, CA-008, RNF-003 | Testes de isolamento via REST e MCP com owner/editor/reader, grants expirados e revogados | Nenhum acesso indevido; revogação aplicada em novas chamadas |
| RF-006 / CA-005 | Exportar e reimportar dados sintéticos e validar `schema_version` e relações | Round-trip preserva dados permitidos e rejeita versão incompatível com erro claro |
| RF-007 / CA-005 | Cliente exemplo para REST e host MCP local | Ambas interfaces executam busca e CRUD autorizado |
| RF-008, RF-009, RF-012 / CA-006 | Casos fornecido/inferido/não confirmado, fonte ausente, memória alterada | Proveniência e motivo de busca visíveis; correção atualiza recuperação |
| RF-010 / CA-007 | Dados com duplicatas, valores conflitantes e atualização concorrente | Nenhuma sobrescrita silenciosa; resolução cria versão e vínculo correto |
| RNF-004 | Benchmark 10 mil memórias, 2 KB cada, hardware documentado | Busca lexical p95 ≤ 250 ms local |
| RNF-006, RNF-007 / CA-008 | Inspeção de logs, métricas, delete e restauração de backup | Sem conteúdo/segredos em logs; métricas úteis; limites de backup documentados |

## Sequência de implementação

1. Revisar as decisões Q-001–Q-010 com o proprietário do produto e congelar contratos `/v1`, política de dados e comportamento de expiração.
2. Executar instalação local em ambiente limpo e corrigir incompatibilidades da stack/FTS5.
3. Validar CRUD, busca, quotas, histórico, exportação/importação e exclusão de derivados no SQLite.
4. Validar isolamento de tokens e grants, revogação e ações destrutivas via REST e MCP local.
5. Validar migrações, Compose, PostgreSQL e uso concorrente no modo compartilhado.
6. Ampliar o corpus de benchmark versionado além do smoke corpus inicial; medir latência, Recall@5, MRR, custo/footprint e compactação antes de aprovar metas.
7. Completar geração opcional de resumo via provedor configurável e decidir se/como habilitar MCP remoto.
8. Fazer hardening e documentar backup/restore antes de chamar o serviço pronto para uso compartilhado em produção.

Etapas 2–6 podem avançar sem deploy multiusuário. Etapa 8 depende da interface de repositório testada e dos requisitos de identidade/compartilhamento definidos. Integração de embeddings depende de avaliação da busca lexical e de uma decisão explícita sobre footprint/custo.

## Riscos e decisões pendentes

| ID | Pergunta/risco | Default proposto | Efeito se mudar |
| --- | --- | --- | --- |
| Q-001 | Compacta significa footprint ou resumo semântico? | Ambos: FTS sem vetor obrigatório e compactação revisável | Escolha de algoritmo, armazenamento e métricas |
| Q-002 | Compartilhamento entre quem? | Mesmo usuário local no modo local; equipes via serviço/Postgres | Identidade e sincronização |
| Q-003 | Local ou hospedado? | Local primeiro, deploy compartilhado self-hosted no MVP | Empacotamento e operação |
| Q-004 | Integração inicial? | CLI + REST + MCP | Ordem e superfície de manutenção |
| Q-005 | Dados sensíveis, residência e criptografia? | Não enviar para fora; TLS no modo remoto; encryption-at-rest pela infraestrutura até decisão de criptografia por aplicação | Modelo de ameaça e garantias de segurança |
| Q-006 | Usuário inicial? | Desenvolvedor solo e pequenas equipes técnicas | UX, autenticação e suporte |
| Q-007 | Qualidade de busca? | Recall@5 ≥ 0,8 em corpus de referência; corpus inicial versionado ainda é pequeno | Aceite do retrieval |
| Q-008 | Confiança/proveniência? | Rótulos `asserted`, `inferred`, `unverified`; origem opcional tipada | Esquema e ergonomia de ingestão |
| Q-009 | Histórico e exclusão? | Versionamento enquanto ativo; exclusão propaga para versões/derivados; backups expiram conforme política divulgada | Auditoria e recuperação |
| Q-010 | Métricas de custo? | Footprint e latência sempre; custo externo só quando provedor usado | Observabilidade e cobrança |

## Referências técnicas consultadas

- FastAPI, documentação oficial consultada via Context7 em 2026-10-15: organização de aplicações maiores e deployment em contêineres. Fonte: [FastAPI Bigger Applications](https://fastapi.tiangolo.com/tutorial/bigger-applications/), [FastAPI Docker Deployment](https://fastapi.tiangolo.com/deployment/docker/).
- MCP Python SDK, documentação oficial consultada via Context7 em 2026-10-15: `stdio` para servidor local e Streamable HTTP para deploy; evitar SSE em novas integrações. Fonte: [Python SDK — Running your server](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/index.md).
- Python `sqlite3`, documentação CPython consultada via Context7 em 2026-10-15: SQLite é uma biblioteca embutida sem processo servidor separado; a documentação também descreve a interface `sqlite3`. Fonte: [Python sqlite3](https://docs.python.org/3/library/sqlite3.html).
- Versões exatas das dependências Python, recomendações de implantação e disponibilidade de FTS5 devem ser confirmadas e fixadas na implementação/CI para cada plataforma suportada. Python.org mantém 3.13 e 3.14 em suporte; o PostgreSQL lista 17 e 18 como versões suportadas em outubro de 2026.
- Ciclo de suporte consultado em 2026-10-15: [versões suportadas do Python](https://www.python.org/downloads/) e [política de versões do PostgreSQL](https://www.postgresql.org/support/versioning/).
