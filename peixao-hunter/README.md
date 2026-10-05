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

Os testes unitários também preservam pontos críticos das fórmulas de score e rodam automaticamente via GitHub Actions.

## Estratégia V2.2

A evolução do Peixão segue uma camada paralela de **Selective Alpha**: priorizar wallets com poucas entradas relevantes por semana, win rate alto, PnL alto, repetibilidade e boa qualidade por decisão — sem premiar atividade excessiva.

A especificação completa está em [`STRATEGY_V2_2.md`](STRATEGY_V2_2.md). V5/V6 e os scores legados continuam preservados para regressão e comparação.

## Rodar localmente

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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
