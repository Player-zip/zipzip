# 🐋 Peixão Hunter

Worker do **Peixão Hunter V6**, migrado do Colab para Python modular com foco em paridade antes de expansão.

## Estado atual

O worker já contém as etapas validadas do V6:

1. descoberta offline em `V3C_CACHE_NORMALIZED`;
2. amostra determinística de transações `entry → p1h`;
3. resolução resumível de `tx.from` via RPC;
4. classificação `EOA_NO_CODE / CONTRACT` via `eth_getCode`;
5. score de descoberta + fila persistente;
6. Peixão Score v0 usando o cache GMGN, quando disponível;
7. comparação automática dos artefatos finais com `V5_WALLETS`.

A fórmula e os critérios foram portados do notebook V6 validado, em vez de serem redesenhados do zero.

### Regressão já verificada

Usando os artefatos reais do V6 no Drive, o código migrado reproduziu exatamente:

- `528` linhas em `V6_scored_wallets.csv`;
- a mesma distribuição da fila: `P0=3`, `P1=15`, `P2=61`, `P3=449`;
- `462` EOAs elegíveis e `66` contratos bloqueados;
- `56` wallets casadas com o cache GMGN;
- o mesmo `V6_peixao_ranked.csv`, incluindo `2 A+` e `2 A`.

Os testes unitários preservam pontos críticos das fórmulas de score e cobrem os ciclos horário e de 6h, o stream/webhook, o login do Telegram e o ledger. Rodam automaticamente via GitHub Actions (`pytest -q`).

## Estratégia V2.2

A evolução do Peixão segue uma camada paralela de **Selective Alpha**: priorizar wallets com poucas entradas relevantes por semana, win rate alto, PnL alto, repetibilidade e boa qualidade por decisão — sem premiar atividade excessiva.

A especificação completa está em [`STRATEGY_V2_2.md`](STRATEGY_V2_2.md). V5/V6 e os scores legados continuam preservados para regressão e comparação.

## Ciclos e tabela final

O `scripts/worker_daemon.py` roda:

| Ciclo | Função | O que faz |
|---|---|---|
| contínuo | thread `robinhood-stream` + `robinhood-stream-flush` | recebe o webhook do QuickNode Stream e materializa candidatas EOA |
| 30 min | `stream_discovery.run_discovery_cycle_stream_first` | radares Solana/Base/Robinhood e fila de execução |
| 1 h | `priority_validation.run_priority_validation_cycle` | Nansen na fila adaptativa, tabela final, backtest, alertas |
| 6 h | `multichain_runner.run_v6` | V6 validado, Dune, reconciliação RPC, tabela final, alertas |

`V22S_wallet_stage1.csv` (lida por `/status`, `/wallets_bs`, alertas e backtest) é escrita **só** por `final_stage.build_final_stage1`:

- identidade da wallet é `chain:address`;
- as métricas passam pelo ledger de evidências (`evidence_ledger.py`), com unidades normalizadas (taxas sempre em razão: `0.65 = 65%`) e janela de frescor;
- a validação Dune entra pelo cache.

## Custo: perfil `economy` (padrão)

O objetivo é gastar o mínimo possível com APIs e RPCs pagos. `PEIXAO_COST_MODE=economy` é o padrão; `balanced` volta aos volumes anteriores. Qualquer variável definida explicitamente no ambiente vence o perfil.

| Onde gastava | O que mudou no `economy` |
|---|---|
| Nansen | até 5 wallets/rede/hora (antes 20); cache de 3 dias; wallet que falhou não é consultada de novo por 3 dias; só gasta com wallet de prioridade ≥ 45 na fila; wallet já pontuada é revisitada a cada 7 dias; teto de 250 chamadas/dia |
| QuickNode Solana | roda só no ciclo de 6h (antes também a cada 30 min); 60k créditos/dia (antes 330k); cache de 6h |
| Alchemy (Base) | descoberta de wallets reaproveitada por 6h quando a shortlist não muda (antes refazia a cada 30 min); teto de 3.000 chamadas/dia |
| Alchemy (Robinhood) | RPC público incremental (grátis) primeiro; Alchemy só se ele falhar |
| Birdeye | 3 tokens / 20 top traders / 10 PnL por rodada (antes 5/30/20); cache de PnL de 3 dias; teto de 200 chamadas/dia |
| Dune | 10 wallets por execução, cache de 3 dias, no máximo 1 execução/dia |
| Zerion / CoinStats | lotes de 10 / 4 (antes 40 / 8); tetos de 100 / 60 chamadas/dia |

Como funciona:
- **Teto diário fail-closed:** cada etapa paga registra o consumo real (tabela `provider_spend`). Se o teto do dia acabou, a etapa é pulada até a virada do dia (UTC). Lotes são dimensionados para caber no que resta.
- **Redes:** `PEIXAO_CHAINS` liga e desliga redes inteiras. Rede fora da lista não gasta API paga.
- **`/status`:** mostra o gasto das últimas 24h por provedor contra o teto, o uso de RPC do dia e o **custo por wallet alpha nova** (7 dias).

## Backtest honesto (sem custo de API)

Cada vez que uma wallet atinge um tier pela primeira vez, o sistema grava um sinal imutável com a foto do momento. Em 7, 14 e 30 dias ele procura no ledger a primeira observação **real** de provedor (data de consulta do cache Nansen/Zerion/CoinStats/Birdeye) depois do horizonte.

- **Sem viés de sobrevivência:** wallets que saíram da tabela também são avaliadas. Sinal sem observação conta como `NO_OBSERVATION` e aparece na cobertura.
- **Controle:** os tiers C/D servem de grupo de comparação.
- **Fora da amostra:** no horizonte de 30 dias, a janela "30d" do provedor cobre só o período depois do sinal.
- **Onde aparece:** no `/status`, em cada alerta (histórico do tier) e em `V23_score_outcomes_summary.csv`.
- **Custo:** nenhuma chamada extra. A cobertura depende das revisitas que o pipeline já faz.

## Acesso ao bot

| Forma | Como funciona |
|---|---|
| Senha compartilhada (`TELEGRAM_ACCESS_PASSWORD`) | Plano `pro`. Trocar a senha revoga essas sessões. Desligue com `PEIXAO_TELEGRAM_PASSWORD_LOGIN=false`. |
| Convite individual | Um admin gera com `/convite [pro\|basico] [dias]`. O código é de uso único, vale 7 dias para resgate e não depende da senha. |
| Admin (`TELEGRAM_ADMIN_CHAT_IDS`) | Sempre autorizado. Comandos: `/convite`, `/usuarios`, `/revogar <chat_id>`. |

O plano `pro` recebe os alertas automáticos; o `basico` só consulta `/status` e `/wallets_bs`.

## Gargalo de evidência: `/gargalo`, `/acelerar`, `/checar` (admins)

Wallets sem win rate não têm score. O `/gargalo` mostra, por rede, o motivo de cada uma estar parada:

| Motivo | O que significa | Acelerar ajuda? |
|---|---|---|
| nunca consultadas | nenhuma fonte de PnL tentou ainda | sim |
| erro no provedor | a última consulta falhou | sim |
| sem histórico | o provedor respondeu sem posições fechadas | não (reconsulta só após `PEIXAO_NO_DATA_RETRY_SECONDS`) |
| não elegíveis | contrato ou tipo de endereço que nunca terá win rate | não (ficam fora do "aguardando" no `/status`) |

O `/gargalo` também mostra a saúde e o orçamento de cada fonte (ex.: Nansen pausado por 403 até tal hora).

| Comando | O que faz |
|---|---|
| `/acelerar <rede> [n] [extra]` | Seleciona até `n` wallets aceleráveis (nunca consultadas primeiro, por prioridade), mostra o custo estimado e só roda depois de `/confirmar <código>` (vale 10 min). Sem `extra`, respeita o teto diário; com `extra`, autoriza passar do teto só neste job. |
| `/checar <endereço> [rede]` | Consulta uma wallet agora, ignorando caches. |
| `/jobs`, `/cancelar <código>` | Lista e cancela pedidos. |

Os pedidos são executados pelo worker **entre** os ciclos (nunca em paralelo com eles). Depois de cada job, a tabela final é reconstruída e o resultado chega no chat: quantas ganharam win rate, tiers, custo e alertas enviados.

Fontes de win rate por rede:

| Rede | Fontes |
|---|---|
| Base / Robinhood | Nansen e, quando ele não resolve, Zerion (`PEIXAO_ZERION_FALLBACK`), dentro dos tetos diários |
| Solana | Birdeye PnL. No ciclo normal só os top traders do Birdeye são consultados; as wallets vindas do QuickNode dependem do `/acelerar solana`. |

## Mudanças de comportamento desta versão

- **Win rate do GMGN preservado:** wallets legadas com win rate do GMGN não perdem mais o dado ao juntar fontes. No código anterior, uma coluna `win_rate` vazia (NaN) fazia o ledger ignorar o `gmgn_winrate_30d`, e essas wallets apareciam como "aguardando evidência" no ciclo horário.
- **Zerion como alternativa:** quando o Nansen está pausado ou falha, a Zerion entra automaticamente nas redes Base e Robinhood, dentro do teto diário.

- **Perfil `economy` é o padrão** e reduz bastante o volume de chamadas pagas (tabela acima). Para voltar aos volumes antigos: `PEIXAO_COST_MODE=balanced`. O `.env.example` deixa as variáveis de custo comentadas de propósito, para não anular o perfil.
- **Backtest antigo substituído:** o `backtest_v23` (fotos de hora em hora, só wallets ainda na tabela) deu lugar ao `score_outcomes`. As tabelas antigas ficam no banco, sem uso.
- **Login do Telegram:** após `PEIXAO_TELEGRAM_AUTH_MAX_ATTEMPTS` senhas erradas o chat fica bloqueado por `PEIXAO_TELEGRAM_AUTH_LOCKOUT_SECONDS`.
  - A mensagem com a senha é apagada do chat.
  - **Trocar `TELEGRAM_ACCESS_PASSWORD` revoga todas as sessões.** Sessões que já existiam ficam presas à senha vigente no primeiro start; troque a senha depois do deploy se quiser derrubá-las.
  - Novo comando `/logout`.
- **Alertas** vão só para sessões válidas. A chave de entrega passa a ser `chain:address`: a mesma wallet EVM em duas redes gera dois alertas, e entregas antigas não se repetem.
- **ROI do Nansen** passa a ser gravado em razão (`realized_roi_unit=ratio`). Entradas de cache antigas são convertidas na leitura.
- **Stream:** falha de RPC na checagem de EOA não marca mais a wallet como contrato. Sem `ROBINHOOD_RPC_URL`, a checagem usa `PEIXAO_RPC_URL`.
- **`gmgn-cli`** fixado em `1.6.6` com `package-lock.json` e executado com ambiente mínimo (sem as outras chaves).
- **Variáveis:** todas as lidas pelo código estão documentadas no `.env.example`, com os padrões. Em vez de `PEIXAO_ROBINHOOD_LOOKBACK_BLOCKS`, o fallback rápido do Robinhood usa `PEIXAO_ROBINHOOD_FAST_LOOKBACK_BLOCKS`.

## Rodar localmente

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
export PEIXAO_PROJECT_ROOT=/caminho/para/PEIXAO_HUNTER/V3_IMPACT
export PEIXAO_DATA_DIR=./data
python scripts/run_worker.py
```

A raiz indicada por `PEIXAO_PROJECT_ROOT` deve conter o JSON V3B e `V3C_CACHE_NORMALIZED`. Se `V5_WALLETS` existir, os caches caros são semeados automaticamente e a paridade V5×V6 é verificada.

## Railway

O repositório já contém `railway.json`. O serviço usa `scripts/worker_daemon.py`: se os dados-base ainda não estiverem no volume, ele espera sem cair; depois que `/data/project` estiver semeado, executa o pipeline e reaproveita os caches persistentes.

Variáveis principais:

- `PEIXAO_DATA_DIR=/data`
- `PEIXAO_PROJECT_ROOT=/data/project`
- `PEIXAO_WORKER_INTERVAL_SECONDS=21600`
- chaves de providers configuradas apenas nos Secrets/Variables do Railway.

Nenhuma chave real de API é commitada.

## Próxima etapa

1. conectar este repositório ao Railway;
2. criar volume persistente em `/data`;
3. semear os dados-base/caches no volume;
4. adicionar a ponte de comandos para que jobs criados pelo chat sejam processados pelo worker;
5. conectar Nansen/Zerion/Alchemy/Blockscout somente para wallets que chegaram em `READY_PROVIDER`.
