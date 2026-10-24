"""Gera o arquivo EFD ICMS/IPI (SPED Fiscal) com o inventário no bloco H.

Entradas aceitas (o tipo é detectado pelo conteúdo):
  - relatório "Registro de Inventário" em texto (colunas de largura fixa)
  - arquivo .FS da Fortes (registros CAB/GRP/UND/PRO/IIV)
  - bloco H solto ou SPED completo (.txt com |H010|)
  - planilhas .xls/.xlsx/.csv (Planilha Modelo, Estoque Escriturado ou parecidas)

O primeiro arquivo com valores é o inventário; os demais só completam
descrição, NCM, unidade, tipo do item, conta contábil e custo unitário.

Leiaute: Guia Prático EFD ICMS/IPI 3.2.2 (leiaute 019 em 2025, 020 em 2026).
"""

import argparse
import calendar
import collections
import csv
import json
import os
import re
import sys
import unicodedata
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

PASTA = Path(__file__).resolve().parent
# No app instalado os dados ficam fora da pasta do programa (ESTOQUE_DADOS)
DADOS = Path(os.environ.get("ESTOQUE_DADOS") or PASTA)
CADASTRO_EMPRESAS = DADOS / "empresas.json"
ESCRITORIO = DADOS / "escritorio.json"

# Código da versão do leiaute por ano do DT_FIN (Ato COTEPE/ICMS 44/2018 e alterações)
VERSAO_LEIAUTE = {2020: "014", 2021: "015", 2022: "016", 2023: "017", 2024: "018", 2025: "019", 2026: "020"}

UNIDADES = {
    "UN": "UNIDADE", "UND": "UNIDADE", "UNID": "UNIDADE", "PC": "PECA", "PCA": "PECA", "PCS": "PECAS",
    "KG": "QUILOGRAMA", "G": "GRAMA", "GR": "GRAMA", "MG": "MILIGRAMA", "TON": "TONELADA", "T": "TONELADA",
    "M": "METRO", "MT": "METRO", "MTS": "METROS", "CM": "CENTIMETRO", "MM": "MILIMETRO", "M2": "METRO QUADRADO",
    "M3": "METRO CUBICO", "L": "LITRO", "LT": "LITRO", "ML": "MILILITRO", "CX": "CAIXA", "PCT": "PACOTE",
    "PCTE": "PACOTE", "PT": "PACOTE", "FD": "FARDO", "SC": "SACO", "RL": "ROLO", "PR": "PAR", "JG": "JOGO",
    "KIT": "KIT", "DZ": "DUZIA", "CJ": "CONJUNTO", "BD": "BALDE", "GL": "GALAO", "LA": "LATA", "FR": "FRASCO",
    "TB": "TUBO", "BL": "BLOCO", "FL": "FOLHA", "CT": "CARTELA", "BB": "BOBINA", "EMB": "EMBALAGEM",
}

# Unidade escrita por extenso na planilha ("UNIDADE", "PEÇA"): volta para a sigla
POR_EXTENSO = {}
for _sigla, _nome in UNIDADES.items():
    POR_EXTENSO.setdefault(_nome, _sigla)
POR_EXTENSO.update({"UNIDADES": "UN", "QUILO": "KG", "QUILOS": "KG", "QUILOGRAMAS": "KG", "LITROS": "L",
                    "CAIXAS": "CX", "PACOTES": "PCT", "PECAS": "PC", "METROS": "M", "PARES": "PR", "JOGOS": "JG"})

DOIS = Decimal("0.01")
TRES = Decimal("0.001")
SEIS = Decimal("0.000001")


@dataclass
class Item:
    codigo: str
    descricao: str = ""
    unidade: str = ""
    ncm: str = ""
    qtd: Decimal | None = None
    vl_unit: Decimal | None = None
    vl_item: Decimal | None = None
    ind_prop: str = "0"
    cod_cta: str = ""
    icms_rec: Decimal | None = None
    tipo: str = ""
    cest: str = ""
    cod_barra: str = ""
    aliases: set = field(default_factory=set)
    origem: str = ""


@dataclass
class Fonte:
    caminho: Path
    tipo: str
    itens: list
    empresa: dict = field(default_factory=dict)
    data_inv: date | None = None
    total_declarado: Decimal | None = None
    unidades: dict = field(default_factory=dict)

    @property
    def tem_valores(self):
        return any(i.vl_unit is not None or i.vl_item is not None for i in self.itens)


# ---------------------------------------------------------------- utilidades

RE_MILHAR = re.compile(r"-?\d{1,3}(\.\d{3})+")


def num(txt, br=False):
    """Converte '1.234,56', '1234.5600', 1234.5 ou '' em Decimal (ou None).

    Sem vírgula, o ponto é decimal ("2.5"), a não ser que seja claramente milhar
    ("1.234.567") ou que o arquivo use vírgula decimal (br=True): aí "1.234" é mil e
    duzentos e trinta e quatro."""
    if txt is None:
        return None
    if isinstance(txt, (int, float)):
        return Decimal(str(txt))
    t = re.sub(r"R\$|\s", "", str(txt).strip())  # "R$ 1.234,56" vindo como texto do Excel
    if not t or t in "-":
        return None
    if "," in t or (br and RE_MILHAR.fullmatch(t)) or (t.count(".") > 1 and RE_MILHAR.fullmatch(t)):
        t = t.replace(".", "").replace(",", ".")
    try:
        return Decimal(t)
    except Exception:
        return None


def fmt(valor, casas):
    q = {2: DOIS, 3: TRES, 6: SEIS}[casas]
    return f"{valor.quantize(q, ROUND_HALF_UP):f}".replace(".", ",")


def so_digitos(txt):
    return re.sub(r"\D", "", str(txt or ""))


def texto(txt, tam=None):
    """Campo C: sem pipe, sem caracteres de controle, sem espaços nas pontas."""
    t = re.sub(r"[\x00-\x1f|]", " ", str(txt or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:tam].strip() if tam else t


def cod_texto(valor):
    """Código vindo de planilha: 20.0 vira '20'."""
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return texto(valor)


def sem_acento(t):
    return unicodedata.normalize("NFKD", str(t)).encode("ascii", "ignore").decode().upper()


def ler_texto(caminho):
    dados = caminho.read_bytes()
    for cod in ("utf-8", "cp1252", "latin-1"):
        try:
            return dados.decode(cod)
        except UnicodeDecodeError:
            continue
    return dados.decode("latin-1", "replace")


def data_ddmmaaaa(txt):
    """'31/12/2025', '31122025' ou '20251231' em date; data inexistente vira None."""
    d = so_digitos(txt)
    if len(d) != 8:
        return None
    try:
        if int(d[:4]) > 1900 and int(d[4:6]) <= 12:  # aaaammdd
            return date(int(d[:4]), int(d[4:6]), int(d[6:]))
        return date(int(d[4:]), int(d[2:4]), int(d[:2]))
    except ValueError:  # 31/02, mês 13...
        return None


def so_cnpj(txt):
    """CNPJ sem máscara. Desde 31/07/2026 a Receita emite CNPJ alfanumérico
    (IN RFB 2.229/2024): as 12 primeiras posições aceitam letras, os 2 DVs são números."""
    return re.sub(r"[^0-9A-Z]", "", str(txt or "").upper())


# CNPJ dentro de um texto: 11.222.333/0001-81, 12.ABC.345/01DE-35 ou sem máscara
RE_CNPJ = re.compile(r"(?<![0-9A-Z])([0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?\d{2})(?![0-9A-Z])", re.I)


def cnpj_valido(c):
    c = so_cnpj(c)
    if not re.fullmatch(r"[0-9A-Z]{12}\d{2}", c) or len(set(c)) == 1:
        return False
    valores = [ord(x) - 48 for x in c]  # dígito = ele mesmo, A = 17 ... Z = 42
    for tam in (12, 13):
        pesos = list(range(tam - 7, 1, -1)) + list(range(9, 1, -1))
        dv = 11 - sum(a * b for a, b in zip(valores[:tam], pesos)) % 11
        if valores[tam] != (0 if dv >= 10 else dv):
            return False
    return True


# ---------------------------------------------------------------- leitores

RE_LINHA_INV = re.compile(
    r"^(?P<ncm>\d{2,8})\s+(?P<texto>.+?)\s{2,}(?P<un>\S{1,6})\s+(?P<qtd>-?[\d.]+,\d+)\s+"
    r"(?P<unit>-?[\d.]+,\d+)\s+(?P<total>-?[\d.]+,\d+)\s*$",
    re.M,  # a detecção procura em todo o arquivo, não só na primeira linha
)


def separar_codigo(txt):
    """No relatório o código e a descrição vêm juntos ("U3424 696 041/22M LEXUS ...").
    O código são os primeiros termos que têm número ou barra; um prefixo curto só de
    letras entra quando o termo seguinte parece código ("CO 11420JS/02", "BG 99100-003-A",
    "GC 1999885"), mas não num "KIT 2 PEÇAS". O "-" que separa código e descrição
    ("5095 - EXPOSITOR", "5102- ISOTONIC") não fica em nenhum dos dois."""
    termos = txt.split()
    tem_cod = lambda t: bool(re.search(r"[\d/]", t))
    n = 0
    if len(termos) > 1 and re.fullmatch(r"[A-Z]{1,3}", termos[0]) and tem_cod(termos[1]) and len(termos[1]) >= 4:
        n = 2
    while n < len(termos) - 1 and tem_cod(termos[n]):
        n += 1
    codigo = " ".join(termos[:n]).rstrip("-–").rstrip()
    descricao = " ".join(termos[n:]).lstrip("-–").lstrip()
    return codigo, descricao


def ler_relatorio_inventario(caminho, conteudo):
    itens, empresa, data_inv, total = [], {}, None, None
    for linha in conteudo.splitlines():
        m = RE_LINHA_INV.match(linha)
        if m:
            codigo, descr = separar_codigo(m["texto"])
            itens.append(Item(
                codigo=codigo, descricao=descr or m["texto"], unidade=m["un"], ncm=m["ncm"],
                qtd=num(m["qtd"]), vl_unit=num(m["unit"]), vl_item=num(m["total"]),
                origem=m["texto"],
            ))
            continue
        # Cabeçalho: o relatório sai em maiúsculas ou não, conforme o sistema do cliente
        if "nome" not in empresa and (m := re.match(r"\s*Firma\b[^:]*:\s*(.+)", linha, re.I)):
            empresa["nome"] = texto(re.split(r"\s{2,}", m[1].strip())[0])
        if "ie" not in empresa and (m := re.search(r"Insc(?:ri[cç][aã]o)?\.?\s*Est[^:]*:\s*([\d./-]+)", linha, re.I)):
            empresa["ie"] = so_digitos(m[1])
        if "cnpj" not in empresa and (m := re.search(r"C\.?N\.?P\.?J\.?[^:\dA-Z]*:?\s*" + RE_CNPJ.pattern, linha, re.I)):
            empresa["cnpj"] = so_cnpj(m[1])
        if not data_inv and re.search(r"Estoques?\s+existentes?\s+em", linha, re.I):
            m = re.search(r"(\d{2})/(\d{2})/(\d{4})", linha)
            data_inv = data_ddmmaaaa(m[0]) if m else None
        elif re.match(r"\s*Total\s*:", linha, re.I):
            total = num(linha.split(":", 1)[1], br=True)
    return Fonte(caminho, "Relatório de inventário", itens, empresa, data_inv, total)


def ler_fortes_fs(caminho, conteudo):
    linhas = [l.rstrip("\r").split("|") for l in conteudo.splitlines() if "|" in l]
    grupos, unidades, produtos, itens, empresa, data_inv = {}, {}, {}, [], {}, None
    for c in linhas:
        reg = c[0]
        if reg == "CAB" and len(c) > 6:
            empresa["nome"] = texto(c[4])
            data_inv = data_ddmmaaaa(c[6])
            if cnpj := next((so_cnpj(x) for x in c[1:] if cnpj_valido(x)), None):
                empresa["cnpj"] = cnpj
        elif reg == "GRP" and len(c) > 4:
            grupos[c[1].lstrip("0")] = c[4]
        elif reg == "UND" and len(c) > 2 and re.fullmatch(r"[A-Za-z0-9]{1,6}", c[1]):
            unidades.setdefault(c[1].upper(), texto(c[2]))
        elif reg == "PRO" and len(c) > 9:
            produtos[c[1]] = c
    for c in linhas:
        if c[0] != "IIV" or len(c) < 9:
            continue
        p = produtos.get(c[3])
        cod_proprio = texto(p[3]) if p and p[3].strip() else c[3]
        item = Item(
            codigo=cod_proprio, unidade=c[5], qtd=num(c[4]), vl_unit=num(c[6]),
            ind_prop={"01": "0", "02": "1", "03": "2"}.get(c[7], "0"),
            aliases={c[3]}, origem=f"IIV {c[3]}",
        )
        if p:
            item.descricao, item.ncm = texto(p[2]), so_digitos(p[4])
            item.unidade = item.unidade or p[5]
            item.tipo = grupos.get(p[9].lstrip("0"), "")
        data_inv = data_inv or data_ddmmaaaa(c[2])
        itens.append(item)
    if not itens:  # só cadastro: serve para completar
        for p in produtos.values():
            cod = texto(p[3]) if p[3].strip() else p[1]
            itens.append(Item(codigo=cod, descricao=texto(p[2]), ncm=so_digitos(p[4]), unidade=p[5],
                              tipo=grupos.get(p[9].lstrip("0"), ""), aliases={p[1]}))
    return Fonte(caminho, "Fortes (.FS)", itens, empresa, data_inv, unidades=unidades)


def ler_sped(caminho, conteudo):
    itens, cadastro, empresa, data_inv, unidades, total = [], {}, {}, None, {}, None
    for linha in conteudo.splitlines():
        c = linha.strip().strip("|").split("|")
        reg = c[0]
        if reg == "0000" and len(c) >= 15:
            empresa.update(nome=c[5], cnpj=c[6], uf=c[8], ie=c[9], cod_mun=c[10], im=c[11],
                           perfil=c[13], ind_ativ=c[14])
        elif reg == "0190" and len(c) >= 3:
            unidades[c[1]] = c[2]
        elif reg == "0200" and len(c) >= 8:
            cadastro[c[1]] = c
        elif reg == "H005" and len(c) >= 3 and not data_inv:
            data_inv, total = data_ddmmaaaa(c[1]), num(c[2])
        elif reg == "H010" and len(c) >= 7:
            c += [""] * (11 - len(c))
            itens.append(Item(codigo=c[1], unidade=c[2], qtd=num(c[3]), vl_unit=num(c[4]),
                              vl_item=num(c[5]), ind_prop=c[6] or "0", cod_cta=c[9], origem="H010"))
    for it in itens:
        c = cadastro.get(it.codigo)
        if c:
            c += [""] * (13 - len(c))
            it.descricao, it.cod_barra, it.tipo, it.ncm, it.cest = c[2], c[3], c[6], c[7], c[12]
    if not itens:
        for c in cadastro.values():
            c += [""] * (13 - len(c))
            itens.append(Item(codigo=c[1], descricao=c[2], unidade=c[5], tipo=c[6], ncm=c[7]))
    return Fonte(caminho, "SPED / bloco H", itens, empresa, data_inv, total, unidades)


# Cabeçalhos de planilha reconhecidos (sem acento, maiúsculo, por "contém")
COLUNAS = [
    ("codigo", ["COD. UTILIZADO", "CODIGO UTILIZADO", "COD UTILIZADO"]),
    ("codigo_alt", ["COD. PRODUTO", "COD PRODUTO", "CODIGO DO PRODUTO"]),
    ("codigo", ["CODIGO", "COD.", "COD ", "REFERENCIA", "SKU", "ITEM CODE", "CODE"]),
    ("descricao", ["DESCRI", "PRODUTO", "NOME"]),
    ("unidade", ["UNIDADE", "UND", "UNID", "UN.", "UN ", "UNIT "]),
    ("ncm", ["NCM", "CLASSIFICACAO"]),
    ("cest", ["CEST"]),
    ("cod_cta", ["CONTA"]),
    ("qtd", ["QTD. NO ESTAB", "QUANTIDADE", "QTDE", "QTD", "SALDO", "ESTOQUE ATUAL", "ESTOQUE", "QUANTITY", "QTY"]),
    ("qtd_terc", ["QTD. EM TERCEIROS", "QTD. FORA"]),
    ("vl_unit", ["VALOR UNIT", "VL UNIT", "VL. UNIT", "CUSTO UNIT", "UNITARIO", "CUSTO", "UNIT COST"]),
    ("vl_item", ["VALOR TOTAL", "VL TOTAL", "VL. TOTAL", "TOTAL", "AMOUNT"]),
    ("icms_rec", ["ICMS"]),
    ("tipo", ["TIPO"]),
]


# Palavras que tiram a coluna de um campo mesmo quando um termo dele aparece no nome:
# "Custo Total" não é valor unitário, "Cód. Barras" e "Código NCM" não são o código do item.
NAO_E = {
    "codigo": ("BARRA", "BARCODE", "EAN", "GTIN", "NCM", "CEST", "CONTA"),
    "codigo_alt": ("BARRA", "EAN", "GTIN"),
    "descricao": ("COD",),
    "unidade": ("VALOR", "VL", "CUSTO", "QTD", "QUANT", "COST", "PRICE", "PRECO"),
    "qtd": ("FORA", "TERCEIRO", "VALOR", "VL", "R$", "CUSTO", "MINIMO", "MAXIMO"),
    "vl_unit": ("TOTAL",),
    "vl_item": ("UNIT", "QTD", "QUANT"),
}


def mapear_colunas(cab):
    """Cada campo fica com a coluna que casa com o termo MAIS específico dele (os termos
    estão em ordem de preferência), não com a primeira coluna que casa com qualquer termo."""
    mapa = {}
    nomes = [sem_acento(c).strip() + " " for c in cab]
    for chave, termos in COLUNAS:
        if chave in mapa:
            continue
        for t in termos:
            i = next((i for i, nome in enumerate(nomes)
                      if nome.strip() and i not in mapa.values() and t in nome
                      and not any(x in nome for x in NAO_E.get(chave, ()))), None)
            if i is not None:
                mapa[chave] = i
                break
    return mapa


def linhas_planilha(caminho):
    ext = caminho.suffix.lower()
    if ext == ".csv":
        txt = ler_texto(caminho)
        sep = ";" if txt.count(";") >= txt.count(",") else ","
        yield [list(r) for r in csv.reader(txt.splitlines(), delimiter=sep)]
        return
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                yield [["" if v is None else v for v in r] for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
        return
    import xlrd
    with open(os.devnull, "w") as nada:
        wb = xlrd.open_workbook(caminho, logfile=nada)
    for sh in wb.sheets():
        linhas = []
        for r in range(sh.nrows):
            linha = []
            for c in range(sh.ncols):
                v = sh.cell_value(r, c)
                if sh.cell_type(r, c) == xlrd.XL_CELL_DATE:  # data no .xls é número de série
                    v = xlrd.xldate.xldate_as_datetime(v, wb.datemode)
                linha.append(v)
            linhas.append(linha)
        yield linhas


RE_TOTAL = re.compile(r"(SUB-?\s*)?TOTA(L|IS)\b|SOMA\b|TRANSPORT|A TRANSPORTAR")


def cabecalho(linhas):
    """Linha do cabeçalho: a primeira, entre as 30 de cima, com um código e mais 2 campos conhecidos."""
    for i, l in enumerate(linhas[:30]):
        m = mapear_colunas(l)
        if len(m) >= 3 and ("codigo" in m or "codigo_alt" in m):
            return i, m
    return None, None


RE_ROTULO_NOME = re.compile(r"(EMPRESA|RAZAO SOCIAL|FIRMA|NOME EMPRESARIAL|CONTRIBUINTE)\s*:?\s*")


def titulo_da_planilha(linhas):
    """Nome, CNPJ, data e total do inventário escritos nas linhas acima do cabeçalho
    ("INVENTÁRIO EM 31/12/2025", "Empresa: ... CNPJ 11.222.333/0001-81", "Total do inventário: 2.317,56")."""
    empresa, data_inv, total = {}, None, None
    for l in linhas:
        celulas = [v for v in l if v not in ("", None)]
        if celulas and isinstance(celulas[0], str):
            rotulo = sem_acento(celulas[0]).strip()
            # "Empresa:" numa célula e o nome na seguinte, ou "Empresa: NOME - CNPJ ..." na mesma
            if "nome" not in empresa and (m := RE_ROTULO_NOME.match(rotulo)):
                resto = celulas[0][m.end():].strip() if len(rotulo) > m.end() else (
                    celulas[1] if len(celulas) > 1 and isinstance(celulas[1], str) else "")
                resto = re.split(r"\s+-?\s*C\.?N\.?P\.?J", resto, flags=re.I)[0].strip(" -")
                if resto:
                    empresa["nome"] = texto(resto)
            if total is None and re.match(r"TOTA(L|IS)\b", rotulo):
                total = next((num(v) for v in celulas[1:] if isinstance(v, (int, float))), None)
        for v in l:
            if isinstance(v, (date,)) and not data_inv:
                txt = sem_acento(" ".join(str(x) for x in l))
                if re.search(r"INVENT|ESTOQUE|POSICAO|SALDO", txt):
                    data_inv = v.date() if hasattr(v, "date") else v
        txt = " ".join(str(v) for v in l if v not in ("", None))
        limpo = sem_acento(txt)
        if "cnpj" not in empresa:
            for m in RE_CNPJ.finditer(txt.upper()):
                if cnpj_valido(m[1]):
                    empresa["cnpj"] = so_cnpj(m[1])
                    break
        if not data_inv and re.search(r"INVENT|ESTOQUE|POSICAO|SALDO", limpo) and not re.search(r"EMITID|IMPRESS|GERAD", limpo):
            if m := re.search(r"(\d{2})/(\d{2})/(\d{4})", txt):
                data_inv = data_ddmmaaaa(m[0])
    return empresa, data_inv, total


def ler_planilha(caminho):
    abas = linhas_planilha(caminho)
    try:
        for linhas in abas:  # a primeira aba que tem um inventário
            ini, mapa = cabecalho(linhas)
            if ini is not None:
                break
        else:
            raise ValueError("não achei a linha de cabeçalho (Código, Descrição, Quantidade...)")
    finally:
        abas.close()  # fecha o arquivo já: no Windows, aberto ele fica preso
    # o total do topo serve para conferir; uma linha de total no fim da tabela vale mais
    empresa, data_inv, total_declarado = titulo_da_planilha(linhas[:ini])
    pega = lambda l, k: l[mapa[k]] if k in mapa and mapa[k] < len(l) else ""
    # Planilha com vírgula decimal (CSV brasileiro, texto no Excel): aí "1.234" é milhar
    numericas = [mapa[k] for k in ("qtd", "qtd_terc", "vl_unit", "vl_item", "icms_rec") if k in mapa]
    br = any(isinstance(l[c], str) and re.search(r"\d,\d", l[c])
             for l in linhas[ini + 1:] for c in numericas if c < len(l))
    n = lambda l, k: num(pega(l, k), br)
    itens = []
    for nlinha, l in enumerate(linhas[ini + 1:], start=ini + 2):
        cod = cod_texto(pega(l, "codigo")) or cod_texto(pega(l, "codigo_alt"))
        if not cod:
            # "Total:" numa coluna qualquer de uma linha sem código
            if any(isinstance(v, str) and RE_TOTAL.match(sem_acento(v).strip()) for v in l) and n(l, "vl_item") is not None:
                total_declarado = n(l, "vl_item")
            continue
        # Linha de total, subtotal ou transporte de página: não é item. O valor dela serve
        # para conferir o total gerado (como o "Total:" do relatório).
        if RE_TOTAL.match(sem_acento(cod)) and not texto(pega(l, "descricao")):
            if n(l, "vl_item") is not None:
                total_declarado = n(l, "vl_item")
            continue
        tipo = so_digitos(pega(l, "tipo"))
        it = Item(
            codigo=cod, descricao=texto(pega(l, "descricao")), unidade=texto(pega(l, "unidade")).upper(),
            ncm=so_digitos(pega(l, "ncm")), cest=so_digitos(pega(l, "cest")), cod_cta=cod_texto(pega(l, "cod_cta")),
            qtd=n(l, "qtd"), vl_unit=n(l, "vl_unit"), vl_item=n(l, "vl_item"),
            icms_rec=n(l, "icms_rec"), tipo=tipo.zfill(2) if tipo else "",
            origem=f"linha {nlinha}",
        )
        alt = cod_texto(pega(l, "codigo_alt"))
        if alt and alt != cod:
            it.aliases.add(alt)
        terc = n(l, "qtd_terc")
        if terc:
            it.origem += f" (qtd em terceiros/fora: {terc})"
        itens.append(it)
    # Planilha com quantidade e valor zerados em tudo é só cadastro
    if all(not i.qtd and not i.vl_unit and not i.vl_item for i in itens):
        for i in itens:
            i.qtd = i.vl_unit = i.vl_item = None
    return Fonte(caminho, "Planilha", itens, empresa, data_inv, total_declarado)


def ler_arquivo(caminho):
    ext = caminho.suffix.lower()
    if ext in (".xls", ".xlsx", ".xlsm", ".csv"):
        return ler_planilha(caminho)
    conteudo = ler_texto(caminho)
    if re.search(r"^CAB\|", conteudo, re.M) and re.search(r"^(PRO|IIV)\|", conteudo, re.M):
        return ler_fortes_fs(caminho, conteudo)
    if re.search(r"^\|(H010|0200|0000)\|", conteudo, re.M):
        return ler_sped(caminho, conteudo)
    if "REGISTRO DE INVENT" in sem_acento(conteudo[:20000]) or RE_LINHA_INV.search(conteudo):
        return ler_relatorio_inventario(caminho, conteudo)
    raise ValueError("formato não reconhecido")


# ---------------------------------------------------------------- montagem

def consultar_cnpj(cnpj):
    req = urllib.request.Request(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}", headers={"User-Agent": "gerar-sped"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    endereco = " ".join(x for x in (d.get("descricao_tipo_de_logradouro"), d.get("logradouro")) if x)
    return {
        "nome": d.get("razao_social", ""), "fantasia": d.get("nome_fantasia") or "", "uf": d.get("uf", ""),
        "cod_mun": str(d.get("codigo_municipio_ibge") or ""), "cep": so_digitos(d.get("cep")),
        "end": endereco, "num": d.get("numero") or "", "compl": d.get("complemento") or "",
        "bairro": d.get("bairro") or "", "fone": so_digitos(d.get("ddd_telefone_1"))[:11],
        "email": (d.get("email") or "").lower(),
    }


def carregar_json(caminho):
    if not caminho.exists():
        return {}
    try:  # utf-8-sig: aceita o arquivo salvo pelo Bloco de Notas
        return json.loads(caminho.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ErroConversao(f"{caminho.name} está com erro de digitação (linha {e.lineno}); corrija ou apague o arquivo.")


def dados_empresa(fontes, args, pendencias):
    """Prioridade: linha de comando > arquivo > empresas.json/consulta do CNPJ.
    O nome empresarial prefere a razão social do cadastro ao nome impresso no relatório."""
    do_arquivo = {}
    for f in reversed(fontes):
        do_arquivo.update({k: v for k, v in f.empresa.items() if v})
    da_linha = {k: getattr(args, k) for k in ("cnpj", "ie", "uf", "cod_mun", "nome", "fantasia", "perfil", "ind_ativ", "im")
                if getattr(args, k, None)}
    emp = {**do_arquivo, **da_linha}
    emp["cnpj"] = so_cnpj(emp.get("cnpj"))
    emp["ie"] = so_digitos(emp.get("ie")) or texto(emp.get("ie"))
    cnpj = emp["cnpj"]
    if not cnpj:
        pendencias.append(("empresa", "", "CNPJ não encontrado no arquivo"))
    elif not cnpj_valido(cnpj):
        pendencias.append(("empresa", cnpj, "CNPJ com dígito verificador inválido"))

    cadastro = carregar_json(CADASTRO_EMPRESAS)
    salvo = cadastro.get(cnpj, {})
    faltando = not all(salvo.get(k) for k in ("uf", "cod_mun", "cep"))
    if cnpj and faltando and not args.sem_consulta:
        try:
            salvo = {**consultar_cnpj(cnpj), **{k: v for k, v in salvo.items() if v}}
            cadastro[cnpj] = salvo
            DADOS.mkdir(parents=True, exist_ok=True)
            CADASTRO_EMPRESAS.write_text(json.dumps(cadastro, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            pendencias.append(("empresa", cnpj, f"consulta do CNPJ falhou ({e}): preencha UF, município e endereço"))
    for k, v in salvo.items():
        if v and (not emp.get(k) or (k == "nome" and "nome" not in da_linha)):
            emp[k] = v
    if not emp.get("fantasia"):
        emp["fantasia"] = do_arquivo.get("nome") or emp.get("nome", "")
    emp.setdefault("perfil", args.perfil_padrao)
    emp.setdefault("ind_ativ", args.ind_ativ_padrao)
    for k, msg in (("uf", "UF"), ("cod_mun", "código IBGE do município"), ("ie", "inscrição estadual"), ("nome", "nome empresarial")):
        if not emp.get(k):
            pendencias.append(("empresa", cnpj, f"falta {msg}"))
    return emp


def ncm_valido(ncm):
    """NCM com 8 dígitos (ou None). Com 7, o Excel guardou como número e comeu o zero
    da frente (capítulos 01 a 09): volta o zero."""
    d = so_digitos(ncm)
    if len(d) == 7:
        d = d.zfill(8)
    return d if len(d) == 8 and d != "00000000" else None


def complementar(principal, extras, pendencias, codigos_gerados):
    indice, arquivo_de = {}, {}
    for f in extras:
        for it in f.itens:
            arquivo_de[id(it)] = f.caminho.name
            for c in {it.codigo, *it.aliases}:
                indice.setdefault(c.upper(), it)

    finais, por_codigo, cadastro = [], {}, {}  # cadastro: código -> item que define o 0200
    for it in principal.itens:
        ref = indice.get(it.codigo.upper()) or next((indice[a.upper()] for a in it.aliases if a.upper() in indice), None)
        if ref:
            # O complemento não muda a quantidade do inventário, mas a divergência é avisada:
            # é o que o contador quer conferir (ex.: inventário x estoque escriturado)
            if it.qtd is not None and ref.qtd is not None and it.qtd != ref.qtd:
                pendencias.append(("item", it.codigo, f"quantidade {fmt(it.qtd, 3)} difere da de "
                                   f"{arquivo_de[id(ref)]} ({fmt(ref.qtd, 3)}): mantida a do inventário"))
            for campo in ("descricao", "ncm", "unidade", "tipo", "cod_cta", "cest", "cod_barra"):
                if not getattr(it, campo) and getattr(ref, campo):
                    setattr(it, campo, getattr(ref, campo))
            # NCM impresso como "00" ou faltando dígitos no inventário: vale o do cadastro
            if not ncm_valido(it.ncm) and ncm_valido(ref.ncm):
                it.ncm = ref.ncm
            if it.vl_unit is None and it.vl_item is None and ref.vl_unit is not None:
                it.vl_unit = ref.vl_unit
                pendencias.append(("item", it.codigo, f"valor unitário veio de {ref.origem or 'outro arquivo'}: {fmt(ref.vl_unit, 6)}"))

        if not it.codigo:
            codigos_gerados[0] += 1
            it.codigo = f"SEMCOD{codigos_gerados[0]:04d}"
            pendencias.append(("item", it.codigo, f"linha sem código no arquivo, código gerado: \"{it.origem}\""))
        # Tamanhos do leiaute antes de comparar códigos: dois códigos longos que só
        # diferem depois do 60º caractere viram o mesmo código no arquivo
        it.codigo = texto(it.codigo, 60)
        it.unidade = POR_EXTENSO.get(sem_acento(texto(it.unidade)), texto(it.unidade, 6).upper()) or "UN"

        if it.qtd is None:
            pendencias.append(("item", it.codigo, "sem quantidade: item fora do arquivo"))
            continue
        if it.vl_item is None and it.vl_unit is None:
            pendencias.append(("item", it.codigo, "sem valor unitário: item fora do arquivo"))
            continue
        if it.qtd < 0 or (it.vl_item or 0) < 0 or (it.vl_unit or 0) < 0:
            pendencias.append(("item", it.codigo, "quantidade ou valor negativo: item fora do arquivo"))
            continue
        if it.qtd == 0:
            pendencias.append(("item", it.codigo, "quantidade zero: item fora do arquivo"))
            continue
        if it.vl_item is None:
            it.vl_item = (it.qtd * it.vl_unit).quantize(DOIS, ROUND_HALF_UP)
        if it.vl_unit is None:
            it.vl_unit = it.vl_item / it.qtd

        # O mesmo item pode estar em poder do informante e de terceiros (um H010 para cada
        # IND_PROP), mas o cadastro 0200 é um só por código: descrição e unidade têm de bater.
        cad = cadastro.get(it.codigo.upper())
        if cad and not (texto(cad.descricao).upper() == texto(it.descricao).upper() and cad.unidade == it.unidade):
            n = 2
            while f"{it.codigo[:56]}-{n}".upper() in cadastro:
                n += 1
            novo = f"{it.codigo[:56]}-{n}"
            pendencias.append(("item", novo, f"código {it.codigo} repetido com outra descrição ou unidade (\"{it.descricao}\"): renomeado"))
            it.codigo = novo
        elif cad:
            it.codigo = cad.codigo  # "abc" e "ABC": o H010 usa o código exato do 0200
        chave = (it.codigo.upper(), it.ind_prop)
        igual = por_codigo.get(chave)
        if igual:
            igual.qtd += it.qtd
            igual.vl_item += it.vl_item
            igual.vl_unit = igual.vl_item / igual.qtd
            pendencias.append(("item", it.codigo, "código repetido com a mesma descrição: quantidades somadas"))
            continue
        cadastro.setdefault(it.codigo.upper(), it)
        por_codigo[chave] = it
        finais.append(it)

    for it in finais:
        if not it.descricao:
            it.descricao = f"ITEM {it.codigo}"
            pendencias.append(("item", it.codigo, "sem descrição em nenhum arquivo: usado o próprio código"))
        if ncm_valido(it.ncm):
            it.ncm = ncm_valido(it.ncm)
        elif it.ncm:
            pendencias.append(("item", it.codigo, f"NCM \"{it.ncm}\" não tem 8 dígitos: campo deixado em branco"))
            it.ncm = ""
        if it.ind_prop in ("1", "2"):
            pendencias.append(("item", it.codigo, "item com terceiros: o H010 exige COD_PART (registro 0150), não informado"))
    return finais


def montar_sped(emp, itens, unidades_desc, data_inv, ini, fim, args, escritorio):
    reg = []
    add = lambda *campos: reg.append("|" + "|".join(str(c) for c in campos) + "|")

    ano = fim.year
    cod_ver = args.cod_ver or VERSAO_LEIAUTE.get(ano, max(VERSAO_LEIAUTE.values()))
    d = lambda x: x.strftime("%d%m%Y")
    # 0000: 15 campos, vazios mantidos na posição (CPF, IM, SUFRAMA)
    add("0000", cod_ver, args.cod_fin, d(ini), d(fim), texto(emp.get("nome"), 100), emp.get("cnpj", ""), "",
        texto(emp.get("uf"), 2).upper(), texto(emp.get("ie"), 14), so_digitos(emp.get("cod_mun")),
        texto(emp.get("im")), "", emp.get("perfil", "A"), emp.get("ind_ativ", "1"))
    add("0001", "0")
    if emp.get("ind_ativ") == "0":
        add("0002", args.clas_estab_ind)
    add("0005", texto(emp.get("fantasia") or emp.get("nome"), 60), so_digitos(emp.get("cep")),
        texto(emp.get("end"), 60), texto(emp.get("num"), 10), texto(emp.get("compl"), 60),
        texto(emp.get("bairro"), 60), so_digitos(emp.get("fone"))[:11], "", texto(emp.get("email")))
    if escritorio:
        e = escritorio
        add("0100", texto(e.get("nome"), 100), so_digitos(e.get("cpf")), texto(e.get("crc"), 15),
            so_cnpj(e.get("cnpj")), so_digitos(e.get("cep")), texto(e.get("end"), 60), texto(e.get("num"), 10),
            texto(e.get("compl"), 60), texto(e.get("bairro"), 60), so_digitos(e.get("fone"))[:11], "",
            texto(e.get("email")), so_digitos(e.get("cod_mun")))
    for un in sorted({i.unidade for i in itens}):
        add("0190", un, texto(unidades_desc.get(un) or UNIDADES.get(un) or un))
    tipo_padrao = args.tipo_item
    vistos = set()
    for it in itens:
        if it.codigo.upper() in vistos:  # mesmo item em poder de terceiros: um 0200 só
            continue
        vistos.add(it.codigo.upper())
        add("0200", it.codigo, texto(it.descricao), texto(it.cod_barra), "", it.unidade,
            (it.tipo or tipo_padrao).zfill(2), it.ncm, "", "", "", "", it.cest if len(it.cest) == 7 else "")
    add("0990", len(reg) + 1)

    for blc in ("B", "C", "D"):
        add(f"{blc}001", "1")
        add(f"{blc}990", "2")
    # E100/E110 são obrigatórios em todo arquivo; sem movimento, apuração zerada
    add("E001", "0")
    add("E100", d(ini), d(fim))
    add("E110", *(["0"] * 14))
    add("E990", "4")
    add("G001", "1")
    add("G990", "2")

    h_ini = len(reg)
    total = sum((it.vl_item.quantize(DOIS, ROUND_HALF_UP) for it in itens), Decimal(0))
    add("H001", "0" if itens else "1")
    if itens:
        add("H005", d(data_inv), fmt(total, 2), args.mot_inv)
        for it in itens:
            vl_item = it.vl_item.quantize(DOIS, ROUND_HALF_UP)
            vl_ir = vl_item - (it.icms_rec or 0)
            add("H010", it.codigo, it.unidade, fmt(it.qtd, 3), fmt(it.vl_unit, 6), fmt(vl_item, 2),
                it.ind_prop, "", "", texto(it.cod_cta), fmt(vl_ir, 2))
    add("H990", len(reg) - h_ini + 1)

    add("K001", "1")
    add("K990", "2")
    add("1001", "0")
    add("1010", *(["N"] * 13))
    add("1990", "3")

    contagem = {}
    for linha in reg:
        r = linha[1:5]
        contagem[r] = contagem.get(r, 0) + 1
    tipos = list(contagem) + ["9001", "9900", "9990", "9999"]
    qtd_9900 = len(tipos)
    contagem.update({"9001": 1, "9900": qtd_9900, "9990": 1, "9999": 1})
    add("9001", "0")
    for r in tipos:
        add("9900", r, contagem[r])
    add("9990", qtd_9900 + 3)
    add("9999", len(reg) + 1)
    return reg, total


def periodo(args, data_inv):
    if args.periodo:
        m = re.fullmatch(r"\s*(\d{1,2})\s*[/\-.]\s*(\d{4})\s*", args.periodo)
        if not m or not 1 <= int(m[1]) <= 12:
            raise ErroConversao(f"Período \"{args.periodo}\" inválido: use mês/ano, por exemplo 02/2026.", "periodo")
        mes, ano = int(m[1]), int(m[2])
    else:
        mes, ano = data_inv.month, data_inv.year
    ini = date(ano, mes, 1)
    return ini, date(ano, mes, calendar.monthrange(ano, mes)[1])


class ErroConversao(Exception):
    """Erro que o usuário resolve: a mensagem vai para a tela. `campo` é o campo do formulário
    que resolve o problema (o painel destaca e põe o cursor nele)."""

    def __init__(self, mensagem, campo=None):
        super().__init__(mensagem)
        self.campo = campo


@dataclass
class Resultado:
    principal: Fonte
    emp: dict
    itens: list
    pendencias: list
    registros: list
    total: Decimal
    data_inv: date
    ini: date
    fim: date

    @property
    def base(self):
        return f"SPED_{self.emp.get('cnpj') or self.principal.caminho.stem}_{self.data_inv:%d%m%Y}"


def converter(principal, extras, args):
    pendencias, gerados = [], [0]
    if not principal.itens:
        raise ErroConversao(f"{principal.caminho.name} não tem nenhum item: confira se é o arquivo certo "
                            "ou se a planilha foi preenchida.")
    # Sem valor em nenhum arquivo não há inventário a gerar: avisa isso antes de pedir a data,
    # que não adiantaria nada (ex.: Estoque Escriturado, que só tem quantidades)
    if principal.itens and not any(f.tem_valores for f in [principal, *extras]):
        tem_qtd = any(i.qtd is not None for i in principal.itens)
        raise ErroConversao(
            f"{principal.caminho.name} " + ("só tem quantidades, sem valor unitário nem total" if tem_qtd
                                           else "não tem quantidades nem valores (é só cadastro)")
            + ", e o SPED exige o valor "
            "de cada item. Junte um arquivo com os valores do mesmo inventário (o relatório de inventário, "
            "o .FS da Fortes ou uma planilha com custo unitário): ele completa os valores pelo código.")
    if args.data_inv:
        data_inv = data_ddmmaaaa(args.data_inv)
        if not data_inv:
            raise ErroConversao(f"Data do inventário \"{args.data_inv}\" inválida: use dia/mês/ano, por exemplo 31/12/2025.", "data_inv")
    else:
        data_inv = principal.data_inv
    if not data_inv:
        for f in extras:
            data_inv = data_inv or f.data_inv
    if not data_inv:
        raise ErroConversao("O arquivo não informa a data do inventário: digite em \"Data do inventário\" (ex.: 31/12/2025) "
                            "e clique em Gerar de novo.", "data_inv")

    emp = dados_empresa([principal, *extras], args, pendencias)
    itens = complementar(principal, extras, pendencias, gerados)
    unidades = {}
    for f in [*extras, principal]:
        unidades.update(f.unidades)
    ini, fim = periodo(args, data_inv)
    if data_inv > fim:
        raise ErroConversao(f"A data do inventário ({data_inv:%d/%m/%Y}) é depois do fim do período ({fim:%d/%m/%Y}).", "periodo")
    escritorio = carregar_json(ESCRITORIO)
    if not (escritorio.get("nome") and escritorio.get("cpf")):
        escritorio = {}
        pendencias.append(("arquivo", "0100", "dados do escritório não cadastrados: registro 0100 (contabilista) não gerado"))

    if not itens:
        # Sem isso sairia um SPED com o bloco H vazio, que parece certo e não tem inventário
        motivo = collections.Counter(m for t, _, m in pendencias if t == "item").most_common(1)
        dica = (" O arquivo não tem valores: use-o junto com o inventário (relatório, .FS ou planilha com valor unitário)."
                if not any(f.tem_valores for f in [principal, *extras]) else "")
        raise ErroConversao(f"Nenhum item de {principal.caminho.name} pôde entrar no arquivo ({len(principal.itens)} lido(s))"
                            + (f" ({motivo[0][0]})" if motivo else "") + "." + dica)

    reg, total = montar_sped(emp, itens, unidades, data_inv, ini, fim, args, escritorio)

    if principal.total_declarado is not None:
        dif = total - principal.total_declarado
        if dif:
            pendencias.append(("total", "", f"total gerado {fmt(total, 2)} difere do total do arquivo {fmt(principal.total_declarado, 2)} em {fmt(dif, 2)}"))
    return Resultado(principal, emp, itens, pendencias, reg, total, data_inv, ini, fim)


def numero_dominio(valor):
    """Número como o Excel em português escreve ao gravar texto: vírgula decimal, sem
    milhar e sem zeros sobrando ("280", "4,27", "0,0689")."""
    if valor is None:
        return "0"
    t = f"{valor.normalize():f}"
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t.replace(".", ",")


def linhas_dominio(res):
    """Arquivo de importação do inventário no Domínio: o mesmo "produtos.txt" que a macro da
    Planilha Modelo grava. Sem cabeçalho, separado por ";", na ordem: código, descrição,
    unidade, NCM, CEST, conta contábil, quantidade, valor unitário, valor total, ICMS recuperável."""
    linhas = []
    for it in res.itens:
        vl_item = it.vl_item.quantize(DOIS, ROUND_HALF_UP)
        campos = [it.codigo, it.descricao, it.unidade, it.ncm, it.cest if len(it.cest) == 7 else "", texto(it.cod_cta),
                  numero_dominio(it.qtd.quantize(TRES, ROUND_HALF_UP)), numero_dominio(it.vl_unit.quantize(SEIS, ROUND_HALF_UP)),
                  numero_dominio(vl_item), numero_dominio(it.icms_rec.quantize(DOIS, ROUND_HALF_UP) if it.icms_rec else None)]
        linhas.append(";".join(texto(c).replace(";", ",") for c in campos))
    return linhas


def gravar(res, pasta):
    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    arq = pasta / f"{res.base}.txt"
    with open(arq, "w", encoding="latin-1", errors="replace", newline="\r\n") as fp:
        fp.write("\n".join(res.registros) + "\n")
    # Mesmo nome com DOMINIO_ no lugar de SPED_ (a lista de gerados só procura SPED_*.txt)
    arq_dom = pasta / f"DOMINIO_{res.base.removeprefix('SPED_')}.txt"
    with open(arq_dom, "w", encoding="cp1252", errors="replace", newline="\r\n") as fp:
        fp.write("\n".join(linhas_dominio(res)) + "\n")
    arq_pend = pasta / f"{res.base}_pendencias.csv"
    if res.pendencias:
        with open(arq_pend, "w", encoding="utf-8-sig", newline="") as fp:
            w = csv.writer(fp, delimiter=";")
            w.writerow(["tipo", "codigo", "pendencia"])
            w.writerows(res.pendencias)
    elif arq_pend.exists():
        arq_pend.unlink()
    return arq, arq_pend, arq_dom


def processar(principal, extras, args):
    try:
        res = converter(principal, extras, args)
    except ErroConversao as e:
        raise SystemExit(str(e))
    arq, arq_pend, arq_dom = gravar(res, args.saida or DADOS / "saida")
    emp = res.emp
    print(f"\n{principal.caminho.name}  ->  {arq}")
    print(f"  empresa: {emp.get('nome') or '?'}  CNPJ {emp.get('cnpj') or '?'}  IE {emp.get('ie') or '?'}  UF {emp.get('uf') or '?'}")
    print(f"  inventário em {res.data_inv:%d/%m/%Y}, período {res.ini:%d/%m/%Y} a {res.fim:%d/%m/%Y}, leiaute {res.registros[0].split('|')[2]}")
    print(f"  {len(res.itens)} itens, total {fmt(res.total, 2)}, {len(res.registros)} linhas")
    if principal.total_declarado is not None:
        print(f"  total no arquivo de origem: {fmt(principal.total_declarado, 2)}")
    if res.pendencias:
        print(f"  {len(res.pendencias)} pendências em {arq_pend.name}")
    print(f"  para importar no Domínio: {arq_dom.name}")
    return arq


def escolher_principal(fontes):
    com_qtd = [f for f in fontes if any(i.qtd is not None for i in f.itens)]
    return next((f for f in com_qtd if f.tem_valores), com_qtd[0] if com_qtd else fontes[0])


def criar_parser():
    ap = argparse.ArgumentParser(description="Gera o SPED Fiscal (EFD ICMS/IPI) com o inventário no bloco H.")
    ap.add_argument("arquivos", nargs="+", help="inventário e arquivos de apoio (relatório, .FS, bloco H, planilhas)")
    ap.add_argument("--separado", action="store_true", help="um SPED por arquivo, sem cruzar os dados")
    ap.add_argument("--saida", help="pasta de saída (padrão: ./saida)")
    ap.add_argument("--data-inv", help="data do inventário, se o arquivo não trouxer (dd/mm/aaaa)")
    ap.add_argument("--periodo", help="mês do arquivo (mm/aaaa); padrão: mês do inventário")
    ap.add_argument("--cnpj"), ap.add_argument("--ie"), ap.add_argument("--uf"), ap.add_argument("--im")
    ap.add_argument("--cod-mun", help="código IBGE do município (7 dígitos)")
    ap.add_argument("--nome", help="nome empresarial")
    ap.add_argument("--fantasia")
    ap.add_argument("--perfil", choices=["A", "B", "C"])
    ap.add_argument("--perfil-padrao", default="A", help=argparse.SUPPRESS)
    ap.add_argument("--ind-ativ", choices=["0", "1"], help="0 industrial, 1 outros")
    ap.add_argument("--ind-ativ-padrao", default="1", help=argparse.SUPPRESS)
    ap.add_argument("--clas-estab-ind", default="00", help="registro 0002 (só se --ind-ativ 0)")
    ap.add_argument("--tipo-item", default="00", help="TIPO_ITEM quando o arquivo não informa (padrão 00 revenda)")
    ap.add_argument("--mot-inv", default="01", choices=["01", "02", "03", "04", "05", "06"])
    ap.add_argument("--cod-fin", default="0", choices=["0", "1"])
    ap.add_argument("--cod-ver", help="força o código da versão do leiaute")
    ap.add_argument("--sem-consulta", action="store_true", help="não consulta o CNPJ na internet")
    return ap


def opcoes(**valores):
    """Opções com os mesmos padrões da linha de comando (para o painel)."""
    args = criar_parser().parse_args(["-"])
    for k, v in valores.items():
        setattr(args, k, v)
    return args


def main():
    if sys.stdout:  # pythonw roda sem console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = criar_parser().parse_args()

    fontes = []
    for a in args.arquivos:
        p = Path(a)
        try:
            f = ler_arquivo(p)
        except Exception as e:
            print(f"{p.name}: não foi possível ler ({e})")
            continue
        print(f"{p.name}: {f.tipo}, {len(f.itens)} itens{'' if f.tem_valores else ' (sem valores)'}")
        fontes.append(f)
    if not fontes:
        raise SystemExit("Nenhum arquivo lido.")

    if args.separado:
        for f in fontes:
            processar(f, [], args)
        return
    principal = escolher_principal(fontes)
    processar(principal, [f for f in fontes if f is not principal], args)


if __name__ == "__main__":
    main()
