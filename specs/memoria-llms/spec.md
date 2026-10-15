# Especificação — Memória controlável para LLMs

- Estado: rascunho
- Versão/data: 0.3 — 2026-10-15
- Fontes: pedido do usuário: “vamos planejar uma memoria para llms e de facil instalação, que controle bem as memoria, compacta, pode ser compartilhada”
- Aceite: pendente; nenhuma decisão de produto foi confirmada além dos atributos do pedido

## Problema e resultado esperado

Aplicações com LLMs precisam guardar contexto útil entre interações sem acumular conteúdo irrelevante, perder controle sobre o que foi retido, nem exigir uma instalação difícil. O produto deve permitir criar, consultar, revisar, compactar, apagar e compartilhar memórias de forma previsível.

## Escopo

- Incluído: armazenamento de memória para uso por LLMs; instalação simples; controles explícitos de retenção e remoção; compactação; compartilhamento com escopo e permissões configuráveis.
- Fora do escopo inicial: treinamento ou ajuste de modelos; busca na web; gestão de conversas como interface de chat; escolha de provedor de LLM.
- Restrições confirmadas: deve atender LLMs, ser fácil de instalar, controlar bem as memórias, ser compacta e permitir compartilhamento. Origem: pedido do usuário.
- Dependências: definição do público inicial, forma de integração e modelo de compartilhamento.

## Atores, fluxos e regras

- Desenvolvedor instala a ferramenta e conecta uma aplicação ou agente.
- Aplicação grava uma memória com conteúdo e metadados, consulta memórias relevantes e pode atualizar ou remover registros.
- Proprietário inspeciona o que foi guardado, altera sua visibilidade e revoga compartilhamentos.
- Regra proposta: memória só é compartilhada quando o proprietário concede acesso; escopo e permissões devem ser explícitos.
- Regra proposta: compactação não deve apagar silenciosamente fatos sem permitir inspeção ou recuperação conforme política definida.

## Requisitos funcionais

| ID | Comportamento esperado | Prioridade | Origem |
| --- | --- | --- | --- |
| RF-001 | Permitir instalar e iniciar o serviço ou biblioteca por um fluxo documentado e curto. | Alta | Pedido do usuário |
| RF-002 | Permitir criar, listar, consultar, editar e apagar memórias, com metadados de origem, data, proprietário, escopo e estado de confiança. | Alta | “controle bem a memória” — interpretação inicial e recomendação |
| RF-003 | Permitir definir limites de armazenamento e políticas de retenção e expiração, incluindo como tratar memórias derivadas, com comportamento observável ao atingir os limites. | Alta | “controle bem a memória” — proposta e recomendação |
| RF-004 | Permitir compactar memórias redundantes ou antigas, preservando ligação com as fontes e apresentando alterações antes de aplicá-las. | Alta | “compacta” — interpretação como compactação de conteúdo; confirmar |
| RF-005 | Permitir compartilhar uma memória ou conjunto de memórias com pessoas, aplicações ou agentes autorizados, definindo permissões e revogando acesso. | Alta | “pode ser compartilhada” — interpretação inicial |
| RF-006 | Permitir exportar e importar dados em formato documentado, incluindo metadados, relações de origem e permissões quando aplicável. | Média | Proposta para portabilidade e controle |
| RF-007 | Oferecer interface de integração estável para aplicações de LLM, com operações de gravação, busca e manutenção. | Alta | Uso para LLMs — proposta |
| RF-008 | Registrar a proveniência de cada memória e distinguir fatos fornecidos, inferências e conteúdo não confirmado; permitir consultar essa origem. | Alta | Recomendação do assistente |
| RF-009 | Apresentar resultados de busca com sinais de relevância e contexto suficiente para que a aplicação consumidora selecione memórias adequadas à consulta. | Alta | Recomendação do assistente |
| RF-010 | Detectar possíveis duplicatas e contradições ao adicionar ou atualizar memórias, permitir resolver o conflito e preservar histórico conforme política definida. | Alta | Recomendação do assistente |
| RF-011 | Permitir definir categorias de dados permitidas e políticas de proteção; restringir cada operação ao proprietário e às permissões concedidas. | Alta | Recomendação do assistente |
| RF-012 | Permitir inspecionar por que uma memória foi recuperada, corrigir o registro e consultar seu histórico de alterações. | Alta | Recomendação do assistente |
| RF-013 | Expor métricas de uso, incluindo espaço ocupado e, quando aplicável, latência e custo de operações de busca e compactação. | Média | Recomendação do assistente |

## Dados, estados e interfaces

### Modelo de memória

Cada memória contém: `id`, `content`, `kind`, `owner_id`, `scope_id`, `source`, `confidence`, `tags`, `created_at`, `updated_at`, `expires_at`, `status` e `version`. `source` identifica tipo e referência de origem, sem exigir guardar a conversa inteira. `confidence` usa rótulos (`asserted`, `inferred`, `unverified`) e não uma probabilidade inventada. Memórias derivadas guardam referências às origens e à operação que as gerou.

Estados propostos: `active`, `archived`, `conflict` e `deleted`. Compactação cria uma proposta revisável; após aplicação, as fontes ficam arquivadas/restauráveis conforme a retenção. Exclusão remove conteúdo, versões, índices e derivados identificáveis; pode manter um tombstone mínimo sem conteúdo para impedir reimportação acidental, sujeito à política de retenção.

### Permissões

- Escopos: privado por padrão; coleção/grupo explicitamente compartilhado.
- Papéis mínimos: `owner` (gerenciar e compartilhar), `editor` (criar e editar) e `reader` (consultar).
- Toda operação de leitura, busca, edição, exportação e exclusão aplica a autorização no servidor. Compartilhamentos são revogáveis e podem ter validade.
- Modo local: serviço vinculado a loopback, sem autenticação de rede. Modo compartilhado: autenticação por tokens revogáveis no MVP; OIDC fica para etapa posterior.

### Interfaces de integração

- API REST versionada em `/v1`; operações conceituais: criar/listar/obter/atualizar/apagar memória, buscar, pré-visualizar/aplicar compactação, revisar conflitos, consultar histórico, compartilhar/revogar e importar/exportar.
- MCP: ferramentas para `memory_search`, `memory_get`, `memory_create` e operações administrativas separadas, com confirmação explícita para apagar, compartilhar ou aplicar compactação.
- CLI para inicializar, servir, inspecionar, exportar/importar e administrar políticas.
- Busca retorna itens com pontuação relativa, motivo resumido, proveniência, confiança e permissões/escopo já aplicados. A pontuação não é apresentada como probabilidade.
- Erros distinguem entrada inválida, falta de autenticação, falta de permissão, recurso inexistente, conflito de versão e limite excedido. O contrato REST usa códigos HTTP coerentes (`400/401/403/404/409/413/422`) e corpo JSON estável com `code`, `message` e `request_id`.

## Escopo do MVP

MVP: armazenamento local, CLI, API REST e servidor MCP local; ciclo de vida da memória, proveniência, busca lexical, políticas de retenção, prévia/aplicação de compactação por provedor configurado, conflitos revisáveis, exportação/importação e compartilhamento por coleção com papéis básicos. Uma implantação compartilhada usa o mesmo serviço com PostgreSQL e tokens revogáveis. Não inclui interface gráfica, modelo de embeddings obrigatório, provedor LLM embutido ou sincronização offline entre réplicas.

## Requisitos não funcionais

| ID | Métrica e alvo | Condições de medição | Método de verificação | Origem |
| --- | --- | --- | --- | --- |
| RNF-001 | Primeira operação local em até 5 minutos, sem conta externa e sem dependência de serviço de terceiros. | Instalação limpa, Python suportado ou Docker; hardware comum documentado | Ensaio cronometrado por usuário novo | Pedido do usuário; alvo proposto |
| RNF-002 | Compactação apresenta redução estimada de tamanho e resultado de busca comparável ao conjunto de origem; a meta de qualidade será definida pelo corpus de referência antes do beta. | Corpus versionado com consultas e fatos esperados | Comparar bytes armazenados e Recall@5 antes/depois | Pedido do usuário; método proposto |
| RNF-003 | Nenhuma leitura ou alteração fora das permissões concedidas; revogação passa a valer em novas requisições. | Testes de isolamento entre usuários/coleções | Testes de autorização por endpoint e ferramenta MCP | Proposta de segurança inerente ao compartilhamento |
| RNF-004 | Busca local lexical em até 250 ms p95 para 10 mil memórias de até 2 KB cada, em máquina de referência a documentar. | Banco local sem carga concorrente; hardware de referência | Benchmark reproduzível | Meta inicial proposta, sujeita a revisão |
| RNF-005 | O conjunto de consultas de referência atinge Recall@5 de pelo menos 0,8 antes de habilitar compactação automática; compactação permanece manual no MVP. | Corpus e consultas aprovados; sem modelo remoto obrigatório | Avaliação offline de Recall@5 | Meta inicial proposta; revisar com dados reais |
| RNF-006 | Logs não contêm conteúdo de memória nem tokens; modo remoto usa TLS e armazenamento com controles de acesso. | Configuração de produção e teste com dados sintéticos | Inspeção de logs e configuração; teste de autorização | Recomendação do assistente; proposta de segurança |
| RNF-007 | Medir bytes por memória, espaço total, latência e chamadas/custo de provedores externos, quando usados. | Carga e configuração registradas | Benchmark e inspeção de métricas | Recomendação do assistente; medição obrigatória, meta aberta |

## Critérios de aceite

### CA-001 — Instalação inicial — RF-001

- Dado um ambiente limpo dentro dos requisitos documentados, quando o usuário seguir o guia de instalação, então consegue iniciar e fazer uma operação de exemplo dentro da meta de instalação aprovada.

### CA-002 — Controle de ciclo de vida — RF-002, RF-003

- Dado um proprietário autenticado, quando cria, consulta, atualiza ou apaga uma memória, então a alteração aparece na listagem e consultas subsequentes conforme o estado persistido.
- Dado um limite configurado, quando uma gravação ultrapassa esse limite, então o sistema aplica a política escolhida e informa o resultado sem descartar dados silenciosamente.

### CA-003 — Compactação revisável — RF-004

- Dado um conjunto de memórias, quando a compactação é proposta, então o proprietário pode inspecionar o resumo, as fontes relacionadas e o tamanho estimado antes de confirmar.
- Após confirmar, a recuperação para consultas de referência atende à qualidade mínima que será definida; a compactação não substitui os originais sem política explícita.

### CA-004 — Compartilhamento e revogação — RF-005

- Dado um proprietário que concede leitura a um destinatário, quando este consulta o escopo autorizado, então recebe apenas as memórias permitidas.
- Quando o proprietário revoga o acesso, então operações posteriores do destinatário são negadas.

### CA-005 — Portabilidade e integração — RF-006, RF-007

- Dado um conjunto exportado, quando importado em uma instalação compatível, então conteúdo e metadados preservados podem ser consultados conforme as regras de propriedade e acesso documentadas.
- Dada uma aplicação de exemplo, quando usa a interface documentada, então consegue gravar e recuperar uma memória sem depender de um provedor específico de LLM.

### CA-006 — Proveniência e recuperação — RF-008, RF-009, RF-012

- Dada uma memória salva, quando um usuário a inspeciona, então consegue ver sua origem, estado de confiança, histórico disponível e política de expiração.
- Dada uma consulta com memórias relevantes, quando a busca retorna resultados, então cada resultado inclui proveniência e informação suficiente para explicar sua inclusão.
- Dado um resultado incorreto ou desatualizado, quando o proprietário o corrige, então as consultas seguintes usam o valor atualizado e o histórico segue a política configurada.

### CA-007 — Contradições e duplicatas — RF-010

- Dada uma nova memória que pode duplicar ou contradizer outra, quando ela é adicionada, então o sistema sinaliza a relação e permite manter, substituir, combinar ou descartar conforme as opções definidas.
- A resolução não apaga silenciosamente a origem ou o histórico que a política determina preservar.

### CA-008 — Privacidade, retenção e métricas — RF-003, RF-011, RF-013

- Dada uma categoria de dados não permitida ou uma operação sem autorização, quando a aplicação tenta armazenar ou acessar o conteúdo, então a operação é bloqueada e o motivo é informado sem expor dados protegidos.
- Dada uma memória expirada ou apagada, quando a política de retenção é aplicada, então a memória e suas derivações recebem o tratamento configurado e o resultado pode ser verificado.
- Dado o uso do sistema, quando o proprietário consulta as métricas, então vê espaço ocupado e as métricas de operação disponíveis para o modo instalado.

## Rastreabilidade e verificação

| Requisito | Critérios de aceite | Verificação prevista | Evidência observada |
| --- | --- | --- | --- |
| RF-001 | CA-001 | Ensaio de instalação em ambiente limpo | Pacote e comandos documentados; ensaio não executado |
| RF-002, RF-003 | CA-002 | Testes de ciclo de vida, limites e políticas | Testes unitários escritos; execução não realizada |
| RF-004 | CA-003 | Revisão de prévia e avaliação de recuperação em corpus acordado | Prévia/aplicação manual implementadas; avaliação não executada |
| RF-005 | CA-004 | Teste de autorização, isolamento e revogação | Grants/tokens implementados; verificação não executada |
| RF-006, RF-007 | CA-005 | Teste de importação/exportação e exemplo de integração | Rotas REST, CLI e MCP stdio implementadas; teste não executado |
| RF-008, RF-009, RF-012 | CA-006 | Testes de proveniência, explicação de busca, correção e histórico | Testes de proveniência/busca escritos; execução não realizada |
| RF-010 | CA-007 | Testes de duplicatas, contradições e resolução | Avisos heurísticos e decisão manual implementados; cobertura semântica não verificada |
| RF-003, RF-011, RF-013 | CA-008 | Testes de políticas de dados/expiração e inspeção de métricas | TTL, quotas e métricas básicas; testes de expiração escritos, execução não realizada |

## Hipóteses, dúvidas e riscos

| ID | Hipótese, dúvida ou risco | Impacto / trabalho dependente | Forma de resolução | Estado |
| --- | --- | --- | --- | --- |
| Q-001 | O que significa “compacta”: pouca memória em disco, resumos menores, ou ambos? | Afeta arquitetura e métricas de aceitação | Definir prioridade entre footprint e compressão semântica | Pendente |
| Q-002 | “Compartilhada” significa entre processos/agentes do mesmo usuário, entre usuários, ou ambos? | Define identidade, autorização e sincronização | Escolher cenário inicial | Pendente |
| Q-003 | O modo inicial deve ser local, hospedado ou ambos? | Define instalação, persistência e operações de sync | Priorizar modo de execução | Pendente |
| Q-004 | Qual integração é necessária primeiro: SDK, API HTTP, MCP ou CLI? | Define superfície de produto e documentação | Escolher interface de entrada | Pendente |
| Q-005 | Que dados podem ser armazenados e por quanto tempo? Há requisitos de criptografia ou residência? | Afeta armazenamento e compartilhamento | Definir política de dados e ameaças | Pendente |
| Q-006 | Qual é o primeiro usuário-alvo: desenvolvedor solo, equipe ou usuário final? | Afeta onboarding, permissões e distribuição | Definir público prioritário | Pendente |
| Q-007 | Como medir relevância e qualidade da busca? | Afeta algoritmo, corpus e aceite da recuperação | Definir consultas de referência e limiares | Pendente |
| Q-008 | Quais níveis de confiança e tipos de proveniência são necessários? | Afeta o contrato de dados e a experiência de revisão | Definir categorias mínimas (por exemplo, fornecido, inferido, não confirmado) | Pendente |
| Q-009 | Qual histórico deve sobreviver à edição, compactação e exclusão, e por quanto tempo? | Afeta privacidade, recuperação e auditoria | Definir retenção de originais, derivados e logs | Pendente |
| Q-010 | Quais métricas e metas são necessárias para uso e custo? | Afeta observabilidade e critérios operacionais | Priorizar espaço, latência, volume e custo por operação | Pendente |

## Histórico de mudanças

| Data | IDs afetados | Mudança e motivo | Fonte |
| --- | --- | --- | --- |
| 2026-10-15 | Inicial | Especificação inicial derivada do pedido | Pedido do usuário |
| 2026-10-15 | RF-002–RF-013, RNF-005–RNF-007, CA-006–CA-008, Q-007–Q-010 | Acrescentadas recomendações sobre recuperação, proveniência, conflitos, privacidade, retenção, portabilidade e métricas | Pedido do usuário para adicionar as recomendações |
| 2026-10-15 | Contratos, estados, permissões, escopo do MVP, RNF-001–RNF-007 | Definidos modelo de dados, interfaces e metas iniciais para viabilizar decisão de arquitetura | Pedido do usuário para revisar e completar o plano |
| 2026-10-15 | Todos | Implementação inicial em Python criada; evidências de execução e aceite permanecem pendentes | Pedido do usuário para implementar |
