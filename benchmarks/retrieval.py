"""Evaluate retrieval against a small, deterministic Portuguese memory corpus."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import uuid
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

CORPUS_VERSION = "1"

CORPUS = [
    ("A usuária prefere respostas em português e com exemplos curtos.", "idioma"),
    ("O banco local do projeto usa SQLite com índice FTS5.", "banco"),
    ("O serviço deve permanecer vinculado a 127.0.0.1 no modo local.", "rede"),
    ("A política de expiração padrão pode ser configurada em dias.", "expiração"),
    ("O acesso compartilhado usa tokens bearer revogáveis.", "autenticação"),
    ("A compactação exige revisão antes de arquivar as memórias de origem.", "compactação"),
    ("O pacote deve suportar Python 3.13 e versões posteriores.", "python"),
    ("A exportação preserva memórias e relações em JSON versionado.", "exportação"),
    ("A busca lexical retorna uma pontuação relativa e um motivo.", "busca"),
    ("Memórias privadas recebem um escopo associado ao proprietário.", "privacidade"),
    ("O limite de resultados da busca aceita entre um e cinquenta itens.", "limite_busca"),
    ("A API pública mantém seus endpoints sob o prefixo /v1.", "api_version"),
    ("O histórico guarda o conteúdo anterior quando uma memória é editada.", "historico"),
    ("A revogação de um grant impede leituras feitas em chamadas seguintes.", "revogacao"),
    ("Os tokens de API são persistidos como hashes SHA-256.", "hash_token"),
    ("O servidor MCP local comunica-se pelo transporte stdio.", "mcp_stdio"),
    ("O PostgreSQL compartilhado inicia pelo Docker Compose do projeto.", "compose"),
    ("A busca não retorna memórias expiradas.", "busca_expirada"),
    ("A origem de uma memória contém tipo e referência opcionais.", "proveniencia"),
    ("A confiança aceita asserted, inferred e unverified.", "confianca"),
    ("Conflitos podem ser resolvidos mantendo um fato ou ambos.", "resolucao"),
    ("O identificador request_id é devolvido no cabeçalho HTTP.", "request_id"),
    ("A API exige confirmação antes de aplicar uma proposta de compactação.", "confirmacao"),
    ("O backup JSON não inclui tokens nem grants de compartilhamento.", "backup_privacidade"),
    ("A configuração do banco usa MEMORY_DATABASE_URL.", "database_url"),
    ("A porta HTTP local padrão do serviço é 8765.", "porta"),
    ("O índice FTS5 pesquisa conteúdo e tags das memórias.", "fts_tags"),
    ("A atualização concorrente usa expected_version para detectar edição obsoleta.", "concorrencia"),
    ("A chave MEMORY_DEFAULT_TTL_DAYS define expiração padrão em dias.", "ttl_env"),
    ("A compactação cria um resumo derivado e arquiva suas fontes.", "resumo_derivado"),
]

QUERIES = [
    ("idioma português exemplos curtos", "idioma"),
    ("qual banco de dados índice textual", "banco"),
    ("endereço de rede modo local", "rede"),
    ("prazo retenção padrão", "expiração"),
    ("como autenticar cliente compartilhado", "autenticação"),
    ("resumir arquivar memórias", "compactação"),
    ("versão mínima interpretador", "python"),
    ("backup formato com ligações", "exportação"),
    ("relevância resultado lexical", "busca"),
    ("isolamento dado dono", "privacidade"),
    ("quantidade máxima de resultados", "limite_busca"),
    ("versão das rotas HTTP", "api_version"),
    ("consultar conteúdo anterior após edição", "historico"),
    ("cancelar permissão de leitura", "revogacao"),
    ("segredo da API armazenado com que algoritmo", "hash_token"),
    ("MCP transporte processo local", "mcp_stdio"),
    ("inicializar postgres compartilhado", "compose"),
    ("omitir registros vencidos da consulta", "busca_expirada"),
    ("metadados que mostram de onde veio o fato", "proveniencia"),
    ("rótulos de certeza da informação", "confianca"),
    ("decidir entre fatos incompatíveis", "resolucao"),
    ("identificar chamada HTTP nos logs", "request_id"),
    ("aprovar resumo antes de substituir memórias", "confirmacao"),
    ("quais credenciais não vão no arquivo de exportação", "backup_privacidade"),
    ("variável de ambiente para endereço do banco", "database_url"),
    ("número de porta usado no localhost", "porta"),
    ("buscar etiquetas junto do texto", "fts_tags"),
    ("evitar sobrescrever versão nova com atualização antiga", "concorrencia"),
    ("configuração de validade em dias", "ttl_env"),
    ("efeito de confirmar uma compactação", "resumo_derivado"),
]


def request(
    base_url: str, token: str | None, method: str, path: str, payload: dict[str, Any] | None = None
) -> Any:
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as response:
            return None if response.status == 204 else json.loads(response.read())
    except HTTPError as exc:
        raise SystemExit(f"API HTTP {exc.code} em {path}: {exc.read().decode(errors='replace')}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("MEMORY_API_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.getenv("MEMORY_API_TOKEN"))
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    if args.k < 1:
        parser.error("--k precisa ser maior que zero")

    base_url = args.url.rstrip("/")
    scope = f"benchmark:{uuid.uuid4()}"
    memory_ids: list[str] = []
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    recalls: list[float] = []
    try:
        expected_ids: dict[str, str] = {}
        for content, label in CORPUS:
            result = request(base_url, args.token, "POST", "/v1/memories", {
                "content": content,
                "kind": "fact",
                "scope_id": scope,
                "source": {"type": "benchmark", "ref": label},
                "confidence": "asserted",
                "tags": [label],
            })
            memory_id = result["memory"]["id"]
            expected_ids[label] = memory_id
            memory_ids.append(memory_id)

        for query, expected_label in QUERIES:
            started = time.perf_counter()
            result = request(base_url, args.token, "POST", "/v1/search", {
                "query": query, "scope_id": scope, "limit": args.k,
            })
            latencies.append((time.perf_counter() - started) * 1000)
            hits = result["results"]
            found = [hit["memory"]["id"] for hit in hits]
            expected_id = expected_ids[expected_label]
            recalls.append(float(expected_id in found))
            reciprocal_ranks.append(1.0 / (found.index(expected_id) + 1) if expected_id in found else 0.0)

        ordered = sorted(latencies)
        p95_index = min(len(ordered) - 1, int(0.95 * len(ordered)))
        print(json.dumps({
            "corpus_version": CORPUS_VERSION,
            "queries": len(QUERIES),
            "corpus_memories": len(CORPUS),
            "k": args.k,
            "recall_at_k": round(statistics.mean(recalls), 4),
            "mrr": round(statistics.mean(reciprocal_ranks), 4),
            "latency_ms_mean": round(statistics.mean(latencies), 2),
            "latency_ms_p95": round(ordered[p95_index], 2),
        }, ensure_ascii=False, indent=2))
    finally:
        for memory_id in memory_ids:
            request(base_url, args.token, "DELETE", f"/v1/memories/{memory_id}")


if __name__ == "__main__":
    main()
