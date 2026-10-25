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


class FormatoDosArquivosReais(unittest.TestCase):
    """Cópias sintéticas (empresa e produtos inventados) dos arquivos reais de clientes:
    relatório de inventário com código e descrição juntos, .FS da Fortes e Estoque Escriturado."""

    def test_relatorio_codigo_e_descricao_juntos(self):
        res, reg = gerar("relatorio_formato_real.txt", uf="CE", cod_mun="2304400")
        cad = {c[1]: c[2] for c in reg("0200")}
        self.assertEqual(cad["5095"], "EXPOSITOR 6 BALDAS")  # sem o "- " na frente
        self.assertEqual(cad["5102"], "ISOTONIC DRINK MONODOSIS (LIMON)")  # "5102-" sem o traço
        self.assertEqual(cad["BG 99100-003-A"], "EXP MEDIO EM PLASTICO P/")  # prefixo de letras
        self.assertEqual(cad["GC 1999885"], "EXPOSITOR EM MADEIRA")
        self.assertEqual(cad["SEMCOD0001"], "SQUEEZE BRANCA 790ML")  # sem código no relatório
        # mesmo código com outra descrição: dois itens, o segundo renomeado e avisado
        self.assertEqual((cad["TI11397JS/61"], cad["TI11397JS/61-2"]), ("TIJA P/ RELOGIO 3H", "TIJA P/ RELOGIO 9H"))
        self.assertEqual(res.emp, {**res.emp, "nome": "COMERCIAL MODELO - FILIAL", "cnpj": "11222333000181", "ie": "061234567"})
        # o total impresso tem 1 centavo a mais que a soma das linhas: vale a soma (H005 = soma dos H010)
        self.assertEqual(res.total, Decimal("40734.74"))
        self.assertEqual(res.principal.total_declarado, Decimal("40734.75"))
        self.assertTrue(any(t == "total" for t, _, _ in res.pendencias))

    def test_fortes_layout_real(self):
        res, reg = gerar("fortes_layout_real.FS")
        h010 = {c[1]: c for c in reg("H010")}
        self.assertEqual(sorted(h010), ["10099", "10497", "BS1277"])  # código próprio quando existe
        self.assertEqual(h010["10497"][3:6], ["27060,000", "0,110000", "2976,60"])  # ponto decimal no .FS
        tipos = {c[1]: c[6] for c in reg("0200")}
        self.assertEqual(tipos, {"BS1277": "04", "10099": "01", "10497": "01"})  # tipo vem do grupo
        self.assertEqual(f"{res.data_inv:%d/%m/%Y}", "31/12/2024")
        self.assertEqual(res.total, Decimal("4598.20"))
        self.assertNotIn("FALSE", {c[1] for c in reg("0190")})

    def test_estoque_escriturado_sozinho_nao_gera_bloco_h_vazio(self):
        with self.assertRaisesRegex(gs.ErroConversao, "só tem quantidades"):
            gerar("estoque_escriturado_formato_real.xls", data_inv="31/12/2024")

    def test_sem_valores_avisa_antes_de_pedir_a_data(self):
        # O Estoque Escriturado não traz data nem valor: pedir a data primeiro não resolveria nada
        with self.assertRaisesRegex(gs.ErroConversao, "só tem quantidades") as erro:
            gerar("estoque_escriturado_formato_real.xls")
        self.assertIsNone(erro.exception.campo)

    def test_sem_data_aponta_o_campo(self):
        with self.assertRaises(gs.ErroConversao) as erro:
            gerar("cod_produto_antigo.xls")
        self.assertEqual(erro.exception.campo, "data_inv")

    def test_fs_com_estoque_escriturado_avisa_quantidade_divergente(self):
        res, reg = gerar("fortes_layout_real.FS", "estoque_escriturado_formato_real.xls")
        self.assertEqual(res.principal.caminho.name, "fortes_layout_real.FS")
        avisos = [(c, m) for t, c, m in res.pendencias if "difere da de" in m]
        self.assertEqual(avisos, [("10099", "quantidade 280,000 difere da de estoque_escriturado_formato_real.xls (300,000): mantida a do inventário")])
        self.assertEqual(res.total, Decimal("4598.20"))  # o complemento não muda o inventário


class ArquivoDominio(unittest.TestCase):
    def test_leiaute_da_planilha_modelo(self):
        res, _ = gerar("fortes_layout_real.FS")
        linhas = gs.linhas_dominio(res)
        self.assertIn("10497;REGULADOR DE METAL 7MM;UN;83089010;;;27060;0,11;2976,6;0", linhas)
        self.assertEqual({len(l.split(";")) for l in linhas}, {10})
        soma = sum(gs.num(l.split(";")[8], br=True) for l in linhas)
        self.assertEqual(soma, res.total)

    def test_gravado_ao_lado_do_sped(self):
        res, _ = gerar("loja_cp1252.csv", data_inv="31/12/2025")
        sped, _, dom = gs.gravar(res, tempfile.mkdtemp())
        self.assertEqual(dom.name, sped.name.replace("SPED_", "DOMINIO_"))
        bruto = dom.read_bytes()
        self.assertIn("CALÇA JEANS 42".encode("cp1252"), bruto)  # Windows-1252, como a macro grava
        self.assertTrue(bruto.endswith(b"\r\n") and b"\n" not in bruto.replace(b"\r\n", b""))

    def test_numeros_como_o_excel_grava(self):
        self.assertEqual(gs.numero_dominio(Decimal("280.000")), "280")
        self.assertEqual(gs.numero_dominio(Decimal("0.068900")), "0,0689")
        self.assertEqual(gs.numero_dominio(None), "0")


class ModeloDePlanilha(unittest.TestCase):
    """O modelo que o escritório manda para o cliente (modelos/Modelo_Inventario_Estoque_SPED.xlsx)."""

    def test_modelo_em_branco_nao_gera_sped_vazio(self):
        fonte = gs.ler_arquivo(APP.parent / "modelos" / "Modelo_Inventario_Estoque_SPED.xlsx")
        with self.assertRaisesRegex(gs.ErroConversao, "não tem nenhum item"):
            gs.converter(fonte, [], gs.opcoes(sem_consulta=True))

    def test_modelo_preenchido(self):
        res, reg = gerar("modelo_preenchido.xlsx", uf="CE", cod_mun="2304400", ie="061234567",
                         cep="60000000", end="RUA UM", bairro="CENTRO")
        self.assertEqual(res.emp["nome"], "COMERCIAL EXEMPLO LTDA")  # lido da célula "Empresa:"
        self.assertEqual(res.emp["cnpj"], "12ABC34501DE35")
        self.assertEqual(f"{res.data_inv:%d/%m/%Y}", "31/12/2025")
        self.assertEqual(res.total, res.principal.total_declarado)  # "Total do inventário:" do topo
        h010 = {c[1]: c for c in reg("H010")}
        self.assertEqual(h010["A-77"][2:6], ["PCT", "40,000", "6,789000", "271,56"])  # só o total; "Pacote"
        self.assertEqual([p for p in res.pendencias if p[0] != "arquivo"], [])


class CnpjAlfanumerico(unittest.TestCase):
    def test_exemplos_oficiais(self):
        self.assertTrue(gs.cnpj_valido("12.ABC.345/01DE-35"))  # exemplo do Serpro
        self.assertTrue(gs.cnpj_valido("00.000.000/E08G-12"))  # primeiro emitido, 31/07/2026
        self.assertTrue(gs.cnpj_valido("11.222.333/0001-81"))
        self.assertFalse(gs.cnpj_valido("12ABC34501DE36"))
        self.assertFalse(gs.cnpj_valido("12ABC34501DEA5"))  # DV é sempre número


if __name__ == "__main__":
    unittest.main()
