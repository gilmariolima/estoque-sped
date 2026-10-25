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
import dataclasses
import io
import itertools
import json
import os
import re
import sys
import unicodedata
import urllib.error
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
VERSAO_LEIAUTE = {2018: "012", 2019: "013", 2020: "014", 2021: "015", 2022: "016", 2023: "017", 2024: "018",
                  2025: "019", 2026: "020"}

# TIPO_ITEM do 0200 (Guia Prático, tabela do campo 07)
TIPOS_ITEM = {"00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "99"}

# Código IBGE da UF: os 2 primeiros dígitos do código do município
UF_IBGE = {"RO": "11", "AC": "12", "AM": "13", "RR": "14", "PA": "15", "AP": "16", "TO": "17", "MA": "21", "PI": "22",
           "CE": "23", "RN": "24", "PB": "25", "PE": "26", "AL": "27", "SE": "28", "BA": "29", "MG": "31", "ES": "32",
           "RJ": "33", "SP": "35", "PR": "41", "SC": "42", "RS": "43", "MS": "50", "MT": "51", "GO": "52", "DF": "53"}

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
    cod_part: str = ""  # participante (0150) do item em poder de terceiros ou de terceiros
    txt_compl: str = ""
    aliases: set = field(default_factory=set)
    origem: str = ""
    pendencias: list = field(default_factory=list)  # avisos do item, com o código final no fim


@dataclass
class Fonte:
    caminho: Path
    tipo: str
    itens: list
    empresa: dict = field(default_factory=dict)
    data_inv: date | None = None
    total_declarado: Decimal | None = None
    unidades: dict = field(default_factory=dict)
    nao_lidas: list = field(default_factory=list)  # linhas com cara de item que o leitor não entendeu
    avisos: list = field(default_factory=list)  # o que o leitor deixou de lado ou supôs sobre o arquivo
    participantes: dict = field(default_factory=dict)  # COD_PART -> dados do 0150
    cadastro: list = field(default_factory=list)  # produtos cadastrados que não estão no inventário do arquivo

    @property
    def tem_valores(self):
        return any(i.vl_unit is not None or i.vl_item is not None for i in self.itens)


# ---------------------------------------------------------------- utilidades

RE_MILHAR = re.compile(r"-?\d{1,3}(\.\d{3})+")
RE_MILHAR_EN = re.compile(r"-?\d{1,3}(,\d{3})+")


def num(txt, br=False, en=False):
    """Converte '1.234,56', '1,234.56', '1234.5600', 1234.5 ou '' em Decimal (ou None).

    Com vírgula e ponto, o que vem por último é o decimal. Só com um deles, ele é decimal
    ("2.5", "2,5"), a não ser que seja claramente milhar ("1.234.567", "1,234,567") ou que o
    arquivo use o outro como decimal: br=True (vírgula decimal) faz "1.234" valer mil e
    duzentos e trinta e quatro; en=True (ponto decimal) faz o mesmo com "1,234".
    Aceita o negativo contábil: "(1.234,56)" e "1.234,56-"."""
    if txt is None or isinstance(txt, bool):
        return None
    if isinstance(txt, (int, float)):
        d = Decimal(str(txt))
        return d if d.is_finite() else None
    t = re.sub(r"R\$|\s", "", str(txt).strip())  # "R$ 1.234,56" vindo como texto do Excel
    negativo = False
    if len(t) > 2 and t[0] == "(" and t[-1] == ")":
        t, negativo = t[1:-1], True
    elif len(t) > 1 and t[-1] == "-":
        t, negativo = t[:-1], True
    if not t or t == "-":
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:
        t = t.replace(",", "") if (en or t.count(",") > 1) and RE_MILHAR_EN.fullmatch(t) else t.replace(",", ".")
    elif (br or t.count(".") > 1) and RE_MILHAR.fullmatch(t):
        t = t.replace(".", "")
    try:
        d = Decimal(t)
    except Exception:
        return None
    if not d.is_finite():
        return None
    return -d if negativo else d


def estilo_numeros(valores, padrao):
    """Separador decimal de uma planilha, pelos números escritos como texto: "br" (1.234,56),
    "en" (1,234.56) ou `padrao` quando nenhum número mostra (só "1.500", "20"...).
    Devolve (estilo, aviso), e o aviso explica a suposição quando ela muda algum número."""
    br = en = ambiguos = 0
    exemplo = ""
    for v in valores:
        t = re.sub(r"R\$|\s|[()]", "", v).strip("-")
        if not t or not t[0].isdigit():
            continue
        if "," in t and "." in t:
            if t.rfind(",") > t.rfind("."):
                br += 1
            else:
                en += 1
        elif re.fullmatch(r"\d+,\d+", t):
            if re.fullmatch(r"[1-9]\d{0,2},\d{3}", t):
                ambiguos += 1
                exemplo = exemplo or t
            else:
                br += 1
        elif re.fullmatch(r"\d+\.\d+", t):
            if re.fullmatch(r"[1-9]\d{0,2}\.\d{3}", t):
                ambiguos += 1
                exemplo = exemplo or t
            else:
                en += 1
        elif RE_MILHAR.fullmatch(t):
            br += 1
        elif RE_MILHAR_EN.fullmatch(t):
            en += 1
    estilo = "br" if br > en else "en" if en > br else padrao
    aviso = ""
    if br and en:
        aviso = (f"a planilha mistura números com vírgula decimal ({br}) e com ponto decimal ({en}): "
                 f"lidos como {'1.234,56' if estilo == 'br' else '1,234.56'}; confira os valores")
    elif ambiguos and not (br or en):
        lido = num(exemplo, br=estilo == "br", en=estilo == "en")
        aviso = (f"números como \"{exemplo}\" podem ser milhar ou decimal; lidos como {str(lido).replace('.', ',')}"
                 f" ({'formato brasileiro' if estilo == 'br' else 'formato inglês'}): confira")
    return estilo, aviso


def fmt(valor, casas):
    q = {2: DOIS, 3: TRES, 6: SEIS}[casas]
    return f"{valor.quantize(q, ROUND_HALF_UP):f}".replace(".", ",")


def so_digitos(txt):
    return re.sub(r"\D", "", str(txt or ""))


# Caracteres do Windows-1252 e do Unicode que não existem no ISO-8859-1 do SPED, pelo equivalente
TROCAS = str.maketrans({
    "–": "-", "—": "-", "‒": "-", "−": "-", "‐": "-", "‑": "-", "‘": "'", "’": "'", "‚": ",", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "…": "...", "€": "EUR", "•": "-", "·": "-", "™": "(TM)",
    "‰": "%o", "‹": "<", "›": ">", "ˆ": "^", "˜": "~", " ": " ", " ": " ", " ": " ",
    "​": "", "﻿": "", "­": "",
})


def _latin1(c):
    """Um caractere fora do ISO-8859-1: a letra sem o acento (ŝ -> s) ou '?'."""
    base = unicodedata.normalize("NFKD", c).encode("latin-1", "ignore").decode("latin-1")
    return base or "?"


def texto(txt, tam=None):
    """Campo C: sem pipe, sem caracteres de controle, sem espaços nas pontas, só com
    caracteres do ISO-8859-1 (aspas curvas e travessão do Word viram " e -)."""
    t = "" if txt is None else str(txt)
    if not t.isascii():
        t = unicodedata.normalize("NFC", t).translate(TROCAS)
        if any(ord(c) > 255 for c in t):
            t = "".join(c if ord(c) < 256 else _latin1(c) for c in t)
    t = re.sub(r"[\x00-\x1f\x7f-\x9f|]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:tam].strip() if tam else t


def perde_caractere(txt):
    """True se algum caractere do texto não tem equivalente no ISO-8859-1 e vira '?' no SPED."""
    t = "" if txt is None else str(txt)
    if t.isascii():
        return False
    t = unicodedata.normalize("NFC", t).translate(TROCAS)
    return any(ord(c) > 255 and _latin1(c) == "?" for c in t)


def cod_texto(valor):
    """Código vindo de planilha: 20.0 vira '20' (o .xls guarda todo número como decimal)."""
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    if isinstance(valor, bool):
        return ""
    return texto(valor)


def bruto(valor):
    """Código ou descrição como veio, só com os espaços arrumados: a limpeza para o SPED
    (texto()) fica para depois, quando o app consegue avisar o que mudou."""
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    if valor is None or isinstance(valor, bool):
        return ""
    return " ".join(str(valor).split())


def sem_acento(t):
    return unicodedata.normalize("NFKD", str(t)).encode("ascii", "ignore").decode().upper()


def ler_texto(caminho):
    dados = caminho.read_bytes()
    for cod in ("utf-8-sig", "cp1252", "latin-1"):  # -sig: sem o BOM, o "|0000|" da 1ª linha é achado
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


def cpf_valido(c):
    c = so_digitos(c)
    if len(c) != 11 or len(set(c)) == 1:
        return False
    for tam in (9, 10):
        dv = sum(int(a) * b for a, b in zip(c[:tam], range(tam + 1, 1, -1))) * 10 % 11 % 10
        if int(c[tam]) != dv:
            return False
    return True


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

# Linha de item: NCM (que pode faltar), código e descrição, unidade, quantidade, unitário e total
RE_LINHA_INV = re.compile(
    r"^(?:(?P<ncm>\d{2,8})\s+|\s*)(?P<texto>\S.*?)\s{2,}(?P<un>\S{1,6})\s+(?P<qtd>-?[\d.]+,\d+)\s+"
    r"(?P<unit>-?[\d.]+,\d+)\s+(?P<total>-?[\d.]+,\d+)\s*$",
    re.M,  # a detecção procura em todo o arquivo, não só na primeira linha
)
# Fim de linha com três números do relatório: tem cara de item mesmo quando o resto não casa
RE_TRES_NUMEROS = re.compile(r"(-?[\d.]+,\d+\s+){2}-?[\d.]+,\d+\s*$")
# "P/" de "para" na descrição ("P/ RELOGIO", "PULS.P/REL."): tem barra, mas não é código
RE_PARA = re.compile(r"(?<![A-Z0-9])P/", re.I)
# Texto que é só um rótulo de total ("TOTAL:", "TOTAL DO GRUPO", "A TRANSPORTAR"), não um produto
RE_TOTAL_REL = re.compile(r"((SUB-?\s*)?TOTA(L|IS)(\s+(DO|DA|DE|DOS|DAS|GERAL)\b.*)?|A TRANSPORTAR|TRANSPORTE)\s*:?")


def separar_codigo(txt):
    """No relatório o código e a descrição vêm juntos ("U3424 696 041/22M LEXUS ...").
    O código são os primeiros termos que têm número ou barra; um prefixo curto só de
    letras entra quando o termo seguinte parece código ("CO 11420JS/02", "BG 99100-003-A",
    "GC 1999885"), mas não num "KIT 2 PEÇAS". O "-" que separa código e descrição
    ("5095 - EXPOSITOR", "5102- ISOTONIC") não fica em nenhum dos dois.

    O "P/" de "para" é descrição, mesmo tendo barra ("PULS.P/REL.", "CAIXA P/"). Sem número no
    começo, o código é o que vem antes de " - " ("BIP LITE BLACK - SMARTWATCH") ou um termo com
    hífen curto ("MW-CX CAIXA EM ACO").

    Devolve (código, descrição, duvidoso): duvidoso quando o código pode ter levado parte da
    descrição (veio do " - " ou tem uma palavra comprida, como "01CAIXA"), e aí vai para as pendências."""
    termos = txt.split()
    tem_cod = lambda t: bool(re.search(r"[\d/]", t)) and not RE_PARA.search(t)
    n = 0
    if len(termos) > 1 and re.fullmatch(r"[A-Z]{1,3}", termos[0]) and tem_cod(termos[1]) and len(termos[1]) >= 4:
        n = 2
    elif termos and re.fullmatch(r"[A-Z]{1,4}-[A-Z0-9]{1,4}", termos[0]):
        n = 1
    while n < len(termos) - 1 and tem_cod(termos[n]):
        n += 1
    if n == 0 and (m := re.match(r"(\S+(?: \S+){0,3}) [-–] (\S.*)", " ".join(termos))) and len(m[1]) <= 25:
        return m[1], m[2], True
    codigo = " ".join(termos[:n]).rstrip("-–").rstrip()
    descricao = " ".join(termos[n:]).lstrip("-–").lstrip()
    partes = codigo.split()
    duvidoso = any(re.search(r"\d[A-Z]{5,}$", t) for t in partes) or any(re.search(r"[A-Z]{5,}", t) for t in partes[1:])
    return codigo, descricao, duvidoso


def ler_relatorio_inventario(caminho, conteudo):
    itens, empresa, data_inv, total, nao_lidas = [], {}, None, None, []
    com_espaco = []  # códigos de mais de um termo separados com segurança: um aviso só, no arquivo
    tamanhos = []  # tamanho do campo código + descrição de cada item (o sistema corta num limite)
    duvidosos = collections.defaultdict(set)  # código separado por suposição -> descrições em que aparece
    for linha in conteudo.splitlines():
        m = RE_LINHA_INV.match(linha)
        if m and RE_TOTAL_REL.fullmatch(sem_acento(m["texto"]).strip()):
            m = None  # "TOTAL DO GRUPO:  UN  ..." com três números não é item
        elif not m and (re.match(r"^\d{2,8}\s+\S", linha) or RE_TRES_NUMEROS.search(linha)):
            nao_lidas.append(texto(linha))
        if m:
            codigo, descr, duvidoso = separar_codigo(m["texto"])
            it = Item(
                codigo=codigo, descricao=descr or m["texto"], unidade=m["un"], ncm=m["ncm"] or "",
                qtd=num(m["qtd"], br=True), vl_unit=num(m["unit"], br=True), vl_item=num(m["total"], br=True),
                origem=texto(m["texto"]),
            )
            tamanhos.append((len(m["texto"].strip()), texto(m["texto"])))
            if duvidoso:  # o código pode ter levado parte da descrição: o contador confere este
                it.pendencias.append(f"código \"{codigo}\" separado da descrição pelo app (linha \"{texto(m['texto'])}\"): confira")
                duvidosos[codigo].add(texto(descr))
            elif " " in codigo:
                com_espaco.append(codigo)
            itens.append(it)
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
    avisos = []
    if com_espaco:
        avisos.append(f"{len(com_espaco)} código(s) com espaço separados da descrição pelos termos com número ou barra "
                      f"(ex.: {', '.join(com_espaco[:3])}): confira a coluna Código na lista de itens")
    # O sistema corta código + descrição num tamanho fixo: muitas linhas exatamente no maior
    # tamanho são descrições cortadas ("GAIVOTA COURO TRABALHA")
    if tamanhos:
        limite = max(t for t, _ in tamanhos)
        cortadas = [txt for t, txt in tamanhos if t == limite]
        # 3 ou mais e pelo menos 5% dos itens: com poucos, o mesmo tamanho pode ser coincidência
        if limite <= 60 and len(cortadas) >= 3 and len(cortadas) >= len(tamanhos) * 0.05:
            avisos.append(f"{len(cortadas)} item(ns) com código e descrição no limite de {limite} caracteres do relatório: "
                          f"a descrição pode ter sido cortada (ex.: \"{cortadas[0]}\"). Junte um cadastro com a descrição completa")
    for codigo, descricoes in duvidosos.items():
        if len(descricoes) > 1:
            avisos.append(f"\"{codigo}\" aparece com {len(descricoes)} descrições diferentes: talvez não seja código, e sim parte "
                          f"do nome do produto; os itens foram separados como {codigo}-2, {codigo}-3...")
    return Fonte(caminho, "Relatório de inventário", itens, empresa, data_inv, total, nao_lidas=nao_lidas, avisos=avisos)


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
        cod_proprio = bruto(p[3]) if p and p[3].strip() else c[3]
        item = Item(
            # o .FS sai com ponto ("12.0000") ou vírgula ("50,000") decimal, conforme a versão da Fortes
            codigo=cod_proprio, unidade=c[5], qtd=num(c[4]), vl_unit=num(c[6]),
            ind_prop={"01": "0", "02": "1", "03": "2"}.get(c[7].strip(), "0"),
            aliases={c[3]}, origem=f"IIV {c[3]}",
        )
        if c[7].strip() not in ("", "01", "02", "03"):
            item.pendencias.append(f"indicador de propriedade \"{c[7].strip()}\" desconhecido no .FS: considerado do próprio informante (0)")
        if p:
            item.descricao, item.ncm = bruto(p[2]), so_digitos(p[4])
            item.unidade = item.unidade or p[5]
            item.tipo = grupos.get(p[9].lstrip("0"), "")
        data_inv = data_inv or data_ddmmaaaa(c[2])
        itens.append(item)
    # Produtos cadastrados (PRO) sem inventário (IIV): sem IIV nenhum, o arquivo é só cadastro;
    # com IIV, os outros produtos ainda completam descrição e NCM de outro inventário
    com_iiv = {a for i in itens for a in i.aliases}
    cadastro = [Item(codigo=bruto(p[3]) if p[3].strip() else p[1], descricao=bruto(p[2]), ncm=so_digitos(p[4]),
                     unidade=p[5], tipo=grupos.get(p[9].lstrip("0"), ""), aliases={p[1]})
                for p in produtos.values() if p[1] not in com_iiv]
    if not itens:
        itens, cadastro = cadastro, []
    return Fonte(caminho, "Fortes (.FS)", itens, empresa, data_inv, unidades=unidades, cadastro=cadastro)


def campos_sped(linha, tam):
    """'|H010|A|UN|1,000||' em ['H010', 'A', 'UN', '1,000', '', ...] com `tam` posições.
    Não usa strip('|'): ele comeria os campos vazios do fim junto com o pipe final."""
    t = linha.strip()
    t = t[1:] if t.startswith("|") else t
    t = t[:-1] if t.endswith("|") else t
    c = t.split("|")
    return c + [""] * (tam - len(c))


def ler_sped(caminho, conteudo):
    itens, cadastro, empresa, data_inv, unidades, total, nao_lidas = [], {}, {}, None, {}, None, []
    participantes, avisos, inventarios, fora, mot_inv = {}, [], [], 0, ""
    for linha in conteudo.splitlines():
        if not linha.strip().startswith("|"):
            continue
        reg = linha.strip()[1:5]
        if reg == "0000":
            c = campos_sped(linha, 15)
            empresa.update(nome=c[5], cnpj=c[6], uf=c[8], ie=c[9], cod_mun=c[10], im=c[11],
                           perfil=c[13], ind_ativ=c[14])
        elif reg == "0005":
            c = campos_sped(linha, 10)
            empresa.update(fantasia=c[1], cep=c[2], end=c[3], num=c[4], compl=c[5], bairro=c[6], fone=c[7], email=c[9])
        elif reg == "0150":
            c = campos_sped(linha, 13)
            participantes[c[1]] = dict(nome=c[2], cod_pais=c[3], cnpj=c[4], cpf=c[5], ie=c[6], cod_mun=c[7],
                                       suframa=c[8], end=c[9], num=c[10], compl=c[11], bairro=c[12])
        elif reg == "0190":
            c = campos_sped(linha, 3)
            unidades[c[1]] = c[2]
        elif reg == "0200":
            c = campos_sped(linha, 13)
            cadastro[c[1]] = c
        elif reg == "H005":
            c = campos_sped(linha, 4)
            inventarios.append(f"{c[1][:2]}/{c[1][2:4]}/{c[1][4:]} motivo {c[3]}")
            if len(inventarios) == 1:
                data_inv, total, mot_inv = data_ddmmaaaa(c[1]), num(c[2]), c[3]
        elif reg == "H010" and len(inventarios) > 1:
            fora += 1  # item de outro inventário do mesmo arquivo
        elif reg == "H010" and len(campos_sped(linha, 0)) < 7:
            nao_lidas.append(texto(linha))
        elif reg == "H010":
            c = campos_sped(linha, 11)
            it = Item(codigo=c[1], unidade=c[2], qtd=num(c[3]), vl_unit=num(c[4]), vl_item=num(c[5]),
                      ind_prop=c[6] or "0", cod_part=c[7], txt_compl=c[8], cod_cta=c[9], origem="H010")
            vl_ir = num(c[10])
            # VL_ITEM_IR menor que o VL_ITEM: a diferença é o ICMS recuperável (volta igual no arquivo novo)
            if vl_ir is not None and it.vl_item is not None and 0 <= vl_ir < it.vl_item:
                it.icms_rec = it.vl_item - vl_ir
            itens.append(it)
    if len(inventarios) > 1:
        avisos.append(f"o arquivo tem {len(inventarios)} inventários (H005: {', '.join(inventarios)}); usado só o "
                      f"primeiro, e os {fora} itens dos outros ficaram fora")
    if mot_inv and mot_inv != "01":
        avisos.append(f"o inventário do arquivo é do motivo {mot_inv}; o arquivo novo sai com o motivo 01 (final do período)")
    for it in itens:
        c = cadastro.get(it.codigo)
        if c:
            it.descricao, it.cod_barra, it.tipo, it.ncm, it.cest = c[2], c[3], c[6], c[7], c[12]
    no_h010 = {it.codigo for it in itens}
    so_cadastro = [Item(codigo=c[1], descricao=c[2], unidade=c[5], tipo=c[6], ncm=c[7], cest=c[12], cod_barra=c[3])
                   for c in cadastro.values() if c[1] not in no_h010]
    if not itens:
        itens, so_cadastro = so_cadastro, []
    return Fonte(caminho, "SPED / bloco H", itens, empresa, data_inv, total, unidades, nao_lidas,
                 avisos=avisos, participantes=participantes, cadastro=so_cadastro)


# Cabeçalhos de planilha reconhecidos (sem acento, maiúsculo, por "contém")
COLUNAS = [
    ("codigo", ["COD. UTILIZADO", "CODIGO UTILIZADO", "COD UTILIZADO"]),
    ("codigo_alt", ["COD. PRODUTO", "COD PRODUTO", "CODIGO DO PRODUTO"]),
    ("cod_part", ["COD_PART", "COD. PART", "COD PART", "PARTICIPANTE", "CNPJ DO TERCEIRO", "CNPJ TERCEIRO",
                  "CPF/CNPJ DO TERCEIRO", "CNPJ/CPF DO TERCEIRO"]),
    ("ind_prop", ["IND_PROP", "IND. PROP", "IND PROP", "PROPRIEDADE", "POSSE"]),
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
    "codigo": ("BARRA", "BARCODE", "EAN", "GTIN", "NCM", "CEST", "CONTA", "PART"),
    "codigo_alt": ("BARRA", "EAN", "GTIN"),
    "descricao": ("COD",),
    "unidade": ("VALOR", "VL", "CUSTO", "QTD", "QUANT", "COST", "PRICE", "PRECO", "VALUE"),
    "qtd": ("FORA", "TERCEIRO", "VALOR", "VL", "R$", "CUSTO", "MINIMO", "MAXIMO"),
    "vl_unit": ("TOTAL",),
    "vl_item": ("UNIT", "QTD", "QUANT", "TOTAL ICMS", "ICMS TOTAL", "VALOR ICMS", "VL ICMS", "VL. ICMS"),
    # "Alíquota ICMS" (18) e "Base ICMS" não são o ICMS recuperável incluído no custo
    "icms_rec": ("ALIQ", "%", "BASE", "BC ", "B.C", " ST ", "DIFAL"),
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
    """Abas da planilha como (nome, linhas, separador do CSV ou None)."""
    ext = caminho.suffix.lower()
    if ext == ".csv":
        txt = ler_texto(caminho)
        # StringIO e não splitlines(): um campo entre aspas pode ter quebra de linha dentro
        ler = lambda s, n=None: [list(r) for r in itertools.islice(csv.reader(io.StringIO(txt, newline=""), delimiter=s), n)]
        # O separador é o que monta o cabeçalho com mais colunas conhecidas (contar vírgulas
        # engana: num CSV com ";" as vírgulas decimais podem ser mais que os separadores)
        def nota(s):
            m = cabecalho(ler(s, 30))[1]
            return (len(m) if m else 0, txt.count(s))
        sep = max((";", ",", "\t"), key=nota)
        yield caminho.stem, ler(sep), sep
        return
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                yield ws.title, [["" if v is None else v for v in r] for r in ws.iter_rows(values_only=True)], None
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
        yield sh.name, linhas, None


RE_TOTAL = re.compile(r"(SUB-?\s*)?TOTA(L|IS)\b|SOMA\b|TRANSPORT|A TRANSPORTAR")


def cabecalho(linhas):
    """Linha do cabeçalho: a primeira, entre as 30 de cima, com um código e mais 2 campos conhecidos
    (ou código e descrição)."""
    for i, l in enumerate(linhas[:30]):
        m = mapear_colunas(l)
        tem_codigo = "codigo" in m or "codigo_alt" in m
        # 3 campos conhecidos, ou só código e descrição (planilha de cadastro para completar o relatório)
        if tem_codigo and (len(m) >= 3 or "descricao" in m):
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


def participante(valor):
    """COD_PART escrito na planilha: CNPJ ou CPF sem máscara, ou o código como veio."""
    t = cod_texto(valor)
    if cnpj_valido(t):
        return so_cnpj(t)
    if cpf_valido(t):
        return so_digitos(t)
    return texto(t, 60)


def ler_planilha(caminho):
    abas = linhas_planilha(caminho)
    candidatas = []
    try:
        for nome, linhas, sep in abas:
            ini, mapa = cabecalho(linhas)
            if ini is not None:
                inventario = "qtd" in mapa and ("vl_unit" in mapa or "vl_item" in mapa)
                candidatas.append((inventario, nome, linhas, sep, ini, mapa))
    finally:
        abas.close()  # fecha o arquivo já: no Windows, aberto ele fica preso
    if not candidatas:
        raise ValueError("não achei a linha de cabeçalho (Código, Descrição, Quantidade...)")
    # a primeira aba com quantidade e valor; sem nenhuma, a primeira com cabeçalho (cadastro)
    _, nome_aba, linhas, sep, ini, mapa = next((c for c in candidatas if c[0]), candidatas[0])
    # A aba "Exemplo" da Planilha Modelo (e de modelos parecidos) não é inventário do cliente
    avisos = [f"a aba \"{c[1]}\" também tem um inventário e não foi lida (usada a aba \"{nome_aba}\"): "
              "se ela faz parte do mesmo inventário, junte as abas numa só"
              for c in candidatas if c[0] and c[1] != nome_aba and "EXEMPLO" not in sem_acento(c[1])]
    # o total do topo serve para conferir; uma linha de total no fim da tabela vale mais
    empresa, data_inv, total_declarado = titulo_da_planilha(linhas[:ini])
    pega = lambda l, k: l[mapa[k]] if k in mapa and mapa[k] < len(l) else ""
    # Números escritos como texto (CSV, texto no Excel): o separador decimal sai do arquivo
    # todo, não de cada célula; sem pista, vale o costume do separador do CSV
    numericas = [mapa[k] for k in ("qtd", "qtd_terc", "vl_unit", "vl_item", "icms_rec") if k in mapa]
    estilo, aviso = estilo_numeros((l[c] for l in linhas[ini + 1:] for c in numericas if c < len(l) and isinstance(l[c], str)),
                                   "en" if sep == "," else "br")
    if aviso:
        avisos.append(aviso)
    n = lambda l, k: num(pega(l, k), br=estilo == "br", en=estilo == "en")
    itens = []
    for nlinha, l in enumerate(linhas[ini + 1:], start=ini + 2):
        cod = bruto(pega(l, "codigo")) or bruto(pega(l, "codigo_alt"))
        descricao = bruto(pega(l, "descricao"))
        if not cod:
            # "Total:" numa coluna qualquer de uma linha sem código
            if any(isinstance(v, str) and RE_TOTAL.match(sem_acento(v).strip()) for v in l) and n(l, "vl_item") is not None:
                total_declarado = n(l, "vl_item")
                continue
            if all(n(l, k) is None for k in ("qtd", "qtd_terc", "vl_unit", "vl_item")):
                continue  # linha em branco ou título de grupo ("BEBIDAS")
            if not descricao:
                avisos.append(f"linha {nlinha} sem código nem descrição, mas com números, ficou fora (parece um total "
                              "ou subtotal): confira")
                continue
            # item sem código: segue com código vazio, e o complementar gera um SEMCOD com pendência
        # Linha de total, subtotal ou transporte de página: não é item. O valor dela serve
        # para conferir o total gerado (como o "Total:" do relatório).
        elif RE_TOTAL.match(sem_acento(cod)) and not descricao:
            if n(l, "vl_item") is not None:
                total_declarado = n(l, "vl_item")
            continue
        tipo = so_digitos(cod_texto(pega(l, "tipo")))
        it = Item(
            codigo=cod, descricao=descricao, unidade=texto(pega(l, "unidade")).upper(),
            # cod_texto antes de so_digitos: o .xls guarda o NCM 94036000 como 94036000.0
            ncm=so_digitos(cod_texto(pega(l, "ncm"))), cest=so_digitos(cod_texto(pega(l, "cest"))),
            cod_cta=cod_texto(pega(l, "cod_cta")), cod_part=participante(pega(l, "cod_part")),
            qtd=n(l, "qtd"), vl_unit=n(l, "vl_unit"), vl_item=n(l, "vl_item"),
            icms_rec=n(l, "icms_rec"), tipo=tipo.zfill(2) if tipo else "",
            origem=f"linha {nlinha}",
        )
        prop = cod_texto(pega(l, "ind_prop"))
        if so_digitos(prop) in ("0", "1", "2") and len(so_digitos(prop)) == 1:
            it.ind_prop = so_digitos(prop)
        elif prop:
            it.pendencias.append(f"indicador de propriedade \"{prop}\" não é 0, 1 ou 2: considerado 0 (do próprio informante)")
        alt = bruto(pega(l, "codigo_alt"))
        if alt and alt != cod:
            it.aliases.add(alt)
        terc = n(l, "qtd_terc")
        if terc:
            # A quantidade em poder de terceiros é um H010 à parte (IND_PROP 1), com o mesmo custo
            t = dataclasses.replace(it, qtd=terc, vl_item=None, icms_rec=None, ind_prop="1", aliases=set(it.aliases),
                                    pendencias=[], origem=f"linha {nlinha} (em terceiros)")
            if not it.qtd:  # tudo em terceiros: o valor da linha é dele
                t.vl_item, t.icms_rec = it.vl_item, it.icms_rec
            elif t.vl_unit is None and it.vl_item is not None:
                t.vl_unit = it.vl_item / it.qtd
                t.pendencias.append("valor em terceiros calculado com o custo unitário da quantidade no estabelecimento: confira")
            if it.qtd:
                itens.append(it)
            itens.append(t)
            continue
        itens.append(it)
    # Planilha com quantidade e valor zerados em tudo é só cadastro
    if all(not i.qtd and not i.vl_unit and not i.vl_item for i in itens):
        for i in itens:
            i.qtd = i.vl_unit = i.vl_item = None
    return Fonte(caminho, "Planilha", itens, empresa, data_inv, total_declarado, avisos=avisos)


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
        "email": (d.get("email") or "").lower(), "cnae": str(d.get("cnae_fiscal") or ""),
    }


def erro_consulta(cnpj, e):
    """Motivo da consulta do CNPJ que falhou, em português."""
    if isinstance(e, urllib.error.HTTPError) and e.code == 404:
        return f"CNPJ {cnpj} não encontrado na Receita: confira o número"
    if isinstance(e, urllib.error.URLError):
        return "sem acesso à internet para consultar o CNPJ"
    return f"consulta do CNPJ falhou ({e})"


# Palavras que não identificam a empresa: ficam fora da comparação do nome do arquivo com o da Receita
PALAVRAS_COMUNS = {"LTDA", "EIRELI", "EPP", "MATRIZ", "FILIAL", "COMERCIO", "COMERCIAL", "IND", "INDUSTRIA", "INDUSTRIAL",
                   "DAS", "DOS", "CIA", "SERVICOS", "LOJA", "EMPRESA", "BRASIL"}


def nomes_parecidos(a, b):
    """True se os dois nomes têm alguma palavra em comum (fora as genéricas, como LTDA e FILIAL)."""
    palavras = lambda t: {w for w in re.findall(r"[A-Z0-9]+", sem_acento(t)) if len(w) >= 3 and w not in PALAVRAS_COMUNS}
    return bool(palavras(a) & palavras(b))


def cnae_industrial(cnae):
    """CNAE da indústria de transformação ou extrativa (divisões 05 a 33)."""
    d = so_digitos(cnae).zfill(7) if so_digitos(cnae) else ""
    return bool(d) and 5 <= int(d[:2]) <= 33


def carregar_json(caminho):
    if not caminho.exists():
        return {}
    try:  # utf-8-sig: aceita o arquivo salvo pelo Bloco de Notas
        return json.loads(caminho.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ErroConversao(f"{caminho.name} está com erro de digitação (linha {e.lineno}); corrija ou apague o arquivo.")


def salvar_cadastro(cadastro):
    DADOS.mkdir(parents=True, exist_ok=True)
    CADASTRO_EMPRESAS.write_text(json.dumps(cadastro, ensure_ascii=False, indent=2), encoding="utf-8")


# Endereço do registro 0005 e as outras opções que a linha de comando e o painel podem informar
CAMPOS_LINHA = ("cnpj", "ie", "uf", "cod_mun", "nome", "fantasia", "perfil", "ind_ativ", "im",
                "cep", "end", "num", "compl", "bairro", "fone", "email")


def dados_empresa(fontes, args, pendencias):
    """Prioridade: linha de comando > arquivo > empresas.json/consulta do CNPJ.
    O nome empresarial prefere a razão social do cadastro ao nome impresso no relatório."""
    do_arquivo = {}
    for f in reversed(fontes):
        do_arquivo.update({k: v for k, v in f.empresa.items() if v})
    da_linha = {k: getattr(args, k) for k in CAMPOS_LINHA if getattr(args, k, None)}
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
    # Consulta só o que falta: com UF, município e CEP no arquivo ou no formulário, não precisa
    faltando = not all(salvo.get(k) or emp.get(k) for k in ("uf", "cod_mun", "cep"))
    if cnpj and faltando and not args.sem_consulta:
        try:
            salvo = {**consultar_cnpj(cnpj), **{k: v for k, v in salvo.items() if v}}
            cadastro[cnpj] = salvo
            salvar_cadastro(cadastro)
        except Exception as e:
            pendencias.append(("empresa", cnpj, f"{erro_consulta(cnpj, e)}: preencha UF, município e endereço"))
    # Nome do arquivo bem diferente do nome na Receita: o CNPJ pode estar errado (é de outra empresa)
    nome_arq, nome_rec = do_arquivo.get("nome") or da_linha.get("nome"), salvo.get("nome")
    if nome_arq and nome_rec and not (nomes_parecidos(nome_arq, nome_rec) or nomes_parecidos(nome_arq, salvo.get("fantasia", ""))):
        pendencias.append(("empresa", cnpj, f"o nome no arquivo (\"{texto(nome_arq)}\") não bate com o do CNPJ {cnpj} na Receita "
                           f"(\"{texto(nome_rec)}\"): confira se o CNPJ está certo; o arquivo saiu com o nome da Receita"))
    for k, v in salvo.items():
        if v and (not emp.get(k) or (k == "nome" and "nome" not in da_linha)):
            emp[k] = v
    if not emp.get("fantasia"):
        emp["fantasia"] = do_arquivo.get("nome") or emp.get("nome", "")
    emp.setdefault("perfil", args.perfil_padrao)
    # Atividade não informada: o CNAE do cadastro do CNPJ diz se é indústria
    if not emp.get("ind_ativ") and cnae_industrial(emp.get("cnae")):
        emp["ind_ativ"] = "0"
        pendencias.append(("empresa", cnpj, f"CNAE {emp['cnae']} é de indústria: atividade marcada como industrial (registro 0002); confira"))
    emp.setdefault("ind_ativ", args.ind_ativ_padrao)
    # Campos obrigatórios do 0000 e do 0005 (CEP, endereço e bairro): sem eles o PVA recusa
    for k, msg in (("uf", "UF"), ("cod_mun", "código IBGE do município"), ("ie", "inscrição estadual"),
                   ("nome", "nome empresarial"), ("cep", "CEP (registro 0005)"), ("end", "endereço (registro 0005)"),
                   ("bairro", "bairro (registro 0005)")):
        if not texto(emp.get(k)):
            pendencias.append(("empresa", cnpj, f"falta {msg}"))
    uf, mun, cep = texto(emp.get("uf")).upper(), so_digitos(emp.get("cod_mun")), so_digitos(emp.get("cep"))
    if uf and uf not in UF_IBGE:
        pendencias.append(("empresa", cnpj, f"UF \"{uf}\" não existe"))
    if mun and (len(mun) != 7 or (uf in UF_IBGE and mun[:2] != UF_IBGE[uf])):
        pendencias.append(("empresa", cnpj, f"código do município {mun} não é um código IBGE de 7 dígitos da UF {uf or '?'}"))
    if cep and len(cep) != 8:
        pendencias.append(("empresa", cnpj, f"CEP {cep} não tem 8 dígitos"))
    return emp


def participantes_usados(itens, fontes, args, pendencias):
    """Dados do registro 0150 de cada participante (COD_PART) dos itens em poder de terceiros:
    do 0150 de um SPED de entrada, do cadastro salvo ou da consulta do CNPJ."""
    dados = {}
    for f in reversed(fontes):
        dados.update({k: v for k, v in f.participantes.items() if k})
    cadastro = None
    for cod in sorted({it.cod_part for it in itens if it.cod_part}):
        if dados.get(cod, {}).get("nome") or not cnpj_valido(cod):
            continue
        if cadastro is None:
            cadastro = carregar_json(CADASTRO_EMPRESAS)
        salvo = cadastro.get(cod, {})
        if not (salvo.get("nome") and salvo.get("cod_mun")) and not args.sem_consulta:
            try:
                salvo = {**consultar_cnpj(cod), **{k: v for k, v in salvo.items() if v}}
                cadastro[cod] = salvo
                salvar_cadastro(cadastro)
            except Exception as e:
                pendencias.append(("participante", cod, f"participante: {erro_consulta(cod, e)}"))
        if salvo.get("nome"):
            dados[cod] = {"nome": salvo["nome"], "cod_pais": "1058", "cnpj": cod, "cpf": "", "ie": salvo.get("ie", ""),
                          "cod_mun": salvo.get("cod_mun", ""), "suframa": "", "end": salvo.get("end", ""),
                          "num": salvo.get("num", ""), "compl": salvo.get("compl", ""), "bairro": salvo.get("bairro", "")}
    # O 0150 só serve com nome e, no Brasil (país 1058), com o município IBGE
    brasil = lambda v: so_digitos(v.get("cod_pais")).lstrip("0") in ("", "1058")
    return {k: v for k, v in dados.items()
            if texto(v.get("nome")) and (len(so_digitos(v.get("cod_mun"))) == 7 or not brasil(v))}


def ncm_valido(ncm):
    """NCM com 8 dígitos (ou None). Com 7, o Excel guardou como número e comeu o zero
    da frente (capítulos 01 a 09): volta o zero."""
    d = so_digitos(ncm)
    if len(d) == 7:
        d = d.zfill(8)
    return d if len(d) == 8 and d != "00000000" else None


def total_confere(qtd, vl_unit, vl_item):
    """Total igual a quantidade × unitário, com diferença de centavo. Unitário com 2 ou mais
    casas pode ter sido arredondado na impressão (2,47 para 2,4666...): aí vale meia casa de
    diferença. Unitário inteiro ou com 1 casa é o valor digitado, sem arredondamento."""
    if abs(qtd * vl_unit - vl_item) <= DOIS or not qtd:
        return True
    casas = -vl_unit.normalize().as_tuple().exponent if vl_unit else 0
    if casas < 2:
        return False
    return abs(vl_item / qtd - vl_unit) <= Decimal(5).scaleb(-casas - 1)


def complementar(principal, extras, pendencias, codigos_gerados, participantes=None, so_cadastro=()):
    """Itens que entram no arquivo (e os que ficaram fora). As pendências de cada item ficam
    nele (it.pendencias) e vão para a lista geral no fim, já com o código final do item.
    `so_cadastro`: arquivos de outra data ou de outro estabelecimento, que só completam
    descrição, NCM, unidade e tipo (quantidade e valor deles são de outro inventário)."""
    participantes = participantes or {}
    so_cadastro = set(so_cadastro)
    indice, arquivo_de = {}, {}
    for f in extras:
        for it in [*f.itens, *f.cadastro]:
            arquivo_de[id(it)] = f
            for c in {it.codigo, *it.aliases}:
                if c:
                    indice.setdefault(c.upper(), it)
    arredondadas, total_errado = [], []

    finais, fora, usados = [], [], set()
    por_codigo, cadastro = {}, {}  # cadastro: código -> item que define o 0200
    for it in principal.itens:
        p = it.pendencias.append
        ref = (indice.get(it.codigo.upper()) if it.codigo else None) or next(
            (indice[a.upper()] for a in it.aliases if a and a.upper() in indice), None)
        if ref:
            usados.add(id(ref))
            mesmo_inventario = arquivo_de[id(ref)].caminho not in so_cadastro
            # O complemento não muda a quantidade do inventário, mas a divergência é avisada:
            # é o que o contador quer conferir (ex.: inventário x estoque escriturado)
            if mesmo_inventario and it.qtd is not None and ref.qtd is not None and it.qtd != ref.qtd:
                p(f"quantidade {fmt(it.qtd, 3)} difere da de {arquivo_de[id(ref)].caminho.name} ({fmt(ref.qtd, 3)}): mantida a do inventário")
            for campo in ("descricao", "ncm", "unidade", "tipo", "cod_cta", "cest", "cod_barra"):
                if not getattr(it, campo) and getattr(ref, campo):
                    setattr(it, campo, getattr(ref, campo))
            # Descrição cortada no relatório ("GAIVOTA COURO TRABALHA"): vale a completa do cadastro
            completa, cortada = texto(ref.descricao).upper(), texto(it.descricao).upper()
            if cortada and len(completa) > len(cortada) and completa.startswith(cortada):
                it.descricao = ref.descricao
            # NCM impresso como "00" ou faltando dígitos no inventário: vale o do cadastro
            if not ncm_valido(it.ncm) and ncm_valido(ref.ncm):
                it.ncm = ref.ncm
            if mesmo_inventario and it.vl_unit is None and it.vl_item is None and ref.vl_unit is not None:
                it.vl_unit = ref.vl_unit
                p(f"valor unitário veio de {ref.origem or 'outro arquivo'}: {fmt(ref.vl_unit, 6)}")

        if not it.codigo:
            codigos_gerados[0] += 1
            it.codigo = f"SEMCOD{codigos_gerados[0]:04d}"
            p(f"linha sem código no arquivo, código gerado: \"{it.origem}\"")
        # Tamanhos do leiaute antes de comparar códigos: dois códigos longos que só
        # diferem depois do 60º caractere viram o mesmo código no arquivo
        original = it.codigo
        it.codigo = texto(original, 60)
        if len(texto(original)) > 60:
            p(f"código com mais de 60 caracteres cortado (era \"{texto(original)}\")")
        elif it.codigo != " ".join(str(original).split()):
            p(f"código \"{original}\" tem caracteres que o SPED não aceita: usado \"{it.codigo}\"")
        if perde_caractere(it.descricao):
            p("descrição tem caracteres sem equivalente no SPED (ISO-8859-1): trocados por \"?\"")
        it.descricao = texto(it.descricao)
        unidade = texto(it.unidade)
        it.unidade = POR_EXTENSO.get(sem_acento(unidade), texto(unidade, 6).upper()) or "UN"
        if len(unidade) > 6 and sem_acento(unidade) not in POR_EXTENSO:
            p(f"unidade \"{unidade}\" tem mais de 6 caracteres: cortada para \"{it.unidade}\"")
        if it.ind_prop not in ("0", "1", "2"):
            p(f"indicador de propriedade \"{it.ind_prop}\" não é 0, 1 ou 2: considerado 0 (do próprio informante)")
            it.ind_prop = "0"
        if it.ind_prop == "0":
            it.cod_part = ""  # o COD_PART do H010 é só para item em terceiros ou de terceiros

        motivo = None
        if it.qtd is None:
            motivo = "sem quantidade: item fora do arquivo"
        elif it.vl_item is None and it.vl_unit is None:
            motivo = "sem valor unitário: item fora do arquivo"
        elif it.qtd < 0 or (it.vl_item or 0) < 0 or (it.vl_unit or 0) < 0:
            motivo = "quantidade ou valor negativo: item fora do arquivo"
        elif it.qtd == 0:
            motivo = "quantidade zero: item fora do arquivo"
        elif it.ind_prop != "0" and not it.cod_part:
            motivo = ("item em poder de terceiros (ou de terceiros em poder do informante) sem o participante: fora do "
                      "arquivo, porque o H010 exige o COD_PART. Informe o CNPJ do terceiro na coluna Participante")
        elif it.ind_prop != "0" and it.cod_part not in participantes:
            motivo = (f"participante {it.cod_part} sem nome e município para o registro 0150: item fora do arquivo. "
                      "Use um CNPJ válido (o app consulta os dados) ou junte o SPED que tem o 0150 dele")
        if motivo:
            p(motivo)
            fora.append(it)
            continue
        if it.qtd != it.qtd.quantize(TRES):
            arredondadas.append(f"{it.codigo} {str(it.qtd).replace('.', ',')} -> {fmt(it.qtd, 3)}")
        # Total que não é quantidade × unitário (fórmula errada na planilha, coluna trocada):
        # vale quantidade × unitário, e o item vai para as pendências
        if it.vl_item is not None and it.vl_unit is not None and not total_confere(it.qtd, it.vl_unit, it.vl_item):
            calculado = (it.qtd * it.vl_unit).quantize(DOIS, ROUND_HALF_UP)
            p(f"total do arquivo {fmt(it.vl_item, 2)} difere de quantidade × unitário ({fmt(it.qtd, 3)} × "
              f"{str(it.vl_unit).replace('.', ',')} = {fmt(calculado, 2)}): usado {fmt(calculado, 2)}")
            total_errado.append(it.codigo)
            it.vl_item = calculado
        if it.vl_item is None:
            it.vl_item = (it.qtd * it.vl_unit).quantize(DOIS, ROUND_HALF_UP)
        if it.vl_unit is None:
            it.vl_unit = it.vl_item / it.qtd
        if it.icms_rec is not None and (it.icms_rec < 0 or it.icms_rec > it.vl_item):
            p(f"ICMS recuperável {fmt(it.icms_rec, 2)} negativo ou maior que o valor do item: ignorado (VL_ITEM_IR = VL_ITEM)")
            it.icms_rec = None

        # O mesmo item pode estar em poder do informante e de terceiros (um H010 para cada
        # IND_PROP), mas o cadastro 0200 é um só por código: descrição e unidade têm de bater.
        cad = cadastro.get(it.codigo.upper())
        if cad and not (texto(cad.descricao).upper() == texto(it.descricao).upper() and cad.unidade == it.unidade):
            n = 2
            while f"{it.codigo[:56]}-{n}".upper() in cadastro:
                n += 1
            p(f"código {it.codigo} repetido com outra descrição ou unidade (\"{it.descricao}\"): renomeado")
            it.codigo = f"{it.codigo[:56]}-{n}"
        elif cad:
            it.codigo = cad.codigo  # "abc" e "ABC": o H010 usa o código exato do 0200
        chave = (it.codigo.upper(), it.ind_prop, it.cod_part)
        igual = por_codigo.get(chave)
        if igual:
            igual.qtd += it.qtd
            igual.vl_item += it.vl_item
            igual.vl_unit = igual.vl_item / igual.qtd
            if it.icms_rec is not None or igual.icms_rec is not None:
                igual.icms_rec = (igual.icms_rec or 0) + (it.icms_rec or 0)
            igual.pendencias.extend(it.pendencias)
            it.pendencias = []
            igual.pendencias.append("código repetido com a mesma descrição: quantidades somadas")
            continue
        cadastro.setdefault(it.codigo.upper(), it)
        por_codigo[chave] = it
        finais.append(it)

    # Nenhum item com descrição: o arquivo não tem cadastro (ex.: bloco H sem 0200). Um aviso só,
    # para o filtro "Com pendência" continuar mostrando o que é de cada item
    sem_descricao = [it for it in finais if not it.descricao]
    if finais and len(sem_descricao) == len(finais):
        pendencias.append(("arquivo", principal.caminho.name, f"nenhum dos {len(finais)} itens tem descrição no arquivo: usado "
                           "\"ITEM <código>\". Junte o cadastro do cliente (SPED com 0200, .FS ou planilha com código e descrição)"))
    for it in finais:
        p = it.pendencias.append
        if not it.descricao:
            it.descricao = f"ITEM {it.codigo}"
            if len(sem_descricao) < len(finais):
                p("sem descrição em nenhum arquivo: usado o próprio código")
        if ncm_valido(it.ncm):
            it.ncm = ncm_valido(it.ncm)
        elif it.ncm:
            p(f"NCM \"{it.ncm}\" não tem 8 dígitos: campo deixado em branco")
            it.ncm = ""
        if it.tipo:
            tipo = it.tipo.zfill(2) if it.tipo.isdigit() else it.tipo
            if tipo in TIPOS_ITEM:
                it.tipo = tipo
            else:
                p(f"tipo do item \"{it.tipo}\" não existe na tabela do SPED (00 a 10 ou 99): usado o tipo padrão")
                it.tipo = ""
        if it.cest and len(so_digitos(it.cest)) != 7:
            p(f"CEST \"{it.cest}\" não tem 7 dígitos: campo deixado em branco")
            it.cest = ""

    # Itens com quantidade nos arquivos de complemento que não estão no inventário: não entram,
    # mas o contador precisa saber (ex.: estoque escriturado com item que o inventário não tem)
    for f in extras:
        if f.caminho in so_cadastro:
            continue
        sobra = [i.codigo for i in f.itens if i.qtd and id(i) not in usados]
        if sobra:
            exemplos = ", ".join(sobra[:5]) + ("..." if len(sobra) > 5 else "")
            pendencias.append(("arquivo", f.caminho.name, f"{len(sobra)} item(ns) com quantidade neste arquivo não estão no "
                               f"inventário ({principal.caminho.name}) e não entraram no SPED: {exemplos}"))
    if arredondadas:
        pendencias.append(("arquivo", principal.caminho.name, f"{len(arredondadas)} quantidade(s) com mais de 3 casas decimais "
                           f"arredondadas para 3, como o SPED aceita, sem mudar o valor do item (ex.: {arredondadas[0]})"))
    if len(total_errado) > 1:
        exemplos = ", ".join(total_errado[:5]) + ("..." if len(total_errado) > 5 else "")
        pendencias.append(("arquivo", principal.caminho.name, f"{len(total_errado)} de {len(principal.itens)} itens com total "
                           f"diferente de quantidade × unitário ({exemplos}): usado quantidade × unitário; confira a "
                           "fórmula ou a coluna de total do arquivo"))
    for it in principal.itens:
        it.pendencias = list(dict.fromkeys(it.pendencias))  # a mesma mensagem uma vez só (linhas somadas)
        for msg in it.pendencias:
            pendencias.append(("item", it.codigo, msg))
    return finais, fora


# Unidades que não se dividem: quantidade com casas decimais nelas é erro de digitação ou de cadastro
UNIDADES_INTEIRAS = {"UN", "UND", "UNID", "PC", "PCA", "PCS", "PCT", "PCTE", "PT", "CX", "PR", "JG", "KIT", "DZ", "CJ",
                     "FD", "SC", "LA", "FR", "TB", "CT", "EMB", "BD", "GL", "BL", "BB"}


# Siglas que querem dizer a mesma unidade (PCT e PCTE são pacote)
SINONIMOS = [{"UN", "UND", "UNID"}, {"PC", "PCA"}, {"PCT", "PCTE", "PT"}, {"M", "MT"}, {"L", "LT"}, {"G", "GR"}, {"T", "TON"}]
# Tipos do item que só a indústria tem: matéria-prima, produto em processo, acabado, subproduto, intermediário
TIPOS_INDUSTRIA = {"01", "03", "04", "05", "06"}


def conferir_cadastro(itens, emp, args, pendencias, unidades_desc=None):
    """Avisos sobre o que o arquivo não traz e o app teve de supor: o tipo do item, o NCM, a
    quantidade fracionada numa unidade que não se divide e as unidades do 0190."""
    industria = emp.get("ind_ativ") == "0"
    usadas = {it.unidade for it in itens}
    desconhecidas = sorted(u for u in usadas if u not in UNIDADES and u not in (unidades_desc or {}))
    if desconhecidas:
        pendencias.append(("arquivo", "0190", f"unidade(s) {', '.join(desconhecidas)} fora da tabela conhecida: o 0190 sai com a "
                           "própria sigla como descrição; confira com o cliente"))
    for grupo in SINONIMOS:
        if len(iguais := sorted(usadas & grupo)) > 1:
            pendencias.append(("arquivo", "0190", f"as siglas {' e '.join(iguais)} são a mesma unidade ({UNIDADES[iguais[0]]}): "
                               "confira o cadastro do cliente (o app não junta, porque mudaria a unidade dos itens)"))
    de_industria = sum(1 for it in itens if it.tipo in TIPOS_INDUSTRIA)
    if de_industria and not industria:
        pendencias.append(("arquivo", "TIPO_ITEM", f"{de_industria} item(ns) com tipo de indústria no arquivo (01 matéria-prima, "
                           "04 produto acabado...), mas a Atividade está como Outros: se a empresa é indústria, marque "
                           "Atividade Industrial"))
    for it in itens:
        if it.qtd != it.qtd.to_integral_value() and it.unidade in UNIDADES_INTEIRAS:
            msg = f"quantidade fracionada ({fmt(it.qtd, 3)}) na unidade {it.unidade}, que não se divide: confira a quantidade ou a unidade"
            it.pendencias.append(msg)
            pendencias.append(("item", it.codigo, msg))
        if not it.ncm and industria:
            msg = "sem NCM: na indústria o NCM é obrigatório no 0200"
            it.pendencias.append(msg)
            pendencias.append(("item", it.codigo, msg))
    sem_ncm = [it.codigo for it in itens if not it.ncm]
    if sem_ncm and not industria:
        exemplos = ", ".join(sem_ncm[:5]) + ("..." if len(sem_ncm) > 5 else "")
        pendencias.append(("arquivo", "NCM", f"{len(sem_ncm)} de {len(itens)} itens sem NCM ({exemplos}): o NCM é obrigatório no "
                           "0200 para indústria, atacado e itens com substituição tributária; junte um cadastro com o NCM"))
    sem_tipo = sum(1 for it in itens if not it.tipo)
    # No comércio o tipo padrão (00, revenda) é o certo; na indústria, falta o tipo de verdade
    if sem_tipo and (industria or args.tipo_item.zfill(2) != "00"):
        padrao = args.tipo_item.zfill(2)
        alerta = (" A empresa é indústria: matéria-prima é 01 e produto acabado 04." if industria and padrao == "00" else
                  " Confira se é o tipo certo para esses itens.")
        pendencias.append(("arquivo", "TIPO_ITEM", f"{sem_tipo} de {len(itens)} itens sem o tipo no arquivo: usado o tipo padrão "
                           f"{padrao}.{alerta}"))


def versao_leiaute(args, fim, pendencias):
    """COD_VER do 0000 pelo ano do fim do período."""
    if args.cod_ver:
        return args.cod_ver
    if fim.year in VERSAO_LEIAUTE:
        return VERSAO_LEIAUTE[fim.year]
    if fim.year < min(VERSAO_LEIAUTE):
        raise ErroConversao(f"O app não conhece o código da versão do leiaute de {fim.year}: informe --cod-ver.", "periodo")
    ultimo = max(VERSAO_LEIAUTE)
    pendencias.append(("arquivo", "0000", f"o app não conhece o leiaute de {fim.year}: usado o de {ultimo} "
                       f"({VERSAO_LEIAUTE[ultimo]}); confira no Guia Prático se há versão nova"))
    return VERSAO_LEIAUTE[ultimo]


def montar_sped(emp, itens, unidades_desc, data_inv, ini, fim, args, escritorio, participantes=None, cod_ver=None):
    reg = []
    add = lambda *campos: reg.append("|" + "|".join(str(c) for c in campos) + "|")

    cod_ver = cod_ver or args.cod_ver or VERSAO_LEIAUTE.get(fim.year, max(VERSAO_LEIAUTE.values()))
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
    # 0150: 13 campos, um por participante dos itens em poder de terceiros
    for cod in sorted({it.cod_part for it in itens if it.cod_part}):
        pt = (participantes or {})[cod]
        add("0150", cod, texto(pt.get("nome"), 100), so_digitos(pt.get("cod_pais")) or "1058", so_cnpj(pt.get("cnpj")),
            so_digitos(pt.get("cpf")), texto(pt.get("ie"), 14), so_digitos(pt.get("cod_mun")), texto(pt.get("suframa"), 9),
            texto(pt.get("end"), 60), texto(pt.get("num"), 10), texto(pt.get("compl"), 60), texto(pt.get("bairro"), 60))
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
                it.ind_prop, it.cod_part, texto(it.txt_compl), texto(it.cod_cta), fmt(vl_ir, 2))
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


def segundo_mes_depois(data_inv):
    """(mês, ano) da EFD em que o inventário vai, pelo Guia Prático: o segundo mês depois da
    data do inventário (o de 31/12 vai na EFD de fevereiro do ano seguinte)."""
    mes, ano = data_inv.month + 2, data_inv.year
    return (mes - 12, ano + 1) if mes > 12 else (mes, ano)


def periodo(args, data_inv):
    if args.periodo:
        m = re.fullmatch(r"\s*(\d{1,2})\s*[/\-.]\s*(\d{4})\s*", args.periodo)
        if not m or not 1 <= int(m[1]) <= 12:
            raise ErroConversao(f"Período \"{args.periodo}\" inválido: use mês/ano, por exemplo 02/2026.", "periodo")
        mes, ano = int(m[1]), int(m[2])
    else:
        mes, ano = segundo_mes_depois(data_inv)
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
    fora: list = field(default_factory=list)  # itens do inventário que não entraram no arquivo

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
    # Os motivos 02 a 06 exigem H020/H030 (CST, base e ICMS de cada item), que nenhum arquivo de entrada traz
    if args.mot_inv != "01":
        raise ErroConversao(f"O motivo do inventário {args.mot_inv} exige o registro H020 ou H030 (CST, base de cálculo e "
                            "ICMS de cada item), que os arquivos de inventário não trazem: o app gera o inventário do "
                            "final do período (motivo 01).")
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

    # Complemento de outro estabelecimento (matriz x filial) ou de outra data (inventário do
    # ano anterior): serve só de cadastro. Quantidade e valor dele são de outro inventário.
    cnpj_base = so_cnpj(args.cnpj or principal.empresa.get("cnpj") or next(
        (f.empresa.get("cnpj") for f in extras if f.empresa.get("cnpj")), ""))
    so_cadastro, mesmo_estab = set(), [principal]
    for f in extras:
        outro = so_cnpj(f.empresa.get("cnpj"))
        if outro and cnpj_base and outro != cnpj_base:
            so_cadastro.add(f.caminho)
            pendencias.append(("arquivo", f.caminho.name, f"é de outro estabelecimento (CNPJ {outro}): usado só como cadastro "
                               "(descrição, NCM, unidade e tipo). Para gerar o SPED dele, converta-o sozinho ou clique na "
                               "estrela para usá-lo como inventário"))
            continue
        mesmo_estab.append(f)
        if f.data_inv and f.data_inv != data_inv:
            so_cadastro.add(f.caminho)
            pendencias.append(("arquivo", f.caminho.name, f"é de outra data ({f.data_inv:%d/%m/%Y}): usado só como cadastro "
                               "(descrição, NCM, unidade e tipo); quantidades e valores não comparados"))
    # Avisos da leitura de cada arquivo; os de um arquivo que só serve de cadastro não interessam
    for f in (principal, *extras):
        if f.caminho in so_cadastro:
            continue
        for linha in f.nao_lidas:
            pendencias.append(("arquivo", f.caminho.name, f"linha não lida (confira e corrija o arquivo): {linha[:120]}"))
        for aviso in f.avisos:
            pendencias.append(("arquivo", f.caminho.name, aviso))
    emp = dados_empresa(mesmo_estab, args, pendencias)
    terceiros = [it for it in principal.itens if it.ind_prop in ("1", "2")]
    participantes = participantes_usados(terceiros, [principal, *extras], args, pendencias) if terceiros else {}
    itens, fora = complementar(principal, extras, pendencias, gerados, participantes, so_cadastro)
    unidades = {}
    for f in [*extras, principal]:
        unidades.update(f.unidades)
    conferir_cadastro(itens, emp, args, pendencias, unidades)
    ini, fim = periodo(args, data_inv)
    if data_inv > fim:
        raise ErroConversao(f"A data do inventário ({data_inv:%d/%m/%Y}) é depois do fim do período ({fim:%d/%m/%Y}).", "periodo")
    if (data_inv.day, data_inv.month) != (31, 12):
        pendencias.append(("arquivo", "H005", f"inventário em {data_inv:%d/%m/%Y} com o motivo 01 (final do período): para a "
                           "maioria das empresas o final do período é 31/12. Confira se é o balanço da empresa; os motivos "
                           "02 a 05 (mudança de regime, encerramento...) exigem o H020, que o app não gera"))
    mes, ano = segundo_mes_depois(data_inv)
    if (ini.month, ini.year) != (mes, ano):
        pendencias.append(("periodo", "", f"pelo Guia Prático, o inventário de {data_inv:%d/%m/%Y} vai na EFD de "
                           f"{mes:02d}/{ano} (segundo mês depois do inventário); o arquivo saiu para {ini:%m/%Y}"))
    escritorio = carregar_json(ESCRITORIO)
    if not (escritorio.get("nome") and escritorio.get("cpf")):
        escritorio = {}
        pendencias.append(("arquivo", "0100", "dados do escritório não cadastrados: registro 0100 (contabilista) não gerado"))
    else:
        if not cpf_valido(escritorio.get("cpf")):
            pendencias.append(("arquivo", "0100", "CPF do contabilista com dígito verificador inválido"))
        if not texto(escritorio.get("crc")):
            pendencias.append(("arquivo", "0100", "falta o CRC do contabilista (registro 0100)"))
        if escritorio.get("cnpj") and not cnpj_valido(escritorio.get("cnpj")):
            pendencias.append(("arquivo", "0100", "CNPJ do escritório com dígito verificador inválido"))

    if not itens:
        # Sem isso sairia um SPED com o bloco H vazio, que parece certo e não tem inventário
        motivo = collections.Counter(m for t, _, m in pendencias if t == "item" and "fora do arquivo" in m).most_common(1)
        dica = (" O arquivo não tem valores: use-o junto com o inventário (relatório, .FS ou planilha com valor unitário)."
                if not any(f.tem_valores for f in [principal, *extras]) else "")
        raise ErroConversao(f"Nenhum item de {principal.caminho.name} pôde entrar no arquivo ({len(principal.itens)} lido(s))"
                            + (f" ({motivo[0][0]})" if motivo else "") + "." + dica)

    cod_ver = versao_leiaute(args, fim, pendencias)
    reg, total = montar_sped(emp, itens, unidades, data_inv, ini, fim, args, escritorio, participantes, cod_ver)

    if principal.total_declarado is not None:
        dif = total - principal.total_declarado
        if dif:
            pendencias.append(("total", "", f"total gerado {moeda(total)} é {moeda(abs(dif))} {'maior' if dif > 0 else 'menor'} "
                               f"que o total do arquivo do cliente ({moeda(principal.total_declarado)})"))
    return Resultado(principal, emp, itens, pendencias, reg, total, data_inv, ini, fim, fora)


def moeda(valor):
    """1234567.8 em '1.234.567,80'."""
    return f"{valor.quantize(DOIS, ROUND_HALF_UP):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


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


def celula_csv(valor):
    """Texto para o CSV de pendências: começando com = + - @ o Excel executaria como fórmula."""
    v = str(valor)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


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
            # Código como texto (="0509"): o Excel apaga o zero à esquerda de números
            # Gerais primeiro; depois as de cada item juntas, na ordem em que o item aparece
            grupos = {}
            for pend in res.pendencias:
                grupos.setdefault(pend[1] if pend[0] == "item" else None, []).append(pend)
            ordem = [*(p for p in res.pendencias if p[0] != "item"),
                     *(p for c, g in grupos.items() if c is not None for p in g)]
            w.writerows((t, '="' + c.replace('"', '""') + '"' if c[:1].isdigit() else celula_csv(c), celula_csv(m))
                        for t, c, m in ordem)
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
    ap.add_argument("--periodo", help="mês do arquivo (mm/aaaa); padrão: o segundo mês depois do inventário "
                                      "(31/12/2025 vai em 02/2026, como manda o Guia Prático)")
    ap.add_argument("--cnpj"), ap.add_argument("--ie"), ap.add_argument("--uf"), ap.add_argument("--im")
    ap.add_argument("--cod-mun", help="código IBGE do município (7 dígitos)")
    ap.add_argument("--nome", help="nome empresarial")
    ap.add_argument("--fantasia")
    ap.add_argument("--cep"), ap.add_argument("--end", help="endereço (logradouro)"), ap.add_argument("--num")
    ap.add_argument("--compl"), ap.add_argument("--bairro"), ap.add_argument("--fone"), ap.add_argument("--email")
    ap.add_argument("--perfil", choices=["A", "B", "C"])
    ap.add_argument("--perfil-padrao", default="A", help=argparse.SUPPRESS)
    ap.add_argument("--ind-ativ", choices=["0", "1"], help="0 industrial, 1 outros")
    ap.add_argument("--ind-ativ-padrao", default="1", help=argparse.SUPPRESS)
    ap.add_argument("--clas-estab-ind", default="00", help="registro 0002 (só se --ind-ativ 0)")
    ap.add_argument("--tipo-item", default="00", help="TIPO_ITEM quando o arquivo não informa (padrão 00 revenda)")
    ap.add_argument("--mot-inv", default="01", choices=["01", "02", "03", "04", "05", "06"],
                    help="motivo do inventário; só o 01 (final do período) é gerado: 02 a 06 exigem H020/H030")
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
