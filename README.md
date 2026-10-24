# Atendimento AI-First · Gocase & GoBeauté

Solução do **Problema 1 (Atendimento)** do Desafio AI-First GoGroup: um diagnóstico das 4.000 conversas da amostra e um **agente operacional** que resolve a fila com ferramentas. O agente consulta o pedido, aplica a política, abre protocolo, cria reenvio e escala com fila, e as regras de negócio ficam no código, não no prompt.

- **Painel:** 9 telas. São quatro perguntas do case, a fila de triagem, um playground do agente, causa raiz e qualidade/método.
- **Agente:** LLM com tool calling (Groq `openai/gpt-oss-120b`) e 19 ferramentas. O modelo escolhe as ferramentas e redige a resposta, e as políticas ficam dentro das ferramentas, então ele não consegue aprovar o que a regra nega. Sem chave, um motor de regras executa as mesmas ferramentas, e o painel funciona sem login.
- **Stack:** Python 3 só com a biblioteca padrão (servidor local e função Vercel) e frontend em HTML/CSS/JS sem dependências.

## Números principais

A amostra é de 1 em cada 3 clientes em 90 dias, então o total da amostra corresponde a cerca de **1 mês** da população.

| | Amostra |
|---|---|
| Conversas / mensagens / pedidos | 4.000 / 14.559 / 3.237 |
| Custo do atendimento | R$ 36.099,60 por mês |
| Taxa de contato | 7,2% (12.000 conversas / 166.000 pedidos em 90 dias) |
| Reclame Aqui | 10,2% do volume e **47,4% do custo** |
| Tag do atendente diverge da mensagem | 1.095 (27,4%) |

### 1. Por que os clientes procuram

**Entrega domina.** Rastreio (871 conversas) e atraso (498) somam 34,2% das conversas e 37,7% do custo. Em seguida vêm avaria (431) e produto faltando (429).

Há também 342 conversas com duas demandas na mesma mensagem. O peso real considera o custo por canal e essas demandas secundárias.

### 2. O que atacar primeiro

O critério é **custo mensal × (1 + taxa de não resolução)**. A não resolução conta conversas pendentes, reabertas ou com recontato no mesmo pedido.

Antes do ranking vem o risco à saúde: 40 relatos de reação adversa, **40 sem protocolo de cosmetovigilância**.

| # | Frente | Conversas | Custo/mês | Sem resolução |
|---|---|---|---|---|
| 1 | Rastreio e atraso (Rota Express: 77 contatos por 100 pedidos, contra cerca de 24 nas demais transportadoras) | 1.369 | R$ 13.579 | 54,5% |
| 2 | Autosserviço (cancelamento, endereço, troca, cupom, dúvida) | 839 | R$ 7.550 | 41,1% |
| 3 | Item e brinde faltando (brinde: 20 casos em agosto, 121 em setembro) | 429 | R$ 3.626 | 58,0% |
| 4 | Avaria, vazamento e personalização (APC-1067 tem 57 dos 139 vazamentos) | 431 | R$ 3.727 | 50,8% |

### 3. Contatos que poderiam não existir

Cada conversa entra em um único grupo, sem dupla contagem:

- **Não precisariam existir: 2.344 conversas (58,6%), R$ 21.647 por mês.**
  - rastreio com pedido em trânsito: 871;
  - autoatendimento: 583;
  - conversa vazia no bot: 459;
  - recontato no mesmo pedido: 431.
- **Evitáveis na origem: 1.309 (32,7%), R$ 11.168.**
  - atraso da transportadora;
  - avaria e vazamento;
  - falha de pagamento e cupom;
  - erro de separação ou brinde.
- **Atendimento necessário: 347 (8,7%).**

### 4. O que o sistema faz sozinho e o que precisa de pessoa

A regra de corte é o risco. **Autônomo** quando não há dinheiro, custo de produto nem saúde envolvidos. **Aprovação de 1 clique** para reenvio com foto, estorno, troca com validação ou resposta pública. **Humano obrigatório** para reação adversa.

Rodando o agente nas 4.000 conversas:

| Nível | Conversas |
|---|---|
| Autônomo | 1.479 (37%) |
| Com aprovação | 2.481 (62%) |
| Humano | 40 (1%) |

O agente gerou 1.562 escalonamentos com fila, 1.301 pedidos de evidência e 1.103 chamados na transportadora. O simulador do painel estima a economia a partir do custo real de cada célula grupo × autonomia.

### Auditoria do atendimento atual

| Falha | Casos |
|---|---|
| Reenvio de item faltante prometido sem foto | 429 de 429 |
| Segunda demanda da mensagem ignorada | 335 de 342 |
| Cupom no lugar da troca de fragrância | 145 de 145 |
| Troca de endereço prometida com pedido já em separação | 115 de 115 |
| Reação adversa sem protocolo de cosmetovigilância | 40 de 40 |

## Proteções do agente

- **PII:** CPF, cartão, telefone e e-mail são mascarados antes do LLM e no frontend.
- **Prompt injection:** as 6 tentativas da base são detectadas. A instrução injetada é ignorada, ações sensíveis ficam retidas para revisão e o caso vai para a fila Segurança.
- **Reação adversa:** o protocolo é aberto mesmo que o LLM não o chame. O caso nunca fica autônomo.
- **Reclame Aqui:** a resposta pública sempre passa por revisão.
- **Falha do LLM:** se o provedor falhar, o agente cai no motor de regras e a resposta traz um aviso.

## Como o agente LLM roda

1. As proteções rodam antes do modelo: mascaramento de PII, detecção de injeção e pré-classificação das demandas.
2. O sistema carrega o pedido e o histórico do cliente e os entrega prontos ao modelo, com origem `contexto` no passo a passo.
3. O modelo recebe só as ferramentas relevantes para as demandas detectadas. Demanda não mapeada recebe o catálogo inteiro.
4. O modelo chama as ferramentas e termina com `responder_cliente`.
5. As proteções rodam de novo depois do modelo: protocolo obrigatório, retenção de ação sensível e fila de Segurança ou Reclame Aqui.

Os passos 2 e 3 reduzem os tokens por conversa. O plano gratuito da Groq limita a 8 mil tokens por minuto, e um erro 429 é repetido com espera. O lote de 4.000 conversas roda no motor de regras, que é determinístico e não gasta tokens.

## Dados e confidencialidade

O enunciado pede para não compartilhar os dados do desafio. Por isso, os CSVs originais (`data/raw/`) **não estão no repositório**.

Versionados ficam só os JSONs derivados de que o painel e a função precisam para funcionar sem login (`public/data/` e `api/_data/`). Neles, CPF, cartão, telefone e e-mail já vêm mascarados.

Para regerar esses JSONs, coloque os CSVs em `data/raw/` e rode o pipeline.

## Como rodar

```bash
python3 scripts/build_dataset.py   # regera public/data e api/_data a partir de data/raw (≈1 s)
python3 dev_server.py              # http://localhost:8080
```

Para o modo LLM, crie um `.env` com `GROQ_API_KEY=...` (chave gratuita em console.groq.com). O modelo pode ser trocado com `GROQ_MODEL`, e `OPENAI_API_KEY` também funciona. No playground, dá para colar uma chave que fica só na memória da aba.

## Deploy na Vercel

O `vercel.json` já está configurado. Os arquivos estáticos ficam em `public/`, e a função Python fica em `api/agent.py`, com `api/_engine.py` e `api/_data/`. Para publicar, importe o repositório na Vercel e defina `GROQ_API_KEY` nas variáveis de ambiente. Não há etapa de build.

## Estrutura

```
api/_engine.py         motor: classificação, políticas, 19 ferramentas, guardrails, agente de regras
api/agent.py           função HTTP: agente LLM (tool calling), triagem, lote, status
scripts/build_dataset.py  pipeline CSV → analytics.json, conversations.json, base do agente
dev_server.py          servidor local (estático + /api/agent)
public/                painel (index.html, styles.css, app.js, data/)
data/raw/              CSVs do desafio (fora do repositório)
```
