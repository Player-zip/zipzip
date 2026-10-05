# Peixão V2.2 — Estratégia Selective Alpha

## Objetivo central

O Peixão não procura simplesmente wallets lucrativas. O objetivo é estimar quais wallets têm maior probabilidade de manter **edge futuro**, com foco explícito em operadores seletivos:

> poucas entradas relevantes por semana + win rate alto + PnL alto + repetibilidade + baixa dependência de um único acerto.

A unidade de atividade é **nova posição/token**, não quantidade bruta de transações. Vários swaps usados para montar a mesma posição não devem ser tratados como várias entradas independentes.

## Perfil-alvo: Selective Alpha Wallet

O perfil desejado prioriza:

- baixa frequência de novas posições por semana;
- `win_rate >= 60%`, com valorização progressiva acima de 65–70%;
- amostra estatística suficiente para confiar no win rate;
- PnL realizado alto e PnL mediano por posição relevante;
- lucro distribuído entre vários tokens, sem concentração excessiva em um único vencedor;
- repetibilidade em tokens independentes e ao longo do tempo;
- entradas relativamente cedo, antes da maior parte do movimento;
- baixa atividade típica de bot/sniper/churn;
- comportamento potencialmente copiável.

Penalizações prioritárias:

- atividade muito alta e indiscriminada;
- centenas de tokens/posições por semana;
- churn excessivo;
- PnL dominado por um único trade/token;
- padrões de dev, deployer, insider ou bundler;
- entradas sistematicamente tardias;
- evidência insuficiente apresentada como certeza.

## Arquitetura do funil

```text
Jupiter / DexScreener / Birdeye Smart Money / GMGN opcional
                         ↓
                    TOKEN RADAR
          organic activity + volume + liquidity
          smart money + token age + risk signals
                         ↓
                TOKENS PROMISSORES
                         ↓
               BIRDEYE TOP TRADERS
                  Top 30 primeiro
                         ↓
                 HARD TAG FILTERS
           dev / insider / bundler / deployer
                         ↓
              CROSS-TOKEN INTERSECTION
                         ↓
                BIRDEYE PNL SUMMARY
                         ↓
                    WR >= 60%
                         ↓
                SAMPLE CONFIDENCE
                         ↓
                 SELECTIVITY SCORE
                         ↓
               STAGE 1 ALPHA SCORE
       WR + confidence + repeatability + selectivity
       profit quality + weighted cross-token edge
                         ↓
                 TOP 20–40 WALLETS
                         ↓
                    DEEP DIVE
      Nansen + Helius/Shyft/RPC + GMGN/cache + outros
                         ↓
      funding / clusters / hold time / drawdown
        copyability / entry timing / behavior
                         ↓
                 FINAL ALPHA SCORE
                         ↓
                MASTER WALLET DB
                         ↓
                      WATCHER
                         ↓
           O QUE AS ALPHA WALLETS COMPRAM?
                         ↓
                TOKEN CONFLUENCE
                         ↓
                   NOVOS TOKENS
                         ↺
```

## Token Radar

O Radar deve ser barato e **zero-RPC por padrão**.

Prioridade de fontes:

1. Jupiter Organic Score / recent activity;
2. DexScreener;
3. Birdeye Smart Money e market data;
4. GMGN quando disponível;
5. providers adicionais de risco como GoPlus/RugCheck quando fizer sentido.

O Radar existe para reduzir o universo antes de usar endpoints mais caros. O Birdeye Top Traders só deve ser chamado para tokens já aprovados por filtros baratos.

## RPC Budget

Discovery/Token Radar deve manter orçamento RPC em zero. RPC fica reservado para wallet validation e Deep Dive.

Regras:

- cache antes de qualquer chamada;
- TTL por tipo de dado;
- deduplicação de token/wallet;
- orçamento por etapa, execução, hora e dia;
- cada retry conta como consumo;
- provider fallback não pode multiplicar chamadas desnecessariamente.

## Win Rate e confiança

`WR >= 60%` continua como hard gate conceitual, mas win rate sem amostra não é suficiente.

O score deve combinar:

- `closed_positions`;
- `total_positions`/`total_trades` quando útil;
- Wilson lower bound ou métrica de confiança equivalente;
- penalização forte para amostra pequena.

Wallets novas com poucas posições não devem ser descartadas automaticamente; devem receber estado de evidência inferior. O deep dive pode usar um limiar mínimo inicial próximo de 10 posições fechadas, sujeito a calibração após coleta de dados reais.

## Selectivity Score

A V2.2 introduz o conceito de `selectivity_score`.

Ele deve medir a qualidade por decisão, e não premiar volume operacional. Conceitualmente:

```text
selectivity_score =
    quality_per_entry
  × winrate_confidence
  × pnl_per_position
  × low_frequency_factor
```

A frequência será medida por **novas posições/tokens por semana**. Não haverá hard gate rígido de frequência inicialmente; o sistema deve observar a distribuição real e aplicar penalização progressiva para atividade excessiva.

## Weighted Cross-Token Edge

`cross_token_hits` deixa de ser apenas contagem. O objetivo é algo como:

```text
weighted_cross_token_edge = Σ(
    token_quality
  × trader_rank_weight
  × entry_quality
  × independence_weight
  × recency_weight
)
```

Ser Top 5 trader em vários tokens fortes deve valer muito mais do que aparecer perto do fim do ranking em tokens medianos.

## Independência e clusters

O Token Confluence deve migrar de `alpha_wallet_count` para `independent_alpha_clusters`.

Cinco wallets não contam como cinco sinais se houver forte evidência de que pertencem ao mesmo cluster. Evidências futuras para clustering:

- funding direto ou origem comum forte;
- transferências internas;
- mesmo deployer/ecossistema;
- forte sincronização temporal;
- sobreposição recorrente de tokens;
- mesma CEX como funding é evidência fraca isoladamente.

Nenhum sinal isolado deve ser tratado como prova definitiva de identidade comum.

## Profit Quality / Concentration

PnL bruto terá influência limitada. Métricas centrais:

- `largest_win_share`;
- `top3_win_share`;
- HHI de lucros;
- `effective_winners = 1 / HHI`;
- PnL mediano por posição/token;
- quantidade e proporção de posições fechadas lucrativas.

O objetivo é distinguir skill repetível de sorte, insider ou um único outlier.

## Repeatability

Repeatability deve medir três dimensões:

1. consistência entre tokens independentes;
2. consistência ao longo do tempo;
3. consistência entre regimes/narrativas diferentes.

Uma wallet que concentrou toda a performance em uma única semana ou narrativa deve perder pontos em relação a uma wallet que repete edge ao longo do tempo.

## Snapshot T0 e entrada precoce

Desde o Radar, cada sinal relevante deve gerar snapshot T0 para reduzir survivorship bias.

Campos desejados incluem:

- preço no momento do sinal;
- market cap;
- liquidez;
- volume;
- idade do token;
- organic/smart-money signals;
- posição/ranking da wallet;
- momento da entrada da wallet;
- `price_vs_launch`;
- `mcap_at_entry`;
- `liquidity_at_entry`.

Depois, acompanhar:

- retorno 1h / 6h / 24h / 7d;
- 3x / 5x / 10x;
- max favorable excursion;
- max adverse excursion;
- tempo até o pico.

## Copyability

A pergunta final não é apenas “essa wallet ganha?”, mas “esse edge seria copiável?”.

Fase posterior deve simular seguidores entrando com atraso de:

- +5s;
- +15s;
- +30s;
- +60s;
- +5min.

Isso alimentará um `copy_capture_ratio` e um componente específico de copyability.

## Stage 1 Score — direção de pesos

Os pesos ainda serão calibrados com dados reais, mas a ordem de importância da V2.2 passa a ser:

- repeatability e consistência temporal;
- win rate ajustado por confiança;
- selectivity;
- independent weighted cross-token edge;
- profit quality/concentration;
- entry quality;
- PnL/ROI bruto com peso limitado.

PnL acumulado sozinho nunca deve dominar o ranking.

## Evidência

Regra permanente:

> Nunca inventar evidência ausente.

Se uma métrica ainda não está disponível, o sistema registra ausência e reduz `evidence_coverage`/estado de evidência. O score não deve assumir comportamento positivo apenas porque faltam dados.

## Visão de produto

```text
Discovery Engine
    encontra onde procurar

Alpha Validation Engine
    decide quem merece confiança

Watcher
    observa o que as melhores wallets fazem agora

Prediction Layer
    mede se o edge continua funcionando no futuro
```

O Peixão Score deve ser tratado como score heurístico de edge até existir backtest walk-forward e calibração suficiente para convertê-lo em probabilidade real.

## Compatibilidade

- V5 permanece congelada como golden backup.
- V6 validada permanece referência operacional/paridade.
- V0/V1 continuam preservados.
- V2.2 evolui em paralelo.
- novas métricas não substituem artefatos legados até passarem regressão e backtest.