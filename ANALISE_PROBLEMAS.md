# Peixão Hunter — análise dos principais problemas

Objeto: `peixao-hunter-main` (commit `42191b0`, ~15 mil linhas Python, 56 módulos).
Caminhos abaixo são relativos à raiz desse projeto.

**Estado verificado**

- `pytest`: 59 testes passam. Cobertura total de **40%**.
- `pyflakes`/`ruff`: só avisos menores (imports não usados, `zip()` sem `strict`).
- Os bugs marcados com **[reproduzido]** foram confirmados com os scripts em `analise/`.

Os problemas estão em ordem de impacto: primeiro o que gera resultado errado, custo ou risco de segurança, depois robustez e por fim higiene.

---

## Críticos / altos

### 1. Três pipelines diferentes sobrescrevem o mesmo resultado final
O arquivo `V22S_wallet_stage1.csv` é a fonte de `/status`, `/wallets_bs` e dos alertas. Hoje três caminhos escrevem nele, cada um com regras diferentes:

| Ciclo | Onde | Identidade da wallet | Usa Dune? | Normaliza unidades? |
|---|---|---|---|---|
| 6h (legado) | `runner.py:255-262` | só `address` | sim | não |
| 6h (multichain, logo depois) | `multichain_runner.py:389-395` | só `address` | **não** | não |
| 1h (V2.3) | `priority_validation.py:130-137` | `chain:address` | não | sim (ledger) |

Consequências:
- No mesmo ciclo de 6h, a validação Dune paga (`runner.py:247`) é calculada e logo descartada, porque o multichain reescreve a tabela sem o `input_override` do Dune.
- O score de uma wallet muda conforme o último ciclo que rodou.
- O ciclo de 6h dispara `notify_alpha_wallets` **duas vezes** (`runner.py:273` e `multichain_runner.py:402`), sobre duas versões diferentes da tabela.

**Correção:** um único construtor da tabela final (o caminho chain-aware da V2.3, com Dune incluído). Os outros ciclos só alimentam insumos. Os alertas saem de um único ponto.

### 2. Unidades de ROI e win rate inconsistentes inflam o score [reproduzido]
- **Nansen:** `nansen_evm.py:142` grava `realized_roi_30d = realized_pnl_percent`, em pontos percentuais. Só o ledger divide por 100 (`evidence_ledger.py:144`). O caminho de 6h (item 1) usa o valor bruto. `alpha_v22.py:203` trata 12,5 como 1250%, e o componente `pnl_roi` satura. No repro, `pnl_roi` vai de 50,8 para 88,3 e a wallet sobe do tier **C para o B**.
- **Birdeye:** usa outra heurística (`birdeye_alpha.py:138`, divide por 100 quando `|roi| > 5`). Um ROI de 600% (6.0) vira 6%, e um ROI de 3% (3.0) vira 300%.
- **Win rate:** `alpha_v22.py:130` e o gate em `:98` **cortam** (`_clamp`) o valor em vez de normalizar. Já `selective_alpha.py:92-93` divide por 100. Um win rate de 55 (55%) vira 100%: passa no gate (`PASS`), dá Wilson 0,87 e `deep_dive=True`. Hoje isso está latente, porque Birdeye, CoinStats e Zerion já entregam razão. Qualquer fonte nova que entregue porcentagem fura o gate de WR ≥ 60%.

**Correção:** cada adaptador de provedor normaliza para razão na saída, com uma única função compartilhada. O score valida a faixa (0–1) e rejeita valores fora dela em vez de cortar.

### 3. O webhook do QuickNode Stream fica bloqueado durante chamadas de rede [reproduzido]
- `materialize_stream_candidates` segura `_STATE_LOCK` (`robinhood_stream.py:455-513`) enquanto faz até 20 `eth_getCode`. Cada um tem timeout de 15 s e até 5 tentativas com backoff de até 8 s.
- O handler do webhook (`ingest_stream_payload`, `:164`) precisa do mesmo lock.
- No repro, um RPC de 3 s bloqueou o webhook por 2,8 s. No pior caso o bloqueio dura minutos, muito acima do `post_timeout_sec: 10` configurado no stream. O resultado são retries, entregas perdidas e risco de o QuickNode pausar o stream.
- Além disso, cada entrega lê e regrava **todo** o JSON de estado com `indent=2` (wallets dos últimos 7 dias + `seen_events` dos últimos 2 dias). O custo por entrega é O(N) e cresce com o volume.

**Correção:** fazer as chamadas de rede fora do lock (copiar a lista, checar sem lock, aplicar o resultado com lock). Guardar o estado do stream em SQLite (WAL), não num JSON único.

### 4. Falha de RPC vira "não é EOA" e fica em cache por 24h [reproduzido]
- `_is_eoa_paced` devolve `(False, attempts)` em `RpcCallError` (`robinhood_chainstack.py:217-218`).
- `materialize_stream_candidates` grava `is_eoa=False` com `eoa_checked_epoch=now` (`robinhood_stream.py:485-490`) e não conta como erro.
- No repro, um 429 persistente resultou em `wallets=0` e `errors=0`, com a wallet descartada por 24 h.
- Relacionado: se `ROBINHOOD_RPC_URL` não estiver definida (ela não aparece no `.env.example`), nenhuma wallet do stream é materializada e nada avisa.

**Correção:** distinguir três estados (EOA / contrato / desconhecido), nunca cachear "desconhecido" e logar quando a URL de RPC está ausente.

### 5. Segurança do bot Telegram
- A senha é única e compartilhada, e as tentativas são **ilimitadas**: errar mantém `awaiting_password=1` (`telegram_auth.py:552`). Não há rate limit, bloqueio nem auditoria. A comparação deveria usar `hmac.compare_digest`.
- A autorização **nunca expira e não pode ser revogada**: trocar `TELEGRAM_ACCESS_PASSWORD` não desloga ninguém. A senha também fica no histórico do chat, porque a mensagem não é apagada.
- O offset do `getUpdates` só é salvo no fim do lote (`:599`). Uma exceção no meio, por exemplo `database is locked`, faz o lote inteiro ser reprocessado a cada 3 s. O efeito é respostas repetidas e um "poison message" que trava o bot. Esse erro é plausível porque `_db` (`:72`) usa o timeout padrão de 5 s no mesmo SQLite em que o pipeline escreve.

**Correção:** limite de tentativas por chat com bloqueio temporário, expiração e revogação (versão da senha gravada junto da autorização), `deleteMessage` na senha e avanço do offset por update.

### 6. Todos os segredos vão para um CLI npm de terceiros sem versão fixa
`gmgn_live.py:52` passa `os.environ.copy()` para o `gmgn-cli`. Isso inclui as chaves de Nansen, Telegram, QuickNode, Birdeye e Dune. O `package.json` usa `^1.6.6` e não há lockfile. Um release comprometido receberia todas as chaves.

**Correção:** passar um ambiente mínimo (`PATH`, `HOME`, `GMGN_API_KEY`), fixar a versão exata e commitar o `package-lock.json`.

### 7. Endpoint público do webhook vulnerável a DoS
`robinhood_stream.py:644-646` lê o corpo inteiro e descompacta o gzip **antes** de verificar a assinatura, sem limite de `Content-Length`. Uma gzip bomb esgota a memória do container. Também não há proteção contra replay de nonce dentro da janela de 600 s; a deduplicação de eventos só mitiga em parte.

**Correção:** limite de tamanho (bruto e descompactado), verificação da assinatura antes de descompactar quando possível, e cache de nonces.

---

## Médios

### 8. Ledger de evidências (`evidence_ledger.py`) [reproduzido]
- **Prioridade vence recência.** A ordenação em `:319` é (prioridade do provedor, qualidade, data). Um dado do Nansen de 2025 vence um do Birdeye de hoje, para sempre. Não há janela nem expiração, então métricas "30d" envelhecem sem limite.
- **Crescimento ilimitado.** A chave primária inclui `observed_at`, e `chain_aware_inputs.py:112` usa `updated_at`, que é "agora" a cada ciclo. Toda hora entram cerca de 17 métricas por wallet. `canonical_metrics_for_wallet` lê o histórico inteiro de cada wallet a cada ciclo, então o ciclo horário fica cada vez mais lento.
- **Atribuição errada.** Uma linha com win rate do GMGN e PnL do Nansen é gravada inteira como `NANSEN` (`_provider(row)`).
- **Conexões demais.** Abre duas conexões SQLite, cada uma com DDL `executescript`, por wallet e por ciclo.

### 9. Identidade da wallet só por endereço
`evm_radar.py:586` e `selective_alpha.py:273` deduplicam só por `address`. A mesma wallet EVM em Base e Robinhood vira uma linha só, e a evidência de uma das chains é perdida. Também não há normalização de maiúsculas e minúsculas nesse caminho (checksum vs. minúsculas gera duplicata). O caminho V2.3 faz isso certo, mas é sobrescrito pelo ciclo de 6h (item 1).

### 10. Rede errada no alerta
As wallets legadas V6 (Robinhood) chegam sem a coluna `chain`. `telegram_notifier.py:24` usa "Solana" como padrão, enquanto o `/status` rotula as mesmas wallets como Robinhood.

### 11. Controle de custo parcial
- O `RpcBudgetManager` ("fail-closed") só protege o pipeline legado (`pipeline/validation.py`). `eth_getLogs` do Robinhood, os checks de EOA do stream, Base/Alchemy e os fallbacks Solana (Helius/Shyft) não têm teto persistente.
- O QuickNode cobra um valor fixo estimado de 30 créditos por chamada, qualquer que seja o método.
- O shim `quicknode_solana_compat.py:70` marca `getMultipleAccounts` como "não suportado" após **qualquer** erro, inclusive timeout ou 429 transitório. A partir daí faz um `getAccountInfo` por conta pelo resto do ciclo: cerca de N vezes os créditos.

### 12. Arquitetura por monkeypatch e versões empilhadas
- `peixao/__init__.py` instala patches na importação (`quicknode_solana_compat`, `telegram_status_patch`).
- `robinhood_stream_v2.py:141-144` reescreve variáveis globais de `robinhood_stream`.
- O comportamento depende da ordem de import, e os testes contaminam uns aos outros.
- Há módulos `*_v2`, `*_v22`, `*_v23`, V5/V6 convivendo. `queue.py` é código morto (nenhum módulo o importa).

### 13. Scheduler single-thread e trabalho duplicado
- O ciclo pesado de 6h (`run_v6`) bloqueia o loop do `worker_daemon.py`. Enquanto ele roda, param a materialização do stream, o discovery de 30 min e a validação horária.
- O ciclo de 6h refaz o discovery rápido (radar de tokens, QuickNode Solana, Base, Robinhood), com custo duplicado.
- `_safe` e `except Exception: pass` engolem exceções sem traceback (`multichain_runner.py:165`, `priority_validation.py:19`, `stream_discovery.py:141`).

### 14. Um provedor novo pode derrubar o ciclo validado
Os probes de GMGN, Mobula, Jupiter, Solana Tracker e Dune rodam com `stage()`, que faz `raise` (`runner.py:138-176`). Isso contradiz o comentário "New providers must never take down the validated V6 worker". Por exemplo, um JSON inválido com HTTP 200 em `dune._usage_summary` aborta tudo antes do Selective Alpha e dos alertas. A chave de etapa `08d` também está duplicada.

### 15. Configuração espalhada
- `Settings` lê o ambiente no import. Um valor inválido (ex.: `PEIXAO_RPC_TIMEOUT=abc`) quebra o import do pacote inteiro.
- 54 variáveis são lidas com `os.getenv` fora do `Settings` e não aparecem no `.env.example`: `ROBINHOOD_RPC_URL`, `QUICKNODE_RPC_URL`, `QUICKNODE_STREAMS_API_KEY`, `COINSTATS_API_KEY`, `PEIXAO_ROBINHOOD_*` e outras.
- A mesma variável tem padrões diferentes conforme o caminho:

| Variável | Um caminho | Outro caminho |
|---|---|---|
| `PEIXAO_QUICKNODE_MAX_WALLETS` | 30 | 25 |
| `PEIXAO_QUICKNODE_CACHE_TTL` | 3600 | 1800 |
| `PEIXAO_ROBINHOOD_LOOKBACK_BLOCKS` | 30000 | 150000 |

---

## Baixos / higiene

16. **Testes.** A orquestração e a segurança estão quase sem teste:

    | Módulo | Cobertura |
    |---|---|
    | `priority_validation` | 0% |
    | `stream_discovery` | 0% |
    | `bootstrap` | 0% |
    | `backtest_v23` | 0% |
    | `telegram_auth` | 9% |
    | `telegram_notifier` | 12% |
    | `runner` | 20% |
    | `multichain_runner` | 20% |
    | `worker_daemon` | sem teste |

    O CI usa Python 3.12, e o Nixpacks não fixa versão.
17. `.github/workflows/fix-runner-newlines.yml` tem `contents: write` e reescreve `runner.py` trocando `\n`. É um remendo de incidente antigo e deve ser removido.
18. `alpha_simulation_once` (em `scripts/worker_daemon.py`) envia aos usuários autorizados uma "simulação" com endereço real e números fixos no código.
19. `pytest` está nas dependências de produção, e não há lock de dependências Python.
20. O bootstrap baixa um ZIP público do Google Drive com ID e SHA fixos no código. Ele protege contra zip-slip, mas não contra zip bomb.
21. Lint: imports não usados (`bootstrap.shutil`, `coinstats_enrichment.math`, `rpc_budget.defaultdict`).

---

## Ordem sugerida de correção

1. **Itens 1 e 2:** fonte única de verdade para a tabela final e normalização de unidades. É o que hoje produz score e alerta errados.
2. **Itens 3, 4 e 7:** confiabilidade e segurança do stream. Lock fora da rede, estado "desconhecido" e limite de payload.
3. **Itens 5 e 6:** segurança do bot e dos segredos.
4. **Itens 8 e 11:** ledger com janela e retenção, e orçamento unificado por provedor.
5. **Itens 12 a 15:** remover monkeypatches, centralizar a configuração e criar testes de integração para `priority_validation`, `run_v6` e `telegram_auth`.

## Como reproduzir

Na raiz do `peixao-hunter-main`, com as dependências instaladas:

```bash
python /caminho/analise/repro_units.py    # itens 2 e 8
python /caminho/analise/repro_stream.py   # itens 3 e 4
```
