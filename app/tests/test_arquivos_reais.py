"""Arquivos no formato que os sistemas exportam de verdade (pasta arquivos/), com as sujeiras
de sempre: título antes do cabeçalho, linha de total, quebra de página, Windows-1252, "R$",
NCM sem o zero da frente, unidade por extenso, CNPJ com letras.

Cada caso confere o total contra o total escrito no próprio arquivo do cliente, quando ele tem."""

import os
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
ARQ = Path(__file__).resolve().parent / "arquivos"
os.environ.setdefault("ESTOQUE_DADOS", tempfile.mkdtemp(prefix="estoque-teste-"))
sys.path.insert(0, str(APP))

import gerar_sped as gs  # noqa: E402


def gerar(*nomes, **opcoes):
    fontes = [gs.ler_arquivo(ARQ / n) for n in nomes]
    principal = gs.escolher_principal(fontes)
    res = gs.converter(principal, [f for f in fontes if f is not principal], gs.opcoes(sem_consulta=True, **opcoes))
    reg = lambda r: [l.strip("|").split("|") for l in res.registros if l.startswith(f"|{r}|")]
    return res, reg


class ArquivosReais(unittest.TestCase):
    def confere_total(self, res):
        if res.principal.total_declarado is not None:
            self.assertEqual(res.total, res.principal.total_declarado, "total gerado difere do total do arquivo")

    def test_erp_com_titulo_linha_de_total_e_cnpj_com_letras(self):
        res, reg = gerar("erp_titulo_e_total.xlsx")
        self.assertEqual([c[1] for c in reg("H010")], ["1001", "1002", "001003"])  # "TOTAL GERAL" não é item
        self.assertEqual(res.total, Decimal("2317.56"))
        self.confere_total(res)
        self.assertEqual(res.emp["cnpj"], "12ABC34501DE35")  # CNPJ alfanumérico lido do título
        self.assertEqual(f"{res.data_inv:%d/%m/%Y}", "31/12/2025")  # data lida do título, não a de emissão
        self.assertFalse(any("dígito verificador" in p[2] for p in res.pendencias))
        self.assertEqual(reg("0200")[1][7], "07133319")  # NCM com pontos

    def test_xls_antigo_so_com_cod_produto(self):
        res, reg = gerar("cod_produto_antigo.xls", data_inv="31/12/2025")
        self.assertEqual([(c[1], c[3], c[5]) for c in reg("H010")], [("20", "3,500", "43,17"), ("21", "1000,000", "81,50")])

    def test_csv_windows_1252_com_milhar_e_total(self):
        res, reg = gerar("loja_cp1252.csv", data_inv="31/12/2025")
        self.assertEqual(reg("0200")[1][2], "CALÇA JEANS 42")
        self.assertEqual(reg("H010")[0][3], "1250,000")
        self.confere_total(res)
        self.assertEqual(res.principal.total_declarado, Decimal("32516.50"))

    def test_relatorio_p7_com_quebra_de_pagina(self):
        res, reg = gerar("relatorio_p7_paginas.txt", uf="CE", cod_mun="2304400")
        self.assertEqual(len(reg("H010")), 3)  # "A TRANSPORTAR" e "TRANSPORTE" não são itens
        self.assertEqual(f"{res.data_inv:%d/%m/%Y}", "31/12/2025")  # "ESTOQUES EXISTENTES EM"
        self.assertEqual(res.emp["nome"], "MERCADINHO BOM PRECO LTDA")
        self.confere_total(res)

    def test_sped_completo_de_outro_sistema(self):
        res, reg = gerar("sped_completo.txt")
        self.assertEqual(reg("0200")[0][3], "7890000000017")  # código de barras mantido
        self.assertEqual(len(reg("C100")), 0)  # só o inventário vem junto
        self.confere_total(res)

    def test_segunda_aba_reais_ncm_numerico_unidade_por_extenso(self):
        res, reg = gerar("segunda_aba_reais.xlsx")
        h010 = {c[1]: c for c in reg("H010")}
        self.assertEqual(h010["X1"][2:6], ["UN", "10,000", "8,500000", "85,00"])  # "Saldo" e "R$ 8,50"
        self.assertEqual(h010["X2"][2], "PC")  # "Peça"
        self.assertEqual({c[1]: c[7] for c in reg("0200")}["X1"], "07133390")  # zero do NCM de volta
        self.assertEqual(f"{res.data_inv:%d/%m/%Y}", "31/12/2025")  # data numa célula de data

    def test_csv_utf8_bom_ponto_decimal_em_ingles(self):
        res, reg = gerar("ingles_utf8_bom.csv", data_inv="31/12/2025")
        self.assertEqual(res.total, Decimal("739.80"))

    def test_fortes_fs(self):
        res, reg = gerar("INV2025.FS")
        self.assertEqual(res.emp["cnpj"], "11222333000181")
        self.assertEqual([c[1] for c in reg("H010")], ["A100", "A200"])  # código próprio, não o interno
        self.assertEqual(res.total, Decimal("1455.00"))

    def test_relatorio_com_ncm_00_completado_pelo_cadastro(self):
        res, reg = gerar("relatorio_ncm_00.txt", "cadastro_ncm.csv", uf="CE", cod_mun="2304400")
        self.assertEqual(reg("0200")[0][7], "96081000")
        self.confere_total(res)

    def test_planilha_de_60_mil_linhas(self):
        import openpyxl
        caminho = Path(tempfile.mkdtemp()) / "grande.xlsx"
        wb = openpyxl.Workbook(write_only=True)
        ws = wb.create_sheet("Estoque")
        ws.append(["Código", "Descrição", "NCM", "Unid", "Quantidade", "Valor Unitário", "Valor Total"])
        for i in range(60000):
            ws.append([f"P{i:06d}", f"PRODUTO {i}", "84713012", "UN", 3, 10.5, 31.5])
        wb.save(caminho)
        ini = time.perf_counter()
        fonte = gs.ler_arquivo(caminho)
        res = gs.converter(fonte, [], gs.opcoes(sem_consulta=True, data_inv="31/12/2025"))
        self.assertEqual((len(res.itens), res.total), (60000, Decimal("1890000.00")))
        self.assertLess(time.perf_counter() - ini, 60)


class CnpjAlfanumerico(unittest.TestCase):
    def test_exemplos_oficiais(self):
        self.assertTrue(gs.cnpj_valido("12.ABC.345/01DE-35"))  # exemplo do Serpro
        self.assertTrue(gs.cnpj_valido("00.000.000/E08G-12"))  # primeiro emitido, 31/07/2026
        self.assertTrue(gs.cnpj_valido("11.222.333/0001-81"))
        self.assertFalse(gs.cnpj_valido("12ABC34501DE36"))
        self.assertFalse(gs.cnpj_valido("12ABC34501DEA5"))  # DV é sempre número


if __name__ == "__main__":
    unittest.main()
