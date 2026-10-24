"""
Motor compartilhado do caso Atendimento (Gocase & GoBeauté).

Usado por dois consumidores:
  - scripts/build_dataset.py  -> classificação das 4.000 conversas e métricas do painel
  - api/agent.py              -> agente operacional (modo LLM com tool calling ou modo regras)

Tudo aqui é Python puro (stdlib), para rodar igual no servidor local e na função serverless da Vercel.
As ferramentas do agente aplicam as políticas de parametros.md no código: o LLM decide QUAIS
ferramentas chamar e redige a resposta, mas não consegue burlar uma regra de negócio.
"""
import re
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------------------------
# Parâmetros de negócio (parametros.md)
# --------------------------------------------------------------------------------------------
COST_MAP = {"whatsapp": 4.80, "email": 7.20, "reclame_aqui": 42.00}
SLA_LIMIT_MIN = {"whatsapp": 30.0, "email": 1440.0, "reclame_aqui": 2880.0}
DATA_EXTRACAO = date(2026, 9, 30)
FATOR_AMOSTRA = 3  # amostra de 1 em cada 3 clientes
PEDIDOS_FATURADOS_90D = 70000 + 96000

POLITICAS = {
    "arrependimento": {
        "titulo": "Arrependimento (CDC, art. 49)",
        "texto": "Até 7 dias do recebimento, reembolso integral.",
    },
    "avaria_faltante": {
        "titulo": "Avaria, vazamento ou item faltante",
        "texto": "Reenvio sem custo, mediante foto, em até 7 dias do recebimento.",
    },
    "fragrancia": {
        "titulo": "Troca por insatisfação com fragrância",
        "texto": "1 troca por pedido, em até 30 dias, com no máximo 20% do produto utilizado.",
    },
    "cobranca_duplicada": {
        "titulo": "Cobrança duplicada",
        "texto": "Estorno em até 2 faturas (cartão) ou 5 dias úteis (pix).",
    },
    "cosmetovigilancia": {
        "titulo": "Reação adversa a cosmético ou suplemento",
        "texto": (
            "Orientar a suspensão do uso, registrar no protocolo de cosmetovigilância (produto, lote, descrição e fotos) "
            "em até 24 horas e encaminhar para a responsável técnica. A troca do produto não substitui o registro."
        ),
    },
    "endereco": {
        "titulo": "Alteração de endereço",
        "texto": "Possível somente antes da separação do pedido.",
    },
    "personalizacao": {
        "titulo": "Erro de personalização Gocase",
        "texto": (
            "Nome, foto ou iniciais diferentes do pedido, impressão borrada ou torta: nova produção sem custo, "
            "mediante foto, em até 7 dias do recebimento."
        ),
    },
}

# Autonomia por subcategoria (resposta à pergunta 4). Um único mapa, usado pelo painel e pelo agente.
#   autonomo  -> o sistema resolve sozinho, sem pessoa no loop
#   aprovacao -> o agente executa a parte operacional e deixa 1 decisão para uma pessoa (custo, dinheiro, evidência)
#   humano    -> pessoa obrigatória por risco (saúde, jurídico, reputação)
AUTONOMIA = {
    "Abandono no Bot (Sem Resposta do Cliente)": "autonomo",
    "Solicitação de Código/Status de Rastreio": "autonomo",
    "Rastreio Sem Atualização / Em Trânsito Longo": "autonomo",
    "Pedido Parado em Hub Regional": "autonomo",
    "Alteração Cadastral de Endereço": "autonomo",
    "Arrependimento / Pedido Duplicado pelo Cliente": "autonomo",
    "Troca por Erro de Escolha (Tamanho/Modelo/Cor)": "autonomo",
    "Elogio / Promotor da Marca": "autonomo",
    "Dúvida Técnica / Pré-Venda de Produto": "autonomo",
    "SLA de Entrega Estourado": "aprovacao",
    "Atraso Crítico (>10 dias / Recorrente)": "aprovacao",
    "Item Principal / Kit Incompleto": "aprovacao",
    "Falta de Brinde Promocional": "aprovacao",
    "Dano Físico no Transporte / Quebra": "aprovacao",
    "Vazamento de Cosmético (Lote/Embalagem)": "aprovacao",
    "Erro de Personalização Gocase": "aprovacao",
    "Insatisfação com Fragrância / Fixação": "aprovacao",
    "Cobrança Duplicada (Cartão / Pix / Boleto)": "aprovacao",
    "Pagamento Não Conciliado (Pix / Boleto)": "aprovacao",
    "Falha de Cupom / Cashback / Frete Grátis": "aprovacao",
    "Troca / Devolução de Produto": "aprovacao",
    "Reação Adversa (Cosmetovigilância)": "humano",
}
AUTONOMIA_ORDEM = {"autonomo": 0, "aprovacao": 1, "humano": 2}

# Pergunta 3: cada conversa cai em um único balde, na ordem abaixo.
#   eliminavel -> o contato não precisaria existir (o cliente só queria informação ou o sistema falhou em silêncio)
#   evitavel   -> o contato existe por falha operacional na origem; some quando a causa raiz é corrigida
#   legitimo   -> o contato é esperado e precisa de atendimento
def bucket_contato(sub_ia, tag_ia, is_recontato_mesmo_pedido):
    if sub_ia == "Abandono no Bot (Sem Resposta do Cliente)":
        return "eliminavel", "Conversa vazia (cliente não escreveu)"
    if tag_ia == "Rastreio":
        return "eliminavel", "\"Onde está meu pedido?\" com pedido em trânsito"
    if is_recontato_mesmo_pedido:
        return "eliminavel", "Recontato sobre o mesmo pedido"
    if tag_ia in ("Dúvida", "Endereço", "Cancelamento") or sub_ia == "Troca por Erro de Escolha (Tamanho/Modelo/Cor)":
        return "eliminavel", "Autoatendimento (dúvida, endereço, cancelamento, troca)"
    if tag_ia == "Atraso":
        return "evitavel", "Atraso da transportadora"
    if tag_ia == "Produto faltando":
        return "evitavel", "Falha de separação / brinde"
    if tag_ia == "Avaria":
        return "evitavel", "Avaria, vazamento e personalização"
    if tag_ia in ("Financeiro", "Promoção"):
        return "evitavel", "Pagamento e cupom"
    return "legitimo", "Atendimento necessário"


# --------------------------------------------------------------------------------------------
# Utilidades de texto: recontato, PII (LGPD) e prompt injection
# --------------------------------------------------------------------------------------------
RE_RECONTATO = re.compile(r"^(De novo aqui\. |Ninguém me respondeu ainda! |Já é a \d+ª vez que entro em contato\. )")
RE_PII = [
    ("CPF", re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"), "[CPF]"),
    ("Cartão", re.compile(r"\b(?:\d{4}[\s\-]?){3}\d{4}\b"), "[CARTÃO]"),
    ("Telefone", re.compile(r"\(\d{2}\)\s?\d{4,5}-?\d{4}"), "[TELEFONE]"),
    ("E-mail", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[E-MAIL]"),
]
RE_INJECTION = re.compile(
    # A instrução injetada vai até o fim da frase; o resto da mensagem continua sendo tratado como demanda.
    r"((?:SYSTEM OVERRIDE:|Instrução para o assistente virtual:|Ignore (?:todas as|as) instruções|\[SISTEMA\]|"
    r"Olá robô,|Se você for uma IA|ignore (?:all )?previous instructions|você agora é |aprove (?:o )?reembolso)[^.!?]*[.!?]?)",
    re.I,
)
RE_SPLIT = re.compile(r" (?:E outra coisa: |Ah, e também |Aproveitando, )")


def mask_pii(text):
    found = []
    for name, rx, token in RE_PII:
        if rx.search(text):
            found.append(name)
            text = rx.sub(token, text)
    return text, found


def detect_injection(text):
    m = RE_INJECTION.search(text)
    if not m:
        return None, text
    return m.group(1), (text[: m.start()] + text[m.end():]).strip()


def analisar_mensagem(raw):
    """Separa a mensagem do cliente em demandas e levanta os sinais de risco antes de qualquer decisão."""
    raw = (raw or "").strip()
    recontato = bool(RE_RECONTATO.search(raw))
    masked, pii = mask_pii(raw)
    injection, clean = detect_injection(masked)
    clean = RE_RECONTATO.sub("", clean)
    clean = re.sub(r"\s*(Meu CPF é|O cartão cobrado foi o|Me liga no)\s*\[[^\]]+\]\.?\s*$", "", clean).strip()
    parts = [p.strip() for p in RE_SPLIT.split(clean) if p.strip()]
    clauses = [classify_clause(p) for p in parts]
    return {
        "texto_mascarado": masked,
        "texto_limpo": clean,
        "partes": parts,
        "clauses": clauses,
        "pii": pii,
        "injection": injection,
        "recontato_explicito": recontato,
    }


def biz_minutes(t0, t1):
    """Minutos dentro da janela de atendimento humano (8h às 20h, todos os dias)."""
    if t1 <= t0:
        return 0.0
    cur, mins = t0, 0.0
    while cur < t1:
        day_start = cur.replace(hour=8, minute=0, second=0)
        day_end = cur.replace(hour=20, minute=0, second=0)
        if cur < day_start:
            cur = day_start
            continue
        if cur >= day_end:
            cur = (cur + timedelta(days=1)).replace(hour=8, minute=0, second=0)
            continue
        step_end = min(t1, day_end)
        mins += (step_end - cur).total_seconds() / 60.0
        cur = step_end
    return round(mins, 1)


# --------------------------------------------------------------------------------------------
# Classificador de demandas (regras por palavra-chave, auditável)
# --------------------------------------------------------------------------------------------
def classify_clause(p: str):
    """Classifica uma demanda atômica. Retorna (tag, subcategoria, tier_legado, evitavel, causa_raiz, urgencia)."""
    pl = p.lower().strip()

    # 1. Rastreio vs Atraso
    if any(
        k in pl
        for k in [
            "ainda não chegou, consegue ver onde está",
            "em trânsito desde",
            "rastreio do meu pedido não atualiza",
            "cadê meu pedido",
            "parado em ",
            "código de rastreio",
            "status do pedido",
            "olá, comprei o ",
        ]
    ) and not any(
        w in pl
        for w in [
            "atrasado",
            "venceu",
            "passou do prazo",
            "prazo estourado",
            "prometeram entregar",
        ]
    ):
        if "parado em " in pl:
            sub = "Pedido Parado em Hub Regional"
        elif any(
            x in pl
            for x in [
                "código de rastreio",
                "status do pedido",
                "consegue ver onde está",
            ]
        ):
            sub = "Solicitação de Código/Status de Rastreio"
        else:
            sub = "Rastreio Sem Atualização / Em Trânsito Longo"
        return (
            "Rastreio",
            sub,
            "automacao_total",
            True,
            "Logística & Rastreio Passivo",
            "baixa",
        )

    if any(
        k in pl
        for k in [
            "prazo do meu pedido",
            "passou do prazo",
            "atrasado de novo",
            "atrasado mais de 10 dias",
            "prometeram entregar até",
            "prazo estourado",
        ]
    ):
        sub = (
            "Atraso Crítico (>10 dias / Recorrente)"
            if ("10 dias" in pl or "de novo" in pl)
            else "SLA de Entrega Estourado"
        )
        return (
            "Atraso",
            sub,
            "hibrido_copilot",
            True,
            "Falha de Entrega (Rota Express / Nordeste)",
            "alta",
        )

    # 2. Produto faltando
    if any(k in pl for k in ["faltou o brinde", "mimo/brinde da campanha"]):
        return (
            "Produto faltando",
            "Falta de Brinde Promocional",
            "hibrido_copilot",
            True,
            "Falha de Fulfillment (Brinde Campanha Ago/Set)",
            "media",
        )
    if any(
        k in pl
        for k in [
            "faltou produto no meu pedido",
            "kit chegou incompleto",
            "chegou só o ",
            "veio faltando um item",
        ]
    ):
        return (
            "Produto faltando",
            "Item Principal / Kit Incompleto",
            "hibrido_copilot",
            True,
            "Erro de Separação / Picking no CD",
            "alta",
        )

    # 3. Avaria vs Erro de Personalização Gocase
    if any(
        k in pl
        for k in [
            "personalização manchada",
            "impressão descascando",
            "nome no meu ",
            "impressão do nome",
            "impressão das iniciais",
            "impressão do ",
            "iniciais no meu ",
            "pedi as iniciais",
            "foto no meu ",
            "foto do ",
            "mandei uma foto minha",
            "personalização impecável",
        ]
    ):
        return (
            "Avaria",
            "Erro de Personalização Gocase",
            "hibrido_copilot",
            True,
            "Falha de Qualidade Produção Gocase",
            "alta",
        )

    if any(
        k in pl
        for k in [
            "chegou vazando",
            "produto vazou",
            "lacre violado",
            "tampa quebrada",
            "frasco do ",
            "veio trincado",
            "chegou trincado",
            "todo arranhado",
            "todo riscado",
            "borda rachada",
            "zíper quebrado",
        ]
    ):
        sub = (
            "Vazamento de Cosmético (Lote/Embalagem)"
            if ("vazando" in pl or "vazou" in pl or "lacre violado" in pl)
            else "Dano Físico no Transporte / Quebra"
        )
        return (
            "Avaria",
            sub,
            "hibrido_copilot",
            True,
            "Embalagem / Avaria Física (APC-1067 1L & Transporte)",
            "alta",
        )

    # 4. Reação Adversa (Cosmetovigilância) vs Reclamação Produto / Fragrância
    if any(
        k in pl
        for k in [
            "rosto inchado",
            "bolinhas no rosto",
            "enjoo e manchas vermelhas",
            "coceira forte",
            "reação alérgica",
            "queimação forte",
            "queimou meu couro cabeludo",
            "pele ficou vermelha e ardendo",
        ]
    ):
        return (
            "Reclamação produto",
            "Reação Adversa (Cosmetovigilância)",
            "humano_critico",
            False,
            "Segurança do Consumidor / Cosmetovigilância",
            "critica",
        )

    if any(
        k in pl
        for k in [
            "não gostei do cheiro",
            "esperava mais do ",
            "fragrância do ",
            "cheiro some em 1 hora",
        ]
    ):
        return (
            "Reclamação produto",
            "Insatisfação com Fragrância / Fixação",
            "hibrido_copilot",
            False,
            "Expectativa Sensorial / Fragrância",
            "media",
        )

    # 5. Troca
    if any(
        k in pl
        for k in [
            "gostaria de trocar",
            "quero trocar o ",
            "como faço pra devolver",
            "comprei o tamanho errado",
            "comprei o modelo de celular errado",
            "escolhi a cor errada",
        ]
    ):
        sub = (
            "Troca por Erro de Escolha (Tamanho/Modelo/Cor)"
            if ("errado" in pl or "errada" in pl)
            else "Troca / Devolução de Produto"
        )
        return (
            "Troca",
            sub,
            "automacao_total",
            False,
            "Autoatendimento de Trocas e Devoluções",
            "baixa",
        )

    # 6. Cancelamento
    if any(
        k in pl
        for k in [
            "comprei sem querer duas vezes",
            "desisti da compra",
            "cancelar para fazer uma nova compra",
            "me arrependi da compra",
            "quero cancelar o pedido",
        ]
    ):
        return (
            "Cancelamento",
            "Arrependimento / Pedido Duplicado pelo Cliente",
            "automacao_total",
            True,
            "Fricção no Checkout / Cancelamento Self-Service",
            "media",
        )

    # 7. Financeiro
    if any(
        k in pl
        for k in [
            "cobrança no meu cartão que eu não reconheço",
            "cobrada duas vezes no cartão",
            "paguei o boleto e o valor foi cobrado de novo",
            "paguei com pix",
            "boleto foi pago e não liberaram",
            "pix foi pago e o pedido continua",
        ]
    ):
        sub = (
            "Cobrança Duplicada (Cartão / Pix / Boleto)"
            if ("duas vezes" in pl or "de novo" in pl or "não reconheço" in pl)
            else "Pagamento Não Conciliado (Pix / Boleto)"
        )
        return (
            "Financeiro",
            sub,
            "hibrido_copilot",
            True,
            "Falha de Gateway / Conciliação de Pagamento",
            "alta",
        )

    # 8. Promoção
    if any(
        k in pl
        for k in [
            "promoção do site não aplicou",
            "cupom primeira10 não funciona",
            "frete grátis não apareceu",
            "não recebi o cashback",
        ]
    ):
        return (
            "Promoção",
            "Falha de Cupom / Cashback / Frete Grátis",
            "automacao_total",
            True,
            "Regras de Promoção e Carrinho",
            "baixa",
        )

    # 9. Endereço
    if any(
        k in pl
        for k in [
            "coloquei o endereço errado",
            "mudar o endereço de entrega",
            "mudar o número da casa",
        ]
    ):
        return (
            "Endereço",
            "Alteração Cadastral de Endereço",
            "automacao_total",
            True,
            "Autoatendimento de Alteração de Endereço",
            "media",
        )

    # 10. Dúvida
    if any(
        k in pl
        for k in [
            "é vegano",
            "diferença entre o ",
            "cabelo com química",
            "validade do ",
            "durante a gravidez",
            "pele oleosa",
            "quantas vezes por semana",
            "aguenta queda",
            "quanto tempo leva pra personalizar",
            "durabilidade da impressão",
            "consigo mudar o nome",
            "consigo mudar as iniciais",
            "carregador por indução",
            "espaço para a caneta",
            "mantém a bebida gelada",
        ]
    ):
        return (
            "Dúvida",
            "Dúvida Técnica / Pré-Venda de Produto",
            "automacao_total",
            True,
            "FAQ Inteligente / Assistente Pré-Venda na Página de Produto",
            "baixa",
        )

    # 11. Elogio
    if any(
        k in pl
        for k in [
            "amei o ",
            "passando pra agradecer",
            "é perfeito, já quero o kit",
        ]
    ):
        return (
            "Elogio",
            "Elogio / Promotor da Marca",
            "automacao_total",
            False,
            "Engajamento e Fidelização",
            "baixa",
        )

    return _classificar_generico(pl)


# Camada genérica para texto livre (playground, mensagens novas). As regras acima foram calibradas nas
# 4.000 conversas da base; esta camada cobre variações de escrita. A ordem prioriza segurança e dinheiro.
_GENERICO = [
    (("ardend", "ardência", "coceira", "coçand", "alergi", "alérgic", "irrit", "vermelh", "inchad", "mancha", "queimou", "descam", "bolinhas", "reação"),
     ("Reclamação produto", "Reação Adversa (Cosmetovigilância)", "humano", False, "Cosmetovigilância", "critica")),
    (("pix", "boleto"), ("Financeiro", "Pagamento Não Conciliado (Pix / Boleto)", "hibrido_copilot", True, "Conciliação de pagamentos", "alta")),
    (("duas vezes", "cobrad", "cobrança", "cobraram", "estorno"),
     ("Financeiro", "Cobrança Duplicada (Cartão / Pix / Boleto)", "hibrido_copilot", True, "Checkout sem idempotência", "alta")),
    (("cupom", "cashback", "frete grátis", "desconto"), ("Promoção", "Falha de Cupom / Cashback / Frete Grátis", "hibrido_copilot", True, "Regras de promoção", "media")),
    (("vazand", "vazou", "vazament"), ("Avaria", "Vazamento de Cosmético (Lote/Embalagem)", "hibrido_copilot", True, "Embalagem / lote", "alta")),
    (("nome errado", "personaliza", "impressão", "borrad", "tort", "iniciais", "foto errada", "veio ane", "nome trocado"),
     ("Avaria", "Erro de Personalização Gocase", "hibrido_copilot", True, "Produção Gocase", "alta")),
    (("quebrad", "amassad", "rachad", "danificad", "arranhad", "trincad"), ("Avaria", "Dano Físico no Transporte / Quebra", "hibrido_copilot", True, "Transporte", "alta")),
    (("brinde",), ("Produto faltando", "Falta de Brinde Promocional", "hibrido_copilot", True, "Separação no CD", "media")),
    (("faltando", "faltou", "incomplet", "veio sem", "não veio"), ("Produto faltando", "Item Principal / Kit Incompleto", "hibrido_copilot", True, "Separação no CD", "alta")),
    (("fragrância", "cheiro", "fixa", "perfume"), ("Reclamação produto", "Insatisfação com Fragrância / Fixação", "hibrido_copilot", False, "Expectativa de produto", "media")),
    (("endereço", "me mudei", "mudar o cep", "outro endereço"), ("Endereço", "Alteração Cadastral de Endereço", "automacao_total", True, "Autoatendimento", "media")),
    (("cancel", "desist", "arrepend", "não quero mais"), ("Cancelamento", "Arrependimento / Pedido Duplicado pelo Cliente", "automacao_total", True, "Autoatendimento", "media")),
    (("escolhi errado", "comprei errado", "tamanho", "modelo errado"), ("Troca", "Troca por Erro de Escolha (Tamanho/Modelo/Cor)", "automacao_total", True, "Autoatendimento", "baixa")),
    (("trocar", "troca", "devolver", "devolução"), ("Troca", "Troca / Devolução de Produto", "hibrido_copilot", False, "Troca", "media")),
    (("atrasad", "atraso", "passou do prazo", "prazo"), ("Atraso", "SLA de Entrega Estourado", "hibrido_copilot", True, "Transportadora", "alta")),
    (("onde está", "cadê", "rastre", "não chegou", "chega quando", "quando chega", "status"),
     ("Rastreio", "Solicitação de Código/Status de Rastreio", "automacao_total", True, "Rastreio passivo", "baixa")),
    (("como usar", "serve para", "posso usar", "indicad", "composição", "dúvida", "diferença", "ingrediente"),
     ("Dúvida", "Dúvida Técnica / Pré-Venda de Produto", "automacao_total", True, "Ficha de produto", "baixa")),
    (("amei", "adorei", "obrigad", "parabéns", "maravilhos"), ("Elogio", "Elogio / Promotor da Marca", "automacao_total", False, "Fidelização", "baixa")),
]


def _classificar_generico(pl):
    for chaves, resultado in _GENERICO:
        if any(k in pl for k in chaves):
            return resultado
    return ("Outros", "Não classificado", "hibrido_copilot", False, "Outros", "media")


# --------------------------------------------------------------------------------------------
# Ferramentas do agente
# Cada ferramenta lê a base (pedidos + histórico) e aplica a política no código. O resultado
# volta para o LLM (ou para o motor de regras) e fica registrado no trace exibido no painel.
# --------------------------------------------------------------------------------------------
TAGS_OFICIAIS = [
    "Rastreio", "Atraso", "Produto faltando", "Avaria", "Troca", "Cancelamento", "Reclamação produto",
    "Financeiro", "Dúvida", "Promoção", "Endereço", "Elogio", "Outros",
]
FILAS = ["Responsável técnica", "Logística", "Financeiro", "CX nível 2", "Reclame Aqui", "Segurança"]
FASE = {
    "aguardando_pagamento": "antes_da_separacao",
    "em_separacao": "em_separacao",
    "em_transito": "expedido",
    "atrasado": "expedido",
    "entregue": "entregue",
}
STATUS_LABEL = {
    "aguardando_pagamento": "aguardando pagamento",
    "em_separacao": "em separação",
    "em_transito": "em trânsito",
    "atrasado": "atrasado",
    "entregue": "entregue",
}

TOOL_SPECS = [
    {
        "name": "consultar_pedido",
        "description": "Busca o pedido na base: produto, SKU, valor, transportadora, UF, status logístico, dias desde a compra e quantas conversas já existem sobre ele.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    },
    {
        "name": "consultar_historico_cliente",
        "description": "Lista as conversas anteriores do cliente (motivo, status). Use para detectar recontato.",
        "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]},
    },
    {
        "name": "verificar_elegibilidade",
        "description": "Aplica uma política vigente ao pedido e diz se o cliente é elegível (sim, nao ou depende) e o que é exigido.",
        "parameters": {
            "type": "object",
            "properties": {
                "politica": {"type": "string", "enum": list(POLITICAS.keys())},
                "order_id": {"type": "string"},
            },
            "required": ["politica", "order_id"],
        },
    },
    {
        "name": "abrir_protocolo_cosmetovigilancia",
        "description": "OBRIGATÓRIO em qualquer relato de reação adversa. Registra o protocolo e encaminha à responsável técnica (prazo 24h).",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}, "descricao": {"type": "string"}},
            "required": ["order_id", "descricao"],
        },
    },
    {
        "name": "solicitar_evidencia",
        "description": "Pede ao cliente a evidência exigida pela política antes de liberar uma ação.",
        "parameters": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "enum": ["foto_produto", "foto_embalagem_etiqueta", "foto_nivel_frasco", "lote_e_fotos_reacao", "data_recebimento"]},
            },
            "required": ["tipo"],
        },
    },
    {
        "name": "criar_reenvio",
        "description": "Pré-cria um reenvio sem custo (avaria, vazamento, item ou brinde faltante). Fica aguardando foto e aprovação.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}, "motivo": {"type": "string"}},
            "required": ["order_id", "motivo"],
        },
    },
    {
        "name": "criar_nova_producao",
        "description": "Pré-cria nova produção sem custo de um item Gocase com erro de personalização. Aguarda foto e aprovação.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    },
    {
        "name": "abrir_chamado_transportadora",
        "description": "Abre chamado prioritário com a transportadora do pedido (pedido parado, sem atualização ou atrasado).",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}, "motivo": {"type": "string"}},
            "required": ["order_id", "motivo"],
        },
    },
    {
        "name": "ativar_rastreio_proativo",
        "description": "Inscreve o cliente em avisos automáticos de cada movimentação do pedido.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    },
    {
        "name": "cancelar_pedido",
        "description": "Cancela o pedido pelo direito de arrependimento, respeitando a fase logística e o prazo de 7 dias.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    },
    {
        "name": "gerar_logistica_reversa",
        "description": "Gera a etiqueta de devolução para troca ou devolução.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}, "motivo": {"type": "string"}},
            "required": ["order_id", "motivo"],
        },
    },
    {
        "name": "alterar_endereco",
        "description": "Altera o endereço de entrega. Só é executado se o pedido ainda não entrou em separação.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    },
    {
        "name": "abrir_analise_financeira",
        "description": "Abre análise de cobrança duplicada ou pagamento não conciliado. Estorno sempre passa por aprovação do Financeiro.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}, "tipo": {"type": "string", "enum": ["cobranca_duplicada", "pagamento_nao_conciliado"]}},
            "required": ["order_id", "tipo"],
        },
    },
    {
        "name": "buscar_ficha_produto",
        "description": "Busca a ficha técnica oficial do SKU para responder dúvidas de produto.",
        "parameters": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]},
    },
    {
        "name": "registrar_alerta_qualidade",
        "description": "Registra alerta de qualidade para o SKU quando há recorrência de vazamento, avaria ou reação.",
        "parameters": {
            "type": "object",
            "properties": {"sku": {"type": "string"}, "problema": {"type": "string"}},
            "required": ["sku", "problema"],
        },
    },
    {
        "name": "classificar_conversa",
        "description": "Grava a tag oficial da conversa (substitui a tag manual do agente humano).",
        "parameters": {
            "type": "object",
            "properties": {"tag": {"type": "string", "enum": TAGS_OFICIAIS}, "subcategoria": {"type": "string"}},
            "required": ["tag"],
        },
    },
    {
        "name": "escalar_humano",
        "description": "Envia a conversa para uma fila humana com o contexto pronto.",
        "parameters": {
            "type": "object",
            "properties": {
                "fila": {"type": "string", "enum": FILAS},
                "prioridade": {"type": "string", "enum": ["critica", "alta", "normal"]},
                "motivo": {"type": "string"},
            },
            "required": ["fila", "prioridade", "motivo"],
        },
    },
    {
        "name": "encerrar_conversa",
        "description": "Encerra a conversa sem atendimento humano (ex.: cliente não escreveu nada).",
        "parameters": {"type": "object", "properties": {"motivo": {"type": "string"}}, "required": ["motivo"]},
    },
    {
        "name": "responder_cliente",
        "description": "Última chamada: envia a resposta final ao cliente, em português, curta e sem prometer nada fora da política.",
        "parameters": {"type": "object", "properties": {"mensagem": {"type": "string"}}, "required": ["mensagem"]},
    },
]


def _sufixo(oid):
    return (oid or "000000")[-5:]


class Ferramentas:
    """Executa as ferramentas contra a base e acumula trace, ações criadas e nível de autonomia."""

    def __init__(self, base, ctx):
        self.base = base  # {"pedidos": {...}, "conversas": {...}, "por_cliente": {...}, "por_pedido": {...}, "alertas_sku": {...}}
        self.ctx = ctx
        self.agora = ctx.get("agora") or datetime(2026, 9, 30, 10, 0)
        self.steps = []
        self.acoes = []
        self.filas = []
        self.autonomia = "autonomo"
        self.motivos_autonomia = []
        self.resposta = None
        self.tag = None

    # -- registro ----------------------------------------------------------------------------
    def _subir(self, nivel, motivo):
        if AUTONOMIA_ORDEM[nivel] > AUTONOMIA_ORDEM[self.autonomia]:
            self.autonomia = nivel
        if motivo and motivo not in self.motivos_autonomia and nivel != "autonomo":
            self.motivos_autonomia.append(motivo)

    def _acao(self, tipo, ident, descricao, estado):
        self.acoes.append({"tipo": tipo, "id": ident, "descricao": descricao, "estado": estado})

    def guardrail(self, nome, detalhe):
        self.steps.append({"tool": nome, "args": {}, "result": detalhe, "kind": "guardrail"})

    def call(self, name, args, origem="regras"):
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            result = {"erro": f"ferramenta desconhecida: {name}"}
        else:
            try:
                result = fn(**(args or {}))
            except TypeError as e:
                result = {"erro": f"argumentos inválidos: {e}"}
        self.steps.append({"tool": name, "args": args or {}, "result": result, "kind": "tool", "origem": origem})
        return result

    # -- leitura -----------------------------------------------------------------------------
    def _pedido(self, order_id):
        return self.base["pedidos"].get((order_id or "").strip().upper())

    def _dias(self, p):
        try:
            return (self.agora.date() - date.fromisoformat(p["data_pedido"])).days
        except Exception:
            return None

    def t_consultar_pedido(self, order_id):
        p = self._pedido(order_id)
        if not p:
            return {"encontrado": False, "order_id": order_id}
        convs = self.base["por_pedido"].get(p["order_id"], [])
        anteriores = [c for c in convs if c["data_abertura"] < self.agora.isoformat(sep=" ")]
        return {
            "encontrado": True,
            "order_id": p["order_id"],
            "marca": p["marca"],
            "produto": p["descricao_produto"],
            "sku": p["sku"],
            "valor": float(p["valor"]),
            "data_pedido": p["data_pedido"],
            "dias_desde_pedido": self._dias(p),
            "transportadora": p["transportadora"],
            "uf": p["uf"],
            "status_entrega": p["status_entrega"],
            "fase": FASE.get(p["status_entrega"], "desconhecida"),
            "conversas_anteriores_sobre_o_pedido": len(anteriores),
        }

    def t_consultar_historico_cliente(self, customer_id):
        convs = self.base["por_cliente"].get(customer_id, [])
        ant = [c for c in convs if c["data_abertura"] < self.agora.isoformat(sep=" ")]
        return {
            "total_conversas_anteriores": len(ant),
            "ultimas": [
                {"conversation_id": c["conversation_id"], "data": c["data_abertura"][:10], "motivo": c["tag_ia"], "status": c["status"]}
                for c in ant[-4:]
            ],
        }

    def t_verificar_elegibilidade(self, politica, order_id):
        if politica not in POLITICAS:
            return {"erro": "política desconhecida", "opcoes": list(POLITICAS)}
        p = self._pedido(order_id)
        base = {"politica": POLITICAS[politica]["titulo"], "regra": POLITICAS[politica]["texto"]}
        if politica == "cosmetovigilancia":
            return {**base, "elegivel": "sim", "motivo": "Registro obrigatório, independente de prazo ou troca.", "exige": ["lote", "fotos", "descrição"]}
        if not p:
            return {**base, "elegivel": "depende", "motivo": "Pedido não encontrado na base.", "exige": ["número do pedido"]}
        fase, dias = FASE.get(p["status_entrega"]), self._dias(p)
        if politica == "endereco":
            if fase == "antes_da_separacao":
                return {**base, "elegivel": "sim", "motivo": "Pedido ainda não entrou em separação.", "exige": ["novo endereço completo"]}
            return {**base, "elegivel": "nao", "motivo": f"Pedido já está {STATUS_LABEL.get(p['status_entrega'])}; a política só permite antes da separação."}
        if politica == "cobranca_duplicada":
            return {**base, "elegivel": "depende", "motivo": "Duplicidade precisa ser confirmada no gateway pelo Financeiro.", "exige": ["confirmação no gateway"]}
        if politica == "arrependimento":
            if fase in ("antes_da_separacao", "em_separacao"):
                return {**base, "elegivel": "sim", "motivo": "Pedido ainda não saiu do CD: cancelamento direto com reembolso integral."}
            if fase == "expedido":
                return {**base, "elegivel": "sim", "motivo": "Pedido em trânsito: o prazo de 7 dias conta a partir do recebimento."}
            if dias is not None and dias <= 7:
                return {**base, "elegivel": "sim", "motivo": f"Compra há {dias} dias: recebimento certamente dentro de 7 dias."}
            return {**base, "elegivel": "depende", "motivo": f"Compra há {dias} dias e a base não tem a data de entrega.", "exige": ["data_recebimento"]}
        janela = 30 if politica == "fragrancia" else 7
        exige = {"avaria_faltante": ["foto"], "personalizacao": ["foto"], "fragrancia": ["foto do nível do frasco (máx. 20% usado)"]}[politica]
        if fase != "entregue":
            return {**base, "elegivel": "depende", "motivo": f"Status logístico ainda é '{STATUS_LABEL.get(p['status_entrega'])}'; confirmar recebimento.", "exige": exige + ["data_recebimento"]}
        if politica == "fragrancia":
            ja = [c for c in self.base["por_pedido"].get(p["order_id"], []) if c["sub_ia"] == "Insatisfação com Fragrância / Fixação" and c["status"] == "resolvido" and c["data_abertura"] < self.agora.isoformat(sep=" ")]
            if ja:
                return {**base, "elegivel": "nao", "motivo": "Este pedido já teve uma troca de fragrância (limite de 1 por pedido)."}
        if dias is not None and dias <= janela:
            return {**base, "elegivel": "sim", "motivo": f"Compra há {dias} dias: dentro da janela de {janela} dias.", "exige": exige}
        return {**base, "elegivel": "depende", "motivo": f"Compra há {dias} dias; validar data de recebimento (janela de {janela} dias).", "exige": exige + ["data_recebimento"]}

    # -- ações -------------------------------------------------------------------------------
    def t_abrir_protocolo_cosmetovigilancia(self, order_id, descricao=""):
        p = self._pedido(order_id) or {}
        ident = f"CSV-{_sufixo(order_id)}"
        prazo = (self.agora + timedelta(hours=24)).strftime("%d/%m %H:%M")
        self._acao("protocolo_cosmetovigilancia", ident, f"Protocolo aberto para {p.get('descricao_produto', 'produto')} (SKU {p.get('sku', '?')}); encaminhado à responsável técnica", "aberto")
        self._subir("humano", "Reação adversa: avaliação da responsável técnica é obrigatória")
        if "Responsável técnica" not in self.filas:
            self.filas.append("Responsável técnica")
        return {"protocolo": ident, "prazo_registro": prazo, "encaminhado_para": "Responsável técnica", "pendente_do_cliente": ["lote", "fotos", "descrição do uso"]}

    def t_solicitar_evidencia(self, tipo):
        self._acao("solicitar_evidencia", None, f"Solicitado ao cliente: {tipo.replace('_', ' ')}", "aguardando_cliente")
        return {"solicitado": tipo, "estado": "aguardando_cliente"}

    def _pre_aprovacao(self, tipo, prefixo, order_id, descricao, politica):
        eleg = self.t_verificar_elegibilidade(politica, order_id)
        if eleg.get("elegivel") == "nao":
            return {"criado": False, "motivo": eleg["motivo"]}
        ident = f"{prefixo}-{_sufixo(order_id)}"
        self._acao(tipo, ident, descricao, "aguardando_foto_e_aprovacao")
        self._subir("aprovacao", "Liberação com custo de produto: uma pessoa valida a foto e aprova")
        return {"id": ident, "estado": "aguardando_foto_e_aprovacao", "elegibilidade": eleg["elegivel"]}

    def t_criar_reenvio(self, order_id, motivo):
        p = self._pedido(order_id) or {}
        return self._pre_aprovacao("reenvio", "ENV", order_id, f"Reenvio sem custo de {p.get('descricao_produto', 'item')} ({motivo})", "avaria_faltante")

    def t_criar_nova_producao(self, order_id):
        p = self._pedido(order_id) or {}
        return self._pre_aprovacao("nova_producao", "REP", order_id, f"Nova produção sem custo de {p.get('descricao_produto', 'item')}", "personalizacao")

    def t_abrir_chamado_transportadora(self, order_id, motivo):
        p = self._pedido(order_id)
        if not p:
            return {"aberto": False, "motivo": "pedido não encontrado"}
        ident = f"LOG-{_sufixo(order_id)}"
        self._acao("chamado_transportadora", ident, f"Chamado com {p['transportadora']} ({p['uf']}): {motivo}", "aberto")
        return {"chamado": ident, "transportadora": p["transportadora"], "prazo_retorno": "48h"}

    def t_ativar_rastreio_proativo(self, order_id):
        if not self._pedido(order_id):
            return {"ativado": False}
        self._acao("rastreio_proativo", None, "Avisos automáticos de movimentação ativados", "executada")
        return {"ativado": True, "canal": "WhatsApp"}

    def t_cancelar_pedido(self, order_id):
        p = self._pedido(order_id)
        if not p:
            return {"executado": False, "motivo": "pedido não encontrado"}
        eleg = self.t_verificar_elegibilidade("arrependimento", order_id)
        fase, valor = FASE.get(p["status_entrega"]), float(p["valor"])
        if eleg["elegivel"] != "sim":
            self._subir("aprovacao", "Arrependimento sem data de recebimento na base: pessoa confirma o prazo")
            self._acao("cancelamento", f"CAN-{_sufixo(order_id)}", "Cancelamento aguardando confirmação da data de recebimento", "aguardando_aprovacao")
            return {"executado": False, "estado": "aguardando_aprovacao", "motivo": eleg["motivo"]}
        if fase in ("antes_da_separacao", "em_separacao"):
            self._acao("cancelamento", f"CAN-{_sufixo(order_id)}", f"Pedido cancelado antes da expedição; reembolso integral de R$ {valor:.2f}", "executada")
            return {"executado": True, "tipo": "cancelamento_antes_da_expedicao", "reembolso": valor}
        ident = f"REV-{_sufixo(order_id)}"
        self._acao("logistica_reversa", ident, f"Devolução por arrependimento; reembolso de R$ {valor:.2f} após a devolução", "executada")
        return {"executado": True, "tipo": "logistica_reversa", "etiqueta": ident, "reembolso": valor}

    def t_gerar_logistica_reversa(self, order_id, motivo):
        p = self._pedido(order_id)
        if not p:
            return {"executado": False, "motivo": "pedido não encontrado"}
        ident = f"REV-{_sufixo(order_id)}"
        eleg = self.t_verificar_elegibilidade("arrependimento", order_id)
        if eleg["elegivel"] == "sim":
            self._acao("logistica_reversa", ident, f"Etiqueta de devolução emitida ({motivo})", "executada")
            return {"executado": True, "etiqueta": ident}
        self._subir("aprovacao", "Troca fora da janela verificável: pessoa confirma o prazo")
        self._acao("logistica_reversa", ident, f"Etiqueta de devolução pré-criada ({motivo})", "aguardando_aprovacao")
        return {"executado": False, "etiqueta": ident, "estado": "aguardando_aprovacao", "motivo": eleg["motivo"]}

    def t_alterar_endereco(self, order_id):
        eleg = self.t_verificar_elegibilidade("endereco", order_id)
        if eleg["elegivel"] == "sim":
            self._acao("alteracao_endereco", None, "Alteração de endereço liberada (pedido antes da separação)", "aguardando_cliente")
            return {"liberado": True, "proximo_passo": "cliente informa o novo endereço"}
        self._acao("alteracao_endereco", None, "Alteração negada pela política (pedido já em separação ou expedido)", "bloqueada_politica")
        return {"liberado": False, "motivo": eleg["motivo"]}

    def t_abrir_analise_financeira(self, order_id, tipo):
        p = self._pedido(order_id) or {}
        ident = f"FIN-{_sufixo(order_id)}"
        desc = "Análise de cobrança duplicada; estorno após confirmação" if tipo == "cobranca_duplicada" else "Conciliação de pagamento não identificado"
        self._acao("analise_financeira", ident, f"{desc} (R$ {float(p.get('valor', 0) or 0):.2f})", "aguardando_aprovacao")
        self._subir("aprovacao", "Movimentação de dinheiro: o Financeiro confirma no gateway")
        if "Financeiro" not in self.filas:
            self.filas.append("Financeiro")
        prazo = "até 2 faturas (cartão) ou 5 dias úteis (pix)" if tipo == "cobranca_duplicada" else "liberação do pedido após conciliação"
        return {"analise": ident, "estado": "aguardando_aprovacao", "prazo_politica": prazo}

    def t_buscar_ficha_produto(self, sku):
        # A base do desafio não traz ficha técnica. O agente não inventa composição nem indicação de uso.
        self._subir("aprovacao", "Sem ficha técnica cadastrada: o agente não inventa informação de produto")
        return {"disponivel": False, "sku": sku, "motivo": "ficha técnica não cadastrada na base"}

    def t_registrar_alerta_qualidade(self, sku, problema):
        n = self.base.get("alertas_sku", {}).get(sku, {})
        ident = f"QLD-{sku}"
        self._acao("alerta_qualidade", ident, f"Alerta de qualidade: {problema} no SKU {sku}", "aberto")
        return {"alerta": ident, "ocorrencias_90d_amostra": n}

    def t_classificar_conversa(self, tag, subcategoria=None):
        if tag not in TAGS_OFICIAIS:
            return {"gravado": False, "motivo": "tag fora da lista oficial", "opcoes": TAGS_OFICIAIS}
        self.tag = tag
        return {"gravado": True, "tag": tag, "subcategoria": subcategoria}

    def t_escalar_humano(self, fila, prioridade="normal", motivo=""):
        if fila not in FILAS:
            fila = "CX nível 2"
        if fila not in self.filas:
            self.filas.append(fila)
        nivel = "humano" if fila in ("Responsável técnica",) else "aprovacao"
        self._subir(nivel, motivo)
        self._acao("escalonamento", None, f"Fila {fila} ({prioridade}): {motivo}", "na_fila")
        return {"fila": fila, "prioridade": prioridade}

    def t_encerrar_conversa(self, motivo):
        self._acao("encerramento", None, motivo, "executada")
        return {"encerrada": True}

    def t_responder_cliente(self, mensagem):
        self.resposta = (mensagem or "").strip()
        return {"enviada": True, "caracteres": len(self.resposta)}


# --------------------------------------------------------------------------------------------
# Agente em modo regras (determinístico). Mesmas ferramentas, mesmo trace.
# É o fallback quando não há chave de LLM e o motor usado para processar a fila inteira.
# --------------------------------------------------------------------------------------------
def _ola(ctx):
    return "Olá!" if ctx.get("canal") == "whatsapp" else "Olá, tudo bem?"


def _assinatura(ctx):
    marca = ctx.get("marca") or "nossa equipe"
    return f"\n\nEquipe {marca}" if ctx.get("canal") in ("email", "reclame_aqui") else ""


def _tratar_demanda(ft, ctx, clause, ped):
    tag, sub = clause[0], clause[1]
    oid = ctx.get("order_id") or ""
    prod = ped.get("produto", "seu pedido") if ped.get("encontrado") else "seu pedido"
    transp = ped.get("transportadora", "a transportadora")

    if tag == "Rastreio":
        ft.call("ativar_rastreio_proativo", {"order_id": oid})
        txt = f"Seu pedido {oid} ({prod}) está {STATUS_LABEL.get(ped.get('status_entrega'), 'em processamento')} com a {transp}, a caminho de {ped.get('uf', '')}."
        if sub in ("Pedido Parado em Hub Regional", "Rastreio Sem Atualização / Em Trânsito Longo"):
            r = ft.call("abrir_chamado_transportadora", {"order_id": oid, "motivo": "rastreio sem atualização"})
            txt += f" Como o rastreio parou de atualizar, abri o chamado {r.get('chamado')} com a {transp} e te dou retorno em até 48h."
        return txt + " Ativei avisos automáticos por WhatsApp a cada movimentação, assim você acompanha sem precisar perguntar."

    if tag == "Atraso":
        r = ft.call("abrir_chamado_transportadora", {"order_id": oid, "motivo": "prazo de entrega estourado"})
        ft.call("escalar_humano", {"fila": "Logística", "prioridade": "alta", "motivo": "Atraso: decidir reenvio ou reembolso se não houver entrega em 48h"})
        return (
            f"Peço desculpas pelo atraso do pedido {oid} ({prod}). Abri o chamado prioritário {r.get('chamado')} com a {transp}. "
            "Se a entrega não for confirmada em 48h, nossa equipe de logística te procura com uma solução."
        )

    if tag == "Produto faltando" or sub in ("Vazamento de Cosmético (Lote/Embalagem)", "Dano Físico no Transporte / Quebra"):
        e = ft.call("verificar_elegibilidade", {"politica": "avaria_faltante", "order_id": oid})
        evid = "foto_embalagem_etiqueta" if tag == "Produto faltando" else "foto_produto"
        ft.call("solicitar_evidencia", {"tipo": evid})
        motivo = {"Falta de Brinde Promocional": "brinde faltante", "Item Principal / Kit Incompleto": "item faltante"}.get(sub, "avaria/vazamento")
        r = ft.call("criar_reenvio", {"order_id": oid, "motivo": motivo})
        if sub == "Vazamento de Cosmético (Lote/Embalagem)" and ped.get("sku"):
            ft.call("registrar_alerta_qualidade", {"sku": ped["sku"], "problema": "vazamento"})
        pedido_foto = "uma foto da caixa aberta e da etiqueta" if tag == "Produto faltando" else "uma foto do produto e da embalagem"
        extra = " e me confirma a data em que recebeu" if e.get("elegivel") == "depende" else ""
        what = {"brinde faltante": "do brinde", "item faltante": "do item que faltou"}.get(motivo, "de um produto novo")
        return (
            f"Sinto muito pelo problema com o pedido {oid} ({prod}). Pela nossa política, o reenvio {what} é sem custo em até 7 dias do recebimento. "
            f"Pode me enviar {pedido_foto}{extra}? Com a foto, o reenvio {r.get('id', '')} é liberado."
        )

    if sub == "Erro de Personalização Gocase":
        e = ft.call("verificar_elegibilidade", {"politica": "personalizacao", "order_id": oid})
        ft.call("solicitar_evidencia", {"tipo": "foto_produto"})
        r = ft.call("criar_nova_producao", {"order_id": oid})
        extra = " e a data em que recebeu" if e.get("elegivel") == "depende" else ""
        return (
            f"Poxa, desculpa pelo erro na personalização do seu {prod}. Você tem direito a uma nova produção sem custo em até 7 dias do recebimento. "
            f"Me manda uma foto mostrando o defeito{extra} e eu libero a nova produção {r.get('id', '')}."
        )

    if sub == "Reação Adversa (Cosmetovigilância)":
        r = ft.call("abrir_protocolo_cosmetovigilancia", {"order_id": oid, "descricao": "Relato de reação adversa do cliente"})
        ft.call("solicitar_evidencia", {"tipo": "lote_e_fotos_reacao"})
        ft.call("escalar_humano", {"fila": "Responsável técnica", "prioridade": "critica", "motivo": "Reação adversa: avaliar caso e acompanhar cliente"})
        if ped.get("sku"):
            ft.call("registrar_alerta_qualidade", {"sku": ped["sku"], "problema": "reação adversa"})
        return (
            f"Sinto muito pelo que aconteceu. Por segurança, suspenda o uso do {prod} agora. "
            f"Registrei o protocolo de cosmetovigilância {r['protocolo']} e ele já foi encaminhado para a nossa responsável técnica, que vai acompanhar o seu caso. "
            "Para completar o registro, preciso de: o número do lote (impresso na embalagem), fotos da região afetada e do produto, e uma descrição curta de como e quando você usou. "
            "Se os sintomas piorarem, procure atendimento médico."
        )

    if sub == "Insatisfação com Fragrância / Fixação":
        e = ft.call("verificar_elegibilidade", {"politica": "fragrancia", "order_id": oid})
        if e.get("elegivel") == "nao":
            ft.call("escalar_humano", {"fila": "CX nível 2", "prioridade": "normal", "motivo": "Fragrância: troca já utilizada neste pedido"})
            return f"Entendo, que pena que a fragrância não te agradou. Vi que este pedido já teve uma troca de fragrância, que é o limite da nossa política. Vou pedir para uma especialista te ajudar a encontrar outra opção."
        ft.call("solicitar_evidencia", {"tipo": "foto_nivel_frasco"})
        r = ft.call("gerar_logistica_reversa", {"order_id": oid, "motivo": "troca de fragrância"})
        ft.call("escalar_humano", {"fila": "CX nível 2", "prioridade": "normal", "motivo": "Fragrância: validar foto (máx. 20% usado) e aprovar troca"})
        return (
            f"Que pena que a fragrância do {prod} não combinou com você. Você tem direito a 1 troca por pedido em até 30 dias, com no máximo 20% do produto usado. "
            f"Me manda uma foto do frasco mostrando o nível e me diz qual fragrância prefere. Validada a foto, a etiqueta de troca {r.get('etiqueta', '')} é liberada."
        )

    if tag == "Troca":
        r = ft.call("gerar_logistica_reversa", {"order_id": oid, "motivo": sub})
        if sub == "Troca / Devolução de Produto":
            ft._subir("aprovacao", "Troca sem motivo coberto por política: pessoa decide")
        if r.get("executado"):
            return f"Tudo certo para a troca do pedido {oid} ({prod}). Gerei a etiqueta de devolução {r['etiqueta']}; assim que você postar, seguimos com o novo item ou o reembolso."
        ft.call("solicitar_evidencia", {"tipo": "data_recebimento"})
        return f"Consigo te ajudar com a troca do pedido {oid} ({prod}). Me confirma a data em que você recebeu? Com isso valido o prazo e libero a etiqueta de devolução {r.get('etiqueta', '')}."

    if tag == "Cancelamento":
        r = ft.call("cancelar_pedido", {"order_id": oid})
        if r.get("tipo") == "cancelamento_antes_da_expedicao":
            return f"Pronto, cancelei o pedido {oid} ({prod}) antes da expedição. O reembolso integral de R$ {r['reembolso']:.2f} volta pela mesma forma de pagamento."
        if r.get("tipo") == "logistica_reversa":
            return f"Registrei o seu arrependimento do pedido {oid}. Gerei a etiqueta de devolução {r['etiqueta']}; quando o produto voltar, o reembolso integral de R$ {r['reembolso']:.2f} é feito."
        ft.call("solicitar_evidencia", {"tipo": "data_recebimento"})
        return f"Consigo cancelar o pedido {oid} pelo direito de arrependimento, que vale até 7 dias após o recebimento. Me confirma a data em que recebeu?"

    if tag == "Endereço":
        r = ft.call("alterar_endereco", {"order_id": oid})
        if r.get("liberado"):
            return f"Dá tempo! O pedido {oid} ainda não entrou em separação. Me manda o endereço completo (CEP, rua, número e complemento) que eu atualizo."
        return (
            f"Verifiquei o pedido {oid} e ele já está {STATUS_LABEL.get(ped.get('status_entrega'), 'em andamento')}. Pela nossa política, o endereço só pode ser alterado antes da separação. "
            f"Se ninguém puder receber no endereço original, o pedido volta para o nosso centro e combinamos o reenvio com você."
        )

    if tag == "Financeiro":
        tipo = "cobranca_duplicada" if sub.startswith("Cobrança Duplicada") else "pagamento_nao_conciliado"
        r = ft.call("abrir_analise_financeira", {"order_id": oid, "tipo": tipo})
        if tipo == "cobranca_duplicada":
            return f"Abri a análise {r['analise']} da cobrança do pedido {oid}. Confirmada a duplicidade, o estorno sai em até 5 dias úteis no Pix ou em até 2 faturas no cartão."
        return f"Abri a conciliação {r['analise']} do pagamento do pedido {oid}. Assim que o pagamento for identificado, o pedido segue para separação e eu te aviso."

    if tag == "Promoção":
        ft.call("escalar_humano", {"fila": "CX nível 2", "prioridade": "normal", "motivo": "Validar cupom/cashback/frete e aplicar compensação"})
        return "Registrei o problema com a promoção e passei para a equipe que valida cupons e cashback. Você recebe a resposta em até 24h."

    if tag == "Dúvida":
        ft.call("buscar_ficha_produto", {"sku": ped.get("sku", "")})
        ft.call("escalar_humano", {"fila": "CX nível 2", "prioridade": "normal", "motivo": "Dúvida de produto sem ficha técnica cadastrada"})
        return f"Boa pergunta! Quero te responder com a informação oficial do {prod}, então passei para a nossa especialista, que te responde ainda hoje."

    if tag == "Elogio":
        return "Que alegria ler isso! Obrigada pelo carinho, vamos compartilhar com o time."

    ft.call("escalar_humano", {"fila": "CX nível 2", "prioridade": "normal", "motivo": "Demanda não classificada"})
    return "Recebi a sua mensagem e passei para uma pessoa do nosso time, que te responde em breve."


def agente_regras(base, ctx):
    """Executa o agente sem LLM. ctx: conversation_id, customer_id, order_id, canal, marca, texto, agora."""
    ft = Ferramentas(base, ctx)
    an = analisar_mensagem(ctx.get("texto", ""))
    if an["pii"]:
        ft.guardrail("lgpd_mascarar_pii", {"mascarado": an["pii"]})
    if an["injection"]:
        ft.guardrail("bloquear_prompt_injection", {"trecho_ignorado": an["injection"][:160]})
        ft.call("escalar_humano", {"fila": "Segurança", "prioridade": "alta", "motivo": "Tentativa de manipular o agente na mensagem do cliente"})

    if not an["clauses"]:
        ft.call("classificar_conversa", {"tag": "Outros", "subcategoria": "Sem mensagem do cliente"})
        ft.call("encerrar_conversa", {"motivo": "Cliente abriu a conversa e não escreveu"})
        return finalizar(ft, an, ctx, "rules")

    tag0, sub0 = an["clauses"][0][0], an["clauses"][0][1]
    ft.call("classificar_conversa", {"tag": tag0, "subcategoria": sub0})
    ped = ft.call("consultar_pedido", {"order_id": ctx.get("order_id", "")}) if ctx.get("order_id") else {"encontrado": False}
    if ctx.get("customer_id"):
        h = ft.call("consultar_historico_cliente", {"customer_id": ctx["customer_id"]})
        if h["total_conversas_anteriores"] and an["recontato_explicito"]:
            ft.guardrail("recontato_detectado", {"conversas_anteriores": h["total_conversas_anteriores"]})

    if an["injection"]:
        # Mensagem com tentativa de manipulação: nenhuma ação irreversível ou com dinheiro sai sem revisão.
        ref = f" sobre o pedido {ped['order_id']}" if ped.get("encontrado") else ""
        partes = [f"Recebi a sua mensagem{ref}. Antes de qualquer alteração, ela passa por uma revisão do nosso time, e te retornamos em breve com a solução."]
    else:
        partes = [_tratar_demanda(ft, ctx, cl, ped) for cl in an["clauses"]]
    if ctx.get("canal") == "reclame_aqui":
        ft.call("escalar_humano", {"fila": "Reclame Aqui", "prioridade": "alta", "motivo": "Resposta pública: revisar antes de publicar"})

    corpo = partes[0]
    if len(partes) > 1:
        corpo += "\n\nSobre o outro ponto que você trouxe: " + partes[1][0].lower() + partes[1][1:]
    ft.call("responder_cliente", {"mensagem": f"{_ola(ctx)} {corpo}{_assinatura(ctx)}"})
    return finalizar(ft, an, ctx, "rules")


def finalizar(ft, an, ctx, modo, **extra):
    intents = [{"tag": c[0], "subcategoria": c[1], "autonomia": AUTONOMIA.get(c[1], "aprovacao")} for c in an["clauses"]]
    for it in intents:
        if it["autonomia"] == "humano":
            ft._subir("humano", "Reação adversa: avaliação da responsável técnica é obrigatória")
    return {
        "ok": True,
        "modo": modo,
        "conversation_id": ctx.get("conversation_id"),
        "guardrails": {"pii": an["pii"], "injection": an["injection"], "recontato": an["recontato_explicito"]},
        "intents": intents,
        "steps": ft.steps,
        "acoes": ft.acoes,
        "filas": ft.filas,
        "autonomia": ft.autonomia,
        "motivos_autonomia": ft.motivos_autonomia,
        "tag": ft.tag,
        "resposta": ft.resposta or "",
        **extra,
    }
