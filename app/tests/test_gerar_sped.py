"""Testes do conversor e do painel. Rodar na pasta app: python -m unittest discover -s tests"""

import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix="estoque-teste-")
os.environ["ESTOQUE_DADOS"] = str(Path(TMP) / "dados")
sys.path.insert(0, str(APP))

import gerar_sped as gs  # noqa: E402


def arquivo(nome, conteudo):
    p = Path(TMP) / nome
    p.write_text(conteudo, encoding="utf-8")
    return p


def gerar(*caminhos, **opcoes):
    """Converte e devolve (Resultado, linhas do SPED)."""
    fontes = [gs.ler_arquivo(Path(c)) for c in caminhos]
    principal = gs.escolher_principal(fontes)
    args = gs.opcoes(sem_consulta=True, **opcoes)
    res = gs.converter(principal, [f for f in fontes if f is not principal], args)
    return res, res.registros


def campos(linhas, reg):
    return [l.strip("|").split("|") for l in linhas if l.startswith(f"|{reg}|")]


RELATORIO = """\
                         {titulo}
Firma: LOJA TESTE LTDA
Insc. Est.: 06.123.456-7        CNPJ: 11.222.333/0001-81
Estoque existente em 31/12/2025
94036000 U3424 696 041/22M LEXUS CADEIRA PRETA     UN      10,000      150,0000      1.500,00
85287200 TV5500 TELEVISOR 55 POL                   PC       2,000    2.300,5000      4.601,00
         Total:  6.101,00
"""


class Planilha(unittest.TestCase):
    def test_custo_total_antes_do_unitario(self):
        # Antes: "Custo Total" virava valor unitário e o inventário saía 100x maior
        p = arquivo("custo.csv", "Codigo;Descricao;Unidade;NCM;Quantidade;Custo Total;Custo Unitario\n"
                                  "A1;CANETA;UN;96081000;100;250,00;2,50\nA2;LAPIS;UN;96091000;10;5,00;0,50\n")
        res, linhas = gerar(p, data_inv="31/12/2025")
        h010 = {c[1]: c for c in campos(linhas, "H010")}
        self.assertEqual(h010["A1"][4], "2,500000")
        self.assertEqual(h010["A1"][5], "250,00")
        self.assertEqual(campos(linhas, "H005")[0][2], "255,00")

    def test_codigo_de_barras_nao_e_codigo(self):
        p = arquivo("barras.csv", "Cod. Barras;Codigo;Descricao;Qtd;Valor Unit\n7891234567890;X9;ITEM;1;10,00\n")
        res, linhas = gerar(p, data_inv="31/12/2025")
        self.assertEqual(campos(linhas, "H010")[0][1], "X9")

    def test_milhar_em_csv_brasileiro(self):
        p = arquivo("milhar.csv", "Codigo;Descricao;Quantidade;Valor Unit\nB1;PARAFUSO;1.500;0,10\n")
        res, linhas = gerar(p, data_inv="31/12/2025")
        self.assertEqual(campos(linhas, "H010")[0][3], "1500,000")

    def test_ponto_decimal_continua_decimal(self):
        p = arquivo("ponto.csv", "Codigo,Descricao,Quantidade,Valor Unit\nC1,FIO,2.5,4.00\n")
        res, linhas = gerar(p, data_inv="31/12/2025")
        self.assertEqual(campos(linhas, "H010")[0][3], "2,500")

    def test_planilha_grande_e_rapida(self):
        linhas = ["Codigo;Descricao;Unidade;Quantidade;Valor Unit"] + [f"C{i};ITEM {i};UN;1;1,00" for i in range(20000)]
        p = arquivo("grande.csv", "\n".join(linhas))
        ini = time.perf_counter()
        res, _ = gerar(p, data_inv="31/12/2025")
        self.assertEqual(len(res.itens), 20000)
        self.assertLess(time.perf_counter() - ini, 5)  # antes: ~6 s só para ler

    def test_numero_da_linha_nas_pendencias(self):
        p = arquivo("linhas.csv", "Codigo;Descricao;Quantidade;Valor Unit\nD1;A;1;1,00\nD1;A;1;1,00\n")
        fonte = gs.ler_arquivo(p)
        self.assertEqual([i.origem for i in fonte.itens], ["linha 2", "linha 3"])


class Itens(unittest.TestCase):
    def test_um_0200_para_proprio_e_terceiros(self):
        p = arquivo("terc.txt", "|0000|019|0|01122025|31122025|LOJA X|11222333000181||CE|061234567|2304400||||A|1|\n"
                                "|H005|31122025|300,00|01|\n"
                                "|H010|X1|UN|1,000|100,000000|100,00|0||||100,00|\n"
                                "|H010|X1|UN|2,000|100,000000|200,00|1||||200,00|\n")
        res, linhas = gerar(p)
        self.assertEqual(len(campos(linhas, "0200")), 1)
        self.assertEqual(len(campos(linhas, "H010")), 2)

    def test_unitario_negativo_fica_fora(self):
        p = arquivo("neg.csv", "Codigo;Descricao;Quantidade;Valor Unit\nN1;X;3;-2,00\nN2;Y;1;5,00\n")
        res, linhas = gerar(p, data_inv="31/12/2025")
        self.assertEqual([c[1] for c in campos(linhas, "H010")], ["N2"])
        self.assertTrue(any(c == "N1" and "negativo" in m for _, c, m in res.pendencias))

    def test_codigos_longos_iguais_depois_de_cortar(self):
        base = "Z" * 60
        p = arquivo("longo.csv", f"Codigo;Descricao;Quantidade;Valor Unit\n{base}A;UM;1;1,00\n{base}B;DOIS;1;1,00\n")
        res, linhas = gerar(p, data_inv="31/12/2025")
        cods = [c[1] for c in campos(linhas, "0200")]
        self.assertEqual(len(cods), len(set(cods)))
        self.assertTrue(all(len(c) <= 60 for c in cods))


class Relatorio(unittest.TestCase):
    def test_titulo_em_maiusculas(self):
        p = arquivo("rel_maiusc.txt", RELATORIO.format(titulo="REGISTRO DE INVENTÁRIO"))
        fonte = gs.ler_arquivo(p)
        self.assertEqual(fonte.tipo, "Relatório de inventário")
        self.assertEqual(fonte.empresa, {"nome": "LOJA TESTE LTDA", "ie": "061234567", "cnpj": "11222333000181"})
        self.assertEqual(fonte.total_declarado, Decimal("6101.00"))

    def test_sem_titulo_reconhece_pelas_linhas(self):
        p = arquivo("rel_sem.txt", RELATORIO.format(titulo="ESTOQUE"))
        self.assertEqual(gs.ler_arquivo(p).tipo, "Relatório de inventário")

    def test_estrutura_do_arquivo(self):
        p = arquivo("rel.txt", RELATORIO.format(titulo="Registro de Inventário"))
        res, linhas = gerar(p, uf="CE", cod_mun="2304400")
        self.assertEqual(res.total, Decimal("6101.00"))
        # totalizadores: 0990, H990, 9900 por registro, 9990 e 9999
        self.assertEqual(int(campos(linhas, "0990")[0][1]), sum(l.startswith("|0") for l in linhas))
        self.assertEqual(int(campos(linhas, "H990")[0][1]), sum(l.startswith("|H") for l in linhas))
        cont = {c[1]: int(c[2]) for c in campos(linhas, "9900")}
        for reg, n in cont.items():
            self.assertEqual(n, sum(l.startswith(f"|{reg}|") for l in linhas), reg)
        self.assertEqual(int(campos(linhas, "9999")[0][1]), len(linhas))

    def test_linha_de_item_que_nao_casa_vira_pendencia(self):
        # Linha de outro formato (sem unidade, total com ponto) não pode sumir calada
        quebrado = RELATORIO.replace("PC       2,000    2.300,5000      4.601,00", "2,000    2.300,5000      4601.00")
        res, linhas = gerar(arquivo("rel_quebrado.txt", quebrado.format(titulo="Registro de Inventário")),
                            uf="CE", cod_mun="2304400")
        self.assertEqual(len(campos(linhas, "H010")), 1)
        self.assertTrue(any(t == "arquivo" and "linha não lida" in m and "TV5500" in m for t, _, m in res.pendencias))


class Pendencias(unittest.TestCase):
    def test_quantidade_com_4_casas_avisa_e_mantem_o_valor(self):
        p = arquivo("qtd4.csv", "Codigo;Descricao;Unidade;Quantidade;Valor Unitario;Valor Total\n"
                                "Q1;TECIDO;M;1670,3408;2,00;3340,68\n")
        res, linhas = gerar(p, data_inv="31/12/2025")
        h010 = campos(linhas, "H010")[0]
        self.assertEqual(h010[3], "1670,341")
        self.assertEqual(h010[5], "3340,68")
        self.assertTrue(any(c == "Q1" and "1670,3408 arredondada para 1670,341" in m for _, c, m in res.pendencias))

    def test_csv_guarda_codigo_numerico_como_texto(self):
        # O Excel apagava o zero à esquerda: 0509 virava 509
        p = arquivo("zero.csv", "Codigo;Descricao;Unidade;Quantidade;Valor Unitario\n"
                                "0509;RELOGIO;UN;0;10,00\n0600;PULSEIRA;UN;2;5,00\n")
        res, _ = gerar(p, data_inv="31/12/2025")
        gs.gravar(res, Path(TMP) / "saida-csv")
        csv = (Path(TMP) / "saida-csv" / f"{res.base}_pendencias.csv").read_text(encoding="utf-8-sig")
        self.assertIn('"=""0509"""', csv)


class Datas(unittest.TestCase):
    def setUp(self):
        self.p = arquivo("datas.csv", "Codigo;Descricao;Quantidade;Valor Unit\nE1;X;1;1,00\n")

    def test_data_inexistente(self):
        with self.assertRaisesRegex(gs.ErroConversao, "inválida"):
            gerar(self.p, data_inv="31/02/2025")

    def test_periodo_mal_digitado(self):
        with self.assertRaisesRegex(gs.ErroConversao, "Período"):
            gerar(self.p, data_inv="31/12/2025", periodo="13/2025")
        with self.assertRaisesRegex(gs.ErroConversao, "Período"):
            gerar(self.p, data_inv="31/12/2025", periodo="fev")

    def test_periodo_depois_do_inventario(self):
        res, linhas = gerar(self.p, data_inv="31/12/2025", periodo="02/2026")
        self.assertEqual(campos(linhas, "0000")[0][3:5], ["01022026", "28022026"])


class Painel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        cls.proc = subprocess.Popen([sys.executable, str(APP / "painel.py"), "--porta", "0", "--sem-navegador"],
                                    stdout=subprocess.PIPE, text=True, env=env)
        linha = cls.proc.stdout.readline()
        assert linha.startswith("PORTA="), linha
        cls.porta = int(linha.split("=")[1])

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)

    def pedir(self, metodo, rota, corpo=None, **cab):
        c = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=10)
        cab.setdefault("Host", f"127.0.0.1:{self.porta}")
        c.request(metodo, rota, body=corpo, headers=cab)
        r = c.getresponse()
        return r.status, r.read()

    def test_painel_abre(self):
        status, corpo = self.pedir("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Estoque", corpo)

    def test_outro_site_nao_grava(self):
        status, _ = self.pedir("POST", "/api/escritorio", b'{"nome":"X"}', Origin="https://site-qualquer.com",
                               **{"Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        status, _ = self.pedir("POST", "/api/encerrar", Origin="https://site-qualquer.com")
        self.assertEqual(status, 403)

    def test_dns_rebinding(self):
        status, _ = self.pedir("GET", "/api/escritorio", Host=f"atacante.com:{self.porta}")
        self.assertEqual(status, 403)

    def test_o_proprio_painel_grava(self):
        status, corpo = self.pedir("POST", "/api/escritorio", json.dumps({"nome": "ESCRITORIO", "cpf": "1"}).encode(),
                                   Origin=f"http://127.0.0.1:{self.porta}", **{"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(corpo)), (200, {"ok": True}))
        status, corpo = self.pedir("GET", "/api/escritorio", Host=f"localhost:{self.porta}")
        self.assertEqual(json.loads(corpo)["nome"], "ESCRITORIO")

    def test_sem_data_o_formulario_volta_e_depois_gera(self):
        _, corpo = self.pedir("POST", "/api/lote")
        lote = json.loads(corpo)["lote"]
        xls = (APP / "tests" / "arquivos" / "cod_produto_antigo.xls").read_bytes()
        self.pedir("POST", f"/api/enviar?lote={lote}&nome=sem_data.xls", xls)
        pedido = {"lote": lote, "arquivos": ["sem_data.xls"], "campos": {"cnpj": "11.222.333/0001-81", "uf": "ce"}}
        _, corpo = self.pedir("POST", "/api/converter", json.dumps(pedido).encode())
        r = json.loads(corpo)
        self.assertIn("não informa a data", r["erro"])
        self.assertEqual(r["campo"], "data_inv")  # a tela destaca e põe o cursor aqui
        self.assertEqual((r["campos"]["cnpj"], r["campos"]["uf"], r["campos"]["data_inv"]), ("11222333000181", "CE", ""))
        pedido["campos"]["data_inv"] = "31/12/2025"
        _, corpo = self.pedir("POST", "/api/converter", json.dumps(pedido).encode())
        r = json.loads(corpo)
        self.assertNotIn("erro", r)
        self.assertEqual((r["resumo"]["itens"], r["resumo"]["total"], r["campos"]["data_inv"]), (2, "124,67", "31/12/2025"))

    def test_converter_pelo_painel(self):
        _, corpo = self.pedir("POST", "/api/lote")
        lote = json.loads(corpo)["lote"]
        rel = RELATORIO.format(titulo="REGISTRO DE INVENTARIO").encode()
        self.pedir("POST", f"/api/enviar?lote={lote}&nome=rel.txt", rel)
        status, corpo = self.pedir("POST", "/api/converter", json.dumps(
            {"lote": lote, "arquivos": ["rel.txt"], "campos": {"uf": "CE", "cod_mun": "2304400", "data_inv": "31/02/2025"}}).encode())
        self.assertEqual(status, 200)
        self.assertIn("inválida", json.loads(corpo)["erro"])
        status, corpo = self.pedir("POST", "/api/converter", json.dumps(
            {"lote": lote, "arquivos": ["rel.txt"], "campos": {"uf": "CE", "cod_mun": "2304400"}}).encode())
        r = json.loads(corpo)
        self.assertEqual((r["resumo"]["itens"], r["resumo"]["total"]), (2, "6101,00"))
        status, corpo = self.pedir("GET", f"/baixar/{r['dominio']}")
        self.assertEqual(status, 200)
        self.assertIn(b"TV5500;TELEVISOR 55 POL;PC;85287200;;;2;2300,5;4601;0", corpo)
        status, _ = self.pedir("GET", "/api/gerados")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
