#!/usr/bin/env python3
"""
Pipeline do caso Atendimento (Gocase & GoBeauté).

Lê data/raw/{conversas,mensagens,pedidos}.csv e gera:
  public/data/analytics.json      -> todos os números do painel (uma única fonte de verdade)
  public/data/conversations.json  -> fila de triagem (mensagens com PII mascarada)
  api/_data/base.json             -> base consultada pelas ferramentas do agente

Uso: python3 scripts/build_dataset.py
"""
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

from _engine import (  # noqa: E402
    AUTONOMIA,
    COST_MAP,
    FATOR_AMOSTRA,
    PEDIDOS_FATURADOS_90D,
    POLITICAS,
    SLA_LIMIT_MIN,
    agente_regras,
    analisar_mensagem,
    biz_minutes,
    bucket_contato,
    mask_pii,
)

RAW_DIR = ROOT / "data" / "raw"
OUT_DIR = ROOT / "public" / "data"
API_DATA = ROOT / "api" / "_data"
OUT_DIR.mkdir(parents=True, exist_ok=True)
API_DATA.mkdir(parents=True, exist_ok=True)

MESES = {"2026-07": "Jul", "2026-08": "Ago", "2026-09": "Set"}

# Palavras que aparecem nas respostas-padrão dos agentes quando eles tratam cada tipo de demanda.
# Usadas para medir se a 2ª demanda de uma mensagem multi-problema recebeu alguma resposta.
SINAL_RESPOSTA = {
    "Rastreio": ["trânsito", "transportadora", "rastreio"],
    "Atraso": ["trânsito", "transportadora", "atraso"],
    "Produto faltando": ["reenvio", "faltante", "brinde"],
    "Avaria": ["foto", "reenvio", "produção"],
    "Troca": ["troca"],
    "Cancelamento": ["cancelamento", "cancelar"],
    "Reclamação produto": ["fragrância", "suspender", "cupom"],
    "Financeiro": ["financeiro", "estorno", "pagamento"],
    "Dúvida": ["produto", "especificaç", "informaç"],
    "Promoção": ["cupom", "promoção", "cashback"],
    "Endereço": ["alterar", "endereço"],
    "Elogio": ["obrigad", "feliz"],
}

FRENTES = [
    {
        "id": "logistica",
        "nome": "Rastreio e atraso de entrega",
        "dono": "Logística",
        "acao": "Aviso proativo a cada movimentação e SLA contratual com a Rota Express",
        "filtro": lambda c: c["tag"] in ("Rastreio", "Atraso"),
    },
    {
        "id": "fulfillment",
        "nome": "Item e brinde faltando",
        "dono": "CD / Fulfillment",
        "acao": "Conferência do brinde no picking da campanha e foto obrigatória antes do reenvio",
        "filtro": lambda c: c["tag"] == "Produto faltando",
    },
    {
        "id": "qualidade",
        "nome": "Avaria, vazamento e personalização",
        "dono": "Qualidade / Produção Gocase",
        "acao": "Trocar a tampa do APC-1067 e inspecionar impressão antes da expedição",
        "filtro": lambda c: c["tag"] == "Avaria",
    },
    {
        "id": "autosservico",
        "nome": "Cancelamento, endereço, troca, cupom e dúvida",
        "dono": "CX Digital",
        "acao": "Autoatendimento com o agente: as políticas são determinísticas",
        "filtro": lambda c: c["tag"] in ("Cancelamento", "Endereço", "Troca", "Dúvida", "Promoção"),
    },
    {
        "id": "financeiro",
        "nome": "Cobrança duplicada e Pix não conciliado",
        "dono": "Pagamentos",
        "acao": "Idempotência no checkout e conciliação automática de Pix",
        "filtro": lambda c: c["tag"] == "Financeiro",
    },
    {
        "id": "fragrancia",
        "nome": "Insatisfação com fragrância",
        "dono": "CX / GoBeauté",
        "acao": "Aplicar a política de troca (hoje 100% recebem cupom)",
        "filtro": lambda c: c["sub"] == "Insatisfação com Fragrância / Fixação",
    },
    {
        "id": "bot",
        "nome": "Conversas vazias no bot",
        "dono": "CX / Bot",
        "acao": "Encerrar automaticamente e tirar da contagem de contatos",
        "filtro": lambda c: c["tag"] == "Sem mensagem",
    },
]


def r2(x):
    return round(x, 2)


def pct(a, b):
    return round(a / b * 100, 1) if b else 0.0


def main():
    with open(RAW_DIR / "conversas.csv", encoding="utf-8") as f:
        convs = list(csv.DictReader(f))
    with open(RAW_DIR / "pedidos.csv", encoding="utf-8") as f:
        peds = {p["order_id"]: p for p in csv.DictReader(f)}
    msgs_by_conv = defaultdict(list)
    n_msgs = 0
    with open(RAW_DIR / "mensagens.csv", encoding="utf-8") as f:
        for m in csv.DictReader(f):
            msgs_by_conv[m["conversation_id"]].append(m)
            n_msgs += 1
    convs.sort(key=lambda c: c["data_abertura"])
    N = len(convs)

    # ---------------------------------------------------------------------------------------
    # 1ª passada: classificação de cada conversa
    # ---------------------------------------------------------------------------------------
    rows = []
    seen_order = Counter()
    for c in convs:
        cid, oid = c["conversation_id"], c["order_id"]
        ms = sorted(msgs_by_conv[cid], key=lambda m: m["timestamp"])
        c_msgs = [m for m in ms if m["autor"] == "cliente"]
        a_msgs = [m for m in ms if m["autor"] == "agente"]
        texto = c_msgs[0]["texto"] if c_msgs else ""
        an = analisar_mensagem(texto)
        if an["clauses"]:
            tag, sub, urg = an["clauses"][0][0], an["clauses"][0][1], an["clauses"][0][5]
        else:
            tag, sub, urg = "Sem mensagem", "Abandono no Bot (Sem Resposta do Cliente)", "baixa"
        cosm = any(cl[1] == "Reação Adversa (Cosmetovigilância)" for cl in an["clauses"])
        if cosm:
            urg = "critica"
        recontato_pedido = seen_order[oid] > 0
        seen_order[oid] += 1

        resp_min = None
        sla_breach = None
        if c_msgs and a_msgs:
            t0 = datetime.fromisoformat(c_msgs[0]["timestamp"])
            t1 = datetime.fromisoformat(a_msgs[0]["timestamp"])
            resp_min = biz_minutes(t0, t1) if c["canal"] == "whatsapp" else round((t1 - t0).total_seconds() / 60, 1)
            sla_breach = resp_min > SLA_LIMIT_MIN[c["canal"]]
        hora = int(c["data_abertura"][11:13])
        bucket, bucket_motivo = bucket_contato(sub, tag, recontato_pedido)

        rows.append(
            {
                "c": c,
                "ms": ms,
                "c_msgs": c_msgs,
                "a_msgs": a_msgs,
                "an": an,
                "tag": tag,
                "sub": sub,
                "urg": urg,
                "cosm": cosm,
                "custo": COST_MAP[c["canal"]],
                "ped": peds.get(oid, {}),
                "resp_min": resp_min,
                "sla_breach": sla_breach,
                "fora_horario": hora < 8 or hora >= 20,
                "recontato_pedido": recontato_pedido,
                "bucket": bucket,
                "bucket_motivo": bucket_motivo,
            }
        )

    # ---------------------------------------------------------------------------------------
    # Base do agente (pedidos + índice de conversas + alertas por SKU)
    # ---------------------------------------------------------------------------------------
    alertas_sku = defaultdict(Counter)
    conv_index = {}
    por_cliente, por_pedido = defaultdict(list), defaultdict(list)
    for r in rows:
        c = r["c"]
        sku = r["ped"].get("sku")
        for cl in r["an"]["clauses"]:
            if sku and cl[1] in ("Vazamento de Cosmético (Lote/Embalagem)", "Dano Físico no Transporte / Quebra", "Reação Adversa (Cosmetovigilância)", "Erro de Personalização Gocase"):
                alertas_sku[sku][cl[1]] += 1
        item = {
            "conversation_id": c["conversation_id"],
            "customer_id": c["customer_id"],
            "order_id": c["order_id"],
            "marca": c["marca"],
            "canal": c["canal"],
            "data_abertura": c["data_abertura"],
            "status": c["status"],
            "tag_ia": r["tag"],
            "sub_ia": r["sub"],
            "texto": r["an"]["texto_mascarado"],
            "pii": r["an"]["pii"],
        }
        conv_index[c["conversation_id"]] = item
        por_cliente[c["customer_id"]].append(item)
        por_pedido[c["order_id"]].append(item)
    base = {
        "pedidos": peds,
        "conversas": conv_index,
        "por_cliente": dict(por_cliente),
        "por_pedido": dict(por_pedido),
        "alertas_sku": {k: dict(v) for k, v in alertas_sku.items()},
    }

    # ---------------------------------------------------------------------------------------
    # 2ª passada: agente em modo regras sobre as 4.000 conversas + auditoria dos agentes humanos
    # ---------------------------------------------------------------------------------------
    violacoes = Counter()
    violacoes_ids = defaultdict(list)
    for r in rows:
        c = r["c"]
        res = agente_regras(
            base,
            {
                "conversation_id": c["conversation_id"],
                "customer_id": c["customer_id"],
                "order_id": c["order_id"],
                "canal": c["canal"],
                "marca": c["marca"],
                "texto": r["c_msgs"][0]["texto"] if r["c_msgs"] else "",
                "agora": datetime.fromisoformat(c["data_abertura"]),
            },
        )
        r["agente"] = res

        a_txt = " ".join(m["texto"] for m in r["a_msgs"]).lower()
        v = []
        if r["c_msgs"] and not r["a_msgs"]:
            v.append("sem_resposta_humana")
        if r["cosm"] and "protocolo" not in a_txt:
            v.append("cosmeto_sem_protocolo")
        if r["tag"] == "Produto faltando" and r["a_msgs"]:
            if "reenvio" in a_txt and "foto" not in a_txt:
                v.append("reenvio_sem_foto")
            elif "reenvio" not in a_txt:
                v.append("faltante_encerrado_sem_tratar")
        if r["sub"] == "Insatisfação com Fragrância / Fixação" and "cupom" in a_txt:
            v.append("fragrancia_cupom_em_vez_de_troca")
        if r["tag"] == "Endereço" and r["ped"].get("status_entrega") != "aguardando_pagamento" and "verificar" in a_txt:
            v.append("endereco_prometido_apos_separacao")
        if len(r["an"]["clauses"]) > 1 and r["a_msgs"]:
            sec = r["an"]["clauses"][1][0]
            prim = r["an"]["clauses"][0][0]
            sinais = [s for s in SINAL_RESPOSTA.get(sec, []) if s not in SINAL_RESPOSTA.get(prim, [])]
            if sinais and not any(s in a_txt for s in sinais):
                v.append("segunda_demanda_ignorada")
        tag_ag = c["tag_agente"] or "(sem tag)"
        validas = [cl[0] for cl in r["an"]["clauses"]] or ["(sem tag)"]
        if r["sub"] == "Erro de Personalização Gocase":
            validas.append("Reclamação produto")
        r["divergencia"] = tag_ag not in validas
        r["tag_agente"] = tag_ag
        r["violacoes"] = v
        for x in v:
            violacoes[x] += 1
            if len(violacoes_ids[x]) < 5:
                violacoes_ids[x].append(c["conversation_id"])

    # ---------------------------------------------------------------------------------------
    # Agregações
    # ---------------------------------------------------------------------------------------
    custo_total = sum(r["custo"] for r in rows)

    # Q1 — motivos
    por_tag = defaultdict(list)
    for r in rows:
        por_tag[r["tag"]].append(r)
    tag_agente_count = Counter(r["tag_agente"] for r in rows)
    demandas = Counter()
    for r in rows:
        for cl in r["an"]["clauses"]:
            demandas[cl[0]] += 1
    motivos = []
    for tag, rs in sorted(por_tag.items(), key=lambda kv: -sum(x["custo"] for x in kv[1])):
        subs = Counter(x["sub"] for x in rs)
        sub_cost = defaultdict(float)
        for x in rs:
            sub_cost[x["sub"]] += x["custo"]
        custo = sum(x["custo"] for x in rs)
        motivos.append(
            {
                "tag": tag,
                "conversas": len(rs),
                "pct_conversas": pct(len(rs), N),
                "demandas": demandas.get(tag, len(rs)),
                "custo": r2(custo),
                "pct_custo": pct(custo, custo_total),
                "tag_agente": tag_agente_count.get(tag, 0),
                "reclame_aqui": sum(1 for x in rs if x["c"]["canal"] == "reclame_aqui"),
                "reabertos": sum(1 for x in rs if x["c"]["status"] == "reaberto"),
                "pendentes": sum(1 for x in rs if x["c"]["status"] == "pendente"),
                "recontato_pedido": sum(1 for x in rs if x["recontato_pedido"]),
                "subcategorias": [
                    {"nome": s, "conversas": n, "custo": r2(sub_cost[s]), "autonomia": AUTONOMIA.get(s, "aprovacao")}
                    for s, n in subs.most_common()
                ],
            }
        )

    # Canais e SLA
    canais = []
    for canal in ("whatsapp", "email", "reclame_aqui"):
        rs = [r for r in rows if r["c"]["canal"] == canal]
        resp = [r["resp_min"] for r in rs if r["resp_min"] is not None]
        fora = [r for r in rs if r["fora_horario"]]
        canais.append(
            {
                "canal": canal,
                "conversas": len(rs),
                "pct_conversas": pct(len(rs), N),
                "custo_unit": COST_MAP[canal],
                "custo": r2(len(rs) * COST_MAP[canal]),
                "pct_custo": pct(len(rs) * COST_MAP[canal], custo_total),
                "sla": {"whatsapp": "30 min (8h–20h)", "email": "24 h", "reclame_aqui": "48 h"}[canal],
                "com_resposta": len(resp),
                "sla_estourado": sum(1 for r in rs if r["sla_breach"]),
                "pct_sla_estourado": pct(sum(1 for r in rs if r["sla_breach"]), len(resp)),
                "p50_resposta_min": round(median(resp), 1) if resp else None,
                "fora_horario": len(fora),
                "sem_resposta_humana": sum(1 for r in rs if r["c_msgs"] and not r["a_msgs"]),
            }
        )

    # Tendência mensal (por data de abertura)
    mensal = defaultdict(Counter)
    for r in rows:
        mensal[r["c"]["data_abertura"][:7]][r["tag"]] += 1
    brinde = Counter()
    for r in rows:
        if any(cl[1] == "Falta de Brinde Promocional" for cl in r["an"]["clauses"]):
            brinde[r["c"]["data_abertura"][:7]] += 1
    tendencia = [
        {"mes": MESES[m], "total": sum(v.values()), "por_tag": dict(v), "brinde_faltando": brinde.get(m, 0)}
        for m, v in sorted(mensal.items())
    ]

    # Q2 — frentes de ataque
    frentes = []
    for fr in FRENTES:
        rs = [r for r in rows if fr["filtro"](r)]
        custo = sum(r["custo"] for r in rs)
        reinc = sum(1 for r in rs if r["c"]["status"] in ("reaberto", "pendente") or r["recontato_pedido"])
        frentes.append(
            {
                "id": fr["id"],
                "nome": fr["nome"],
                "dono": fr["dono"],
                "acao": fr["acao"],
                "conversas": len(rs),
                "custo": r2(custo),
                "pct_custo": pct(custo, custo_total),
                "reclame_aqui": sum(1 for r in rs if r["c"]["canal"] == "reclame_aqui"),
                "pct_nao_resolvido": pct(reinc, len(rs)),
                "score": r2(custo * (1 + reinc / max(1, len(rs)))),
            }
        )
    frentes.sort(key=lambda x: -x["score"])
    cosm_rows = [r for r in rows if r["cosm"]]
    risco = {
        "nome": "Reação adversa sem protocolo de cosmetovigilância",
        "dono": "Responsável técnica",
        "acao": "Protocolo automático em 100% dos relatos; troca deixa de ser oferecida como solução",
        "conversas": len(cosm_rows),
        "sem_protocolo": violacoes["cosmeto_sem_protocolo"],
        "custo": r2(sum(r["custo"] for r in cosm_rows)),
    }

    # Q3 — contatos que poderiam não existir
    baldes = defaultdict(lambda: defaultdict(lambda: {"conversas": 0, "custo": 0.0}))
    for r in rows:
        b = baldes[r["bucket"]][r["bucket_motivo"]]
        b["conversas"] += 1
        b["custo"] += r["custo"]
    q3 = {}
    for k in ("eliminavel", "evitavel", "legitimo"):
        itens = [{"motivo": m, "conversas": v["conversas"], "custo": r2(v["custo"])} for m, v in sorted(baldes[k].items(), key=lambda kv: -kv[1]["conversas"])]
        q3[k] = {
            "conversas": sum(i["conversas"] for i in itens),
            "custo": r2(sum(i["custo"] for i in itens)),
            "itens": itens,
        }

    # Q4 — autonomia (o que o agente fez na simulação das 4.000 conversas)
    aut = defaultdict(lambda: {"conversas": 0, "custo": 0.0})
    acoes = Counter()
    motivos_aut = Counter()
    for r in rows:
        a = r["agente"]
        aut[a["autonomia"]]["conversas"] += 1
        aut[a["autonomia"]]["custo"] += r["custo"]
        for ac in a["acoes"]:
            acoes[ac["tipo"]] += 1
        for m in a["motivos_autonomia"][:1]:
            motivos_aut[m] += 1
    cruz = defaultdict(lambda: {"conversas": 0, "custo": 0.0, "reclame_aqui": 0})
    for r in rows:
        k = f'{r["bucket"]}|{r["agente"]["autonomia"]}'
        cruz[k]["conversas"] += 1
        cruz[k]["custo"] += r["custo"]
        cruz[k]["reclame_aqui"] += r["c"]["canal"] == "reclame_aqui"
    q4 = {
        "cruzamento": {k: {**v, "custo": r2(v["custo"])} for k, v in cruz.items()},
        "niveis": {k: {"conversas": v["conversas"], "custo": r2(v["custo"]), "pct": pct(v["conversas"], N)} for k, v in aut.items()},
        "acoes": dict(acoes.most_common()),
        "motivos_escalonamento": dict(motivos_aut.most_common(8)),
        "por_subcategoria": [
            {"subcategoria": s, "autonomia": a, "conversas": sum(1 for r in rows if r["sub"] == s)} for s, a in AUTONOMIA.items()
        ],
    }

    # Transportadoras (pedidos únicos)
    car = defaultdict(lambda: {"pedidos": 0, "status": Counter(), "ufs": Counter(), "conversas": 0, "logistica": 0, "custo": 0.0})
    seen = set()
    for r in rows:
        p = r["ped"]
        t = p.get("transportadora", "N/A")
        car[t]["conversas"] += 1
        car[t]["custo"] += r["custo"]
        if r["tag"] in ("Rastreio", "Atraso"):
            car[t]["logistica"] += 1
            car[t]["ufs"][p.get("uf", "?")] += 1
        if p and p["order_id"] not in seen:
            seen.add(p["order_id"])
            car[t]["pedidos"] += 1
            car[t]["status"][p["status_entrega"]] += 1
    transportadoras = sorted(
        [
            {
                "nome": t,
                "pedidos": v["pedidos"],
                "conversas": v["conversas"],
                "conversas_logistica": v["logistica"],
                "custo": r2(v["custo"]),
                "atrasado": v["status"]["atrasado"],
                "em_transito": v["status"]["em_transito"],
                "entregue": v["status"]["entregue"],
                "logistica_por_100_pedidos": round(v["logistica"] / max(1, v["pedidos"]) * 100, 1),
                "top_ufs_logistica": v["ufs"].most_common(5),
            }
            for t, v in car.items()
        ],
        key=lambda x: -x["conversas_logistica"],
    )

    # SKUs com problema de qualidade
    sku = defaultdict(lambda: {"conversas": 0, "vazamento": 0, "avaria": 0, "personalizacao": 0, "reacao": 0, "faltando": 0})
    sku_meta = {}
    for r in rows:
        p = r["ped"]
        if not p:
            continue
        s = sku[p["sku"]]
        sku_meta[p["sku"]] = (p["descricao_produto"], p["marca"])
        s["conversas"] += 1
        for cl in r["an"]["clauses"][:1]:
            s["vazamento"] += cl[1] == "Vazamento de Cosmético (Lote/Embalagem)"
            s["avaria"] += cl[1] == "Dano Físico no Transporte / Quebra"
            s["personalizacao"] += cl[1] == "Erro de Personalização Gocase"
            s["faltando"] += cl[0] == "Produto faltando"
        s["reacao"] += r["cosm"]
    skus = sorted(
        [{"sku": k, "produto": sku_meta[k][0], "marca": sku_meta[k][1], **v, "qualidade": v["vazamento"] + v["avaria"] + v["personalizacao"] + v["reacao"]} for k, v in sku.items()],
        key=lambda x: -x["qualidade"],
    )[:12]
    total_vaz = sum(s["vazamento"] for s in sku.values())

    # Marcas
    mar = defaultdict(lambda: {"conversas": 0, "custo": 0.0, "top": Counter()})
    for r in rows:
        m = mar[r["c"]["marca"]]
        m["conversas"] += 1
        m["custo"] += r["custo"]
        m["top"][r["tag"]] += 1
    marcas = sorted(
        [{"marca": k, "grupo": "Gocase" if k == "Gocase" else "GoBeauté", "conversas": v["conversas"], "custo": r2(v["custo"]), "principal_motivo": v["top"].most_common(1)[0][0]} for k, v in mar.items()],
        key=lambda x: -x["custo"],
    )

    # Qualidade da tag manual
    confusao = Counter((r["tag_agente"], r["tag"]) for r in rows if r["divergencia"])
    clientes = Counter(c["customer_id"] for c in convs)

    analytics = {
        "metadata": {
            "periodo": "02/07/2026 a 29/09/2026",
            "dias": 90,
            "data_extracao": "30/09/2026",
            "fator_amostra": FATOR_AMOSTRA,
            "conversas_amostra": N,
            "mensagens_amostra": n_msgs,
            "pedidos_amostra": len(peds),
            "clientes_amostra": len(clientes),
            "clientes_com_mais_de_uma_conversa": sum(1 for v in clientes.values() if v > 1),
            "pedidos_faturados_90d": PEDIDOS_FATURADOS_90D,
            "taxa_contato_pct": pct(N * FATOR_AMOSTRA, PEDIDOS_FATURADOS_90D),
            "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
        },
        "kpis": {
            "custo_amostra": r2(custo_total),
            "conversas_fora_horario": sum(1 for r in rows if r["fora_horario"]),
            "conversas_sem_mensagem": sum(1 for r in rows if not r["c_msgs"]),
            "multi_demanda": sum(1 for r in rows if len(r["an"]["clauses"]) > 1),
            "recontato_mesmo_pedido": sum(1 for r in rows if r["recontato_pedido"]),
            "recontato_explicito": sum(1 for r in rows if r["an"]["recontato_explicito"]),
            "reabertos": sum(1 for r in rows if r["c"]["status"] == "reaberto"),
            "pendentes": sum(1 for r in rows if r["c"]["status"] == "pendente"),
            "cosmetovigilancia": len(cosm_rows),
            "prompt_injection": sum(1 for r in rows if r["an"]["injection"]),
            "conversas_com_pii": sum(1 for r in rows if r["an"]["pii"]),
            "divergencia_tag": sum(1 for r in rows if r["divergencia"]),
            "tag_vazia_ou_outros": tag_agente_count["(sem tag)"] + tag_agente_count["Outros"],
            "vazamentos_total": total_vaz,
        },
        "motivos": motivos,
        "canais": canais,
        "tendencia": tendencia,
        "frentes": frentes,
        "risco_prioritario": risco,
        "q3": q3,
        "q4": q4,
        "transportadoras": transportadoras,
        "skus": skus,
        "marcas": marcas,
        "auditoria": {
            "violacoes": dict(violacoes),
            "aplicaveis": {
                "cosmeto_sem_protocolo": len(cosm_rows),
                "reenvio_sem_foto": sum(1 for r in rows if r["tag"] == "Produto faltando" and r["a_msgs"]),
                "faltante_encerrado_sem_tratar": sum(1 for r in rows if r["tag"] == "Produto faltando" and r["a_msgs"]),
                "fragrancia_cupom_em_vez_de_troca": sum(1 for r in rows if r["sub"] == "Insatisfação com Fragrância / Fixação"),
                "endereco_prometido_apos_separacao": sum(1 for r in rows if r["tag"] == "Endereço"),
                "segunda_demanda_ignorada": sum(1 for r in rows if len(r["an"]["clauses"]) > 1 and r["a_msgs"]),
                "sem_resposta_humana": sum(1 for r in rows if r["c_msgs"]),
            },
            "exemplos": dict(violacoes_ids),
            "confusao_top": [{"agente": a, "ia": b, "conversas": n} for (a, b), n in confusao.most_common(10)],
        },
        "politicas": POLITICAS,
        "premissas_roi": {
            "eliminavel": 0.60,
            "evitavel": 0.30,
            "contencao_autonomo": 0.70,
            "reducao_esforco_aprovacao": 0.40,
            "nota": "Premissas conservadoras e ajustáveis no painel. Custos por contato vêm de parametros.md.",
        },
    }

    # Fila de triagem (PII mascarada nas mensagens)
    fila = []
    for r in rows:
        c, p, a = r["c"], r["ped"], r["agente"]
        prio = {"critica": 100, "alta": 60, "media": 40, "baixa": 20}[r["urg"]]
        if r["an"]["injection"]:
            prio = max(prio, 90)
        prio += 25 if c["canal"] == "reclame_aqui" else 0
        prio += 10 if c["status"] in ("pendente", "reaberto") else 0
        prio += 8 if r["recontato_pedido"] or r["an"]["recontato_explicito"] else 0
        prio += 5 if len(r["an"]["clauses"]) > 1 else 0
        fila.append(
            {
                "id": c["conversation_id"],
                "cliente": c["customer_id"],
                "pedido": c["order_id"],
                "marca": c["marca"],
                "canal": c["canal"],
                "abertura": c["data_abertura"],
                "status": c["status"],
                "custo": r["custo"],
                "tag_agente": r["tag_agente"],
                "tag": r["tag"],
                "sub": r["sub"],
                "tag2": r["an"]["clauses"][1][0] if len(r["an"]["clauses"]) > 1 else None,
                "sub2": r["an"]["clauses"][1][1] if len(r["an"]["clauses"]) > 1 else None,
                "urgencia": r["urg"],
                "prioridade": min(100, prio),
                "balde": r["bucket"],
                "autonomia": a["autonomia"],
                "acoes": [ac["tipo"] for ac in a["acoes"]],
                "flags": {
                    "cosmeto": r["cosm"],
                    "injection": bool(r["an"]["injection"]),
                    "pii": r["an"]["pii"],
                    "multi": len(r["an"]["clauses"]) > 1,
                    "recontato": r["recontato_pedido"] or r["an"]["recontato_explicito"],
                    "sla": r["sla_breach"],
                    "fora_horario": r["fora_horario"],
                    "divergencia": r["divergencia"],
                    "violacoes": r["violacoes"],
                },
                "resp_min": r["resp_min"],
                "ped": {k: p.get(k) for k in ("data_pedido", "valor", "sku", "descricao_produto", "transportadora", "uf", "status_entrega")} if p else None,
                "msgs": [[m["autor"], m["timestamp"], mask_pii(m["texto"])[0]] for m in r["ms"]],
            }
        )
    fila.sort(key=lambda x: (-x["prioridade"], x["abertura"]))

    with open(OUT_DIR / "analytics.json", "w", encoding="utf-8") as f:
        json.dump(analytics, f, ensure_ascii=False, indent=1)
    with open(OUT_DIR / "conversations.json", "w", encoding="utf-8") as f:
        json.dump(fila, f, ensure_ascii=False, separators=(",", ":"))
    with open(API_DATA / "base.json", "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False, separators=(",", ":"))

    for p in (OUT_DIR / "analytics.json", OUT_DIR / "conversations.json", API_DATA / "base.json"):
        print(f"  {p.relative_to(ROOT)}: {p.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
