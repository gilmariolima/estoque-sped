"""Painel local do conversor de inventário: http://127.0.0.1:8765

No app desktop a porta é escolhida na hora (--porta 0) e o painel avisa qual é numa linha
"PORTA=<n>" na saída padrão, que o Electron lê para abrir a janela.
"""

import argparse
import hashlib
import hmac
import subprocess
import json
import os
import re
import secrets
import shutil
import sys
import threading
import traceback
import uuid
import webbrowser
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import gerar_sped as gs

PORTA = 8765  # padrão quando roda fora do app desktop
LIMITE_ENVIO = 300 * 1024 * 1024  # arquivo do cliente maior que isso não é inventário
PASTA = Path(__file__).resolve().parent
WEB = PASTA / "painel"
SAIDA = gs.DADOS / "saida"
ENTRADAS = gs.DADOS / "entradas"
TIPOS = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2",
         ".ttf": "font/ttf", ".webmanifest": "application/manifest+json"}
CAMPOS_EMPRESA = ("nome", "ie", "uf", "cod_mun", "perfil", "ind_ativ", "cep", "end", "num", "compl", "bairro")
# Política da página: só o que vem do próprio painel (sem script de outro site nem script embutido)
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
# Nomes que o Windows reserva para dispositivos: "CON.txt" não é um arquivo
RESERVADOS = re.compile(r"(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³])(\..*)?", re.I)
# Código do desenho do ícone no endereço do favicon: o Edge guarda o ícone por endereço
VERSAO_ICONE = hashlib.sha1((WEB / "icone.svg").read_bytes()).hexdigest()[:8]
trava = threading.Lock()


def nome_seguro(nome):
    """Só o nome do arquivo, sem pasta, sem caractere proibido no Windows e sem "." ou ".."."""
    nome = unquote(nome).replace("\\", "/").rsplit("/", 1)[-1]
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", nome).strip().rstrip(". ")  # o Windows apaga ponto e espaço do fim
    if not nome.strip("."):
        return "arquivo"
    return "_" + nome if RESERVADOS.fullmatch(nome) else nome


def dentro(pasta, nome):
    """Arquivo `nome` dentro de `pasta`, nunca fora dela."""
    arq = pasta / nome_seguro(nome)
    if arq.resolve().parent != pasta.resolve():
        raise PedidoInvalido("nome de arquivo inválido")
    return arq


class PedidoInvalido(ValueError):
    """Pedido malformado: responde 400, não 500."""


def arquivo_web(caminho):
    """Arquivo estático de painel/, aceitando subpastas mas nunca saindo dela."""
    arq = (WEB / unquote(caminho)).resolve()
    return arq if arq.is_file() and WEB.resolve() in arq.parents else None


def pasta_lote(lote):
    if not re.fullmatch(r"[0-9a-f]{12}", lote or ""):
        raise PedidoInvalido("lote inválido")
    return ENTRADAS / lote


def resumo_fonte(caminho):
    try:
        f = gs.ler_arquivo(caminho)
        return f, {"nome": caminho.name, "tipo": f.tipo, "itens": len(f.itens), "valores": f.tem_valores}
    except Exception as e:
        return None, {"nome": caminho.name, "erro": str(e)}


def campos_conhecidos(principal, extras, campos, args):
    """Dados da empresa e do inventário que já se sabem sem conseguir gerar o arquivo:
    o que foi digitado, o que veio nos arquivos e o cadastro salvo da empresa."""
    emp = {}
    for f in reversed([principal, *extras]):
        emp.update({k: v for k, v in f.empresa.items() if v})
    cnpj = gs.so_cnpj(campos.get("cnpj") or emp.get("cnpj"))
    salvo = gs.carregar_json(gs.CADASTRO_EMPRESAS).get(cnpj, {}) if cnpj else {}
    valor = lambda k, padrao="": campos.get(k) or emp.get(k) or salvo.get(k) or padrao
    data = principal.data_inv or next((f.data_inv for f in extras if f.data_inv), None)
    return {
        "cnpj": cnpj, "ie": valor("ie"), "uf": valor("uf").upper(), "cod_mun": valor("cod_mun"), "nome": valor("nome"),
        "perfil": valor("perfil", "A"), "ind_ativ": valor("ind_ativ", "1"), "periodo": campos.get("periodo", ""),
        "data_inv": campos.get("data_inv") or (f"{data:%d/%m/%Y}" if data else ""), "tipo_item": args.tipo_item,
        **{k: valor(k) for k in ("cep", "end", "num", "compl", "bairro")},
    }


def converter(dados):
    pasta = pasta_lote(dados.get("lote"))
    ordem = dados.get("arquivos") or []
    caminhos = [dentro(pasta, n) for n in ordem if dentro(pasta, n).exists()]
    fontes, arquivos = [], []
    for c in caminhos:
        f, info = resumo_fonte(c)
        arquivos.append(info)
        if f:
            fontes.append(f)
    if not fontes:
        return {"erro": "Nenhum arquivo pôde ser lido.", "arquivos": arquivos}

    escolhido = dados.get("principal")
    principal = next((f for f in fontes if f.caminho.name == escolhido), None) or gs.escolher_principal(fontes)
    extras = [f for f in fontes if f is not principal]
    campos = {k: str(v if v is not None else "").strip() for k, v in (dados.get("campos") or {}).items()}

    args = gs.opcoes(
        cnpj=campos.get("cnpj") or None, ie=campos.get("ie") or None, uf=(campos.get("uf") or "").upper() or None,
        cod_mun=campos.get("cod_mun") or None, nome=campos.get("nome") or None,
        perfil=campos.get("perfil") or None, ind_ativ=campos.get("ind_ativ") or None,
        periodo=campos.get("periodo") or None, data_inv=campos.get("data_inv") or None,
        tipo_item=campos.get("tipo_item") or "00",
        **{k: campos.get(k) or None for k in ("cep", "end", "num", "compl", "bairro")},
    )
    with trava:
        cnpj = gs.so_cnpj(campos.get("cnpj"))
        if cnpj and gs.cnpj_valido(cnpj):
            cad = gs.carregar_json(gs.CADASTRO_EMPRESAS)
            reg = cad.setdefault(cnpj, {})
            reg.update({k: campos[k] for k in CAMPOS_EMPRESA if campos.get(k)})
            gs.CADASTRO_EMPRESAS.write_text(json.dumps(cad, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            res = gs.converter(principal, extras, args)
        except gs.ErroConversao as e:
            # Devolve o que já se sabe para a tela mostrar o formulário: sem ele não haveria
            # onde digitar a data que falta (ex.: Estoque Escriturado, que não traz data)
            return {"erro": str(e), "campo": e.campo, "arquivos": arquivos, "principal": principal.caminho.name,
                    "campos": campos_conhecidos(principal, extras, campos, args)}
        arq, arq_pend, arq_dom = gs.gravar(res, SAIDA)

    # As pendências de item vêm do próprio item: com código repetido (próprio e em terceiros,
    # ou um renomeado), agrupar pelo código mostraria o aviso no item errado
    gerais = [{"tipo": tipo, "msg": msg} for tipo, _, msg in res.pendencias if tipo != "item"]
    itens = []
    for it in res.itens:
        vl_item = it.vl_item.quantize(gs.DOIS)
        itens.append({
            "c": it.codigo, "d": it.descricao, "n": it.ncm, "u": it.unidade,
            "q": gs.fmt(it.qtd, 3), "vu": gs.fmt(it.vl_unit, 6), "vt": gs.fmt(vl_item, 2),
            "t": it.tipo or args.tipo_item, "p": list(it.pendencias),
        })
    fora = [{"c": it.codigo, "p": list(it.pendencias)} for it in res.fora]
    emp = res.emp
    return {
        "arquivos": arquivos,
        "principal": principal.caminho.name,
        "campos": {
            "cnpj": emp.get("cnpj", ""), "ie": emp.get("ie", ""), "uf": emp.get("uf", ""),
            "cod_mun": emp.get("cod_mun", ""), "nome": emp.get("nome", ""), "perfil": emp.get("perfil", "A"),
            "ind_ativ": emp.get("ind_ativ", "1"), "periodo": f"{res.ini:%m/%Y}", "data_inv": f"{res.data_inv:%d/%m/%Y}",
            "tipo_item": args.tipo_item, **{k: emp.get(k, "") for k in ("cep", "end", "num", "compl", "bairro")},
        },
        "resumo": {
            "itens": len(res.itens), "total": gs.fmt(res.total, 2), "linhas": len(res.registros),
            "origem": gs.fmt(res.principal.total_declarado, 2) if res.principal.total_declarado is not None else "",
            "leiaute": res.registros[0].split("|")[2],
            "periodo": f"{res.ini:%d/%m/%Y} a {res.fim:%d/%m/%Y}",
            # o arquivo leva só o inventário: não substitui a EFD do mês com as notas e a apuração
            "conteudo": "Só bloco H (E110 zerada)",
        },
        "itens": itens, "fora": fora, "gerais": gerais,
        "arquivo": arq.name, "pendencias": arq_pend.name if res.pendencias else "", "dominio": arq_dom.name,
    }


def gerados():
    lista = []
    for arq in sorted(SAIDA.glob("SPED_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True):
        info = {"arquivo": arq.name, "data": datetime.fromtimestamp(arq.stat().st_mtime).strftime("%d/%m/%Y %H:%M"),
                "nome": "", "cnpj": "", "inventario": "", "total": "", "itens": 0, "linhas": 0}
        with open(arq, encoding="latin-1") as fp:
            for linha in fp:
                c = linha.rstrip("\r\n").split("|")
                info["linhas"] += 1
                if len(c) < 4:  # linha em branco ou arquivo mexido à mão
                    continue
                if c[1] == "0000" and len(c) > 7:
                    info["nome"], info["cnpj"] = c[6], c[7]
                elif c[1] == "H005":
                    info["inventario"] = f"{c[2][:2]}/{c[2][2:4]}/{c[2][4:]}"
                    info["total"] = c[3]
                elif c[1] == "H010":
                    info["itens"] += 1
        pend = arq.with_name(arq.stem + "_pendencias.csv")
        info["pendencias"] = pend.name if pend.exists() else ""
        dom = arq.with_name("DOMINIO_" + arq.name.removeprefix("SPED_"))
        info["dominio"] = dom.name if dom.exists() else ""
        lista.append(info)
    return lista


class Painel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def responder(self, corpo, tipo="application/json; charset=utf-8", status=200, extra=None):
        if not isinstance(corpo, bytes):
            corpo = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def falha(self, erro):
        print(f"{datetime.now():%d/%m/%Y %H:%M:%S} {self.command} {self.path}", file=sys.stderr)
        traceback.print_exc()
        self.responder({"erro": str(erro)}, status=500)

    def corpo(self):
        try:
            tam = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise PedidoInvalido("Content-Length inválido")
        if tam < 0:  # read(-1) leria até a conexão fechar, sem limite
            raise PedidoInvalido("Content-Length inválido")
        if tam > LIMITE_ENVIO:
            raise PedidoInvalido("arquivo grande demais")
        return self.rfile.read(tam) if tam else b""

    def autorizado(self):
        """O pedido traz o token desta execução (cookie da página ou cabeçalho do app).

        Sem token, qualquer programa ou outro usuário logado no mesmo computador (servidor de
        terminal) conseguiria usar o painel pela porta local."""
        token = self.server.token.encode()
        enviados = [self.headers.get("X-Estoque-Token") or ""]
        try:
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            if (c := cookie.get(self.nome_cookie())) is not None:
                enviados.append(c.value)
        except CookieError:
            pass
        return any(v and hmac.compare_digest(v.encode(), token) for v in enviados)

    def nome_cookie(self):
        # cookie não separa por porta: o nome leva a porta, para dois painéis não se misturarem
        return f"estoque_{self.server.server_address[1]}"

    def recusar(self, rota):
        if rota == "/":
            pagina = ("<!doctype html><meta charset='utf-8'><title>Estoque SPED</title>"
                      "<p style='font-family:sans-serif;margin:40px'>Abra o Estoque SPED pelo atalho do programa.</p>")
            return self.responder(pagina.encode("utf-8"), TIPOS[".html"], status=401)
        self.responder({"erro": "pedido sem autorização: abra o painel pelo programa"}, status=401)

    def de_fora(self):
        """Recusa pedidos que não vêm do próprio painel.

        O servidor só escuta em 127.0.0.1, mas qualquer site aberto no navegador consegue
        mandar pedidos para cá. O Host confere que o endereço é o nosso (protege contra
        DNS rebinding, que permitiria a um site LER as respostas: CPF do contador, SPEDs dos
        clientes) e o Origin confere que o pedido saiu de uma página do painel (protege
        contra outro site gravar, apagar ou encerrar)."""
        porta = self.server.server_address[1]
        locais = {f"127.0.0.1:{porta}", f"localhost:{porta}"}
        host = (self.headers.get("Host") or "").strip().lower()
        origem = (self.headers.get("Origin") or "").strip().lower()
        if host not in locais or (origem and origem.removeprefix("http://") not in locais):
            self.responder({"erro": "pedido recusado: só o próprio painel pode usar este endereço"}, status=403)
            return True
        return False

    def do_GET(self):
        if self.de_fora():
            return
        url = urlparse(self.path)
        rota, q = url.path, parse_qs(url.query)
        try:
            if rota.startswith("/web/") or rota == "/favicon.ico":  # estáticos, sem dado nenhum
                arq = arquivo_web(rota[5:] if rota.startswith("/web/") else "icone.ico")
                if arq and arq.suffix == ".webmanifest":
                    texto = arq.read_text(encoding="utf-8").replace("__ICONE__", VERSAO_ICONE)
                    return self.responder(texto.encode("utf-8"), TIPOS[".webmanifest"])
                if arq:
                    return self.responder(arq.read_bytes(), TIPOS.get(arq.suffix, "application/octet-stream"))
                return self.responder({"erro": "não encontrado"}, status=404)
            if rota == "/" and "t" in q:
                # O programa abre /?t=<token>: guarda o token num cookie e tira ele do endereço
                if not hmac.compare_digest(q["t"][0].encode(), self.server.token.encode()):
                    return self.recusar(rota)
                cookie = f"{self.nome_cookie()}={self.server.token}; Path=/; HttpOnly; SameSite=Strict"
                return self.responder(b"", "text/plain", status=303, extra={"Location": "/", "Set-Cookie": cookie})
            if not self.autorizado():
                return self.recusar(rota)
            if rota == "/":
                pagina = (WEB / "index.html").read_text(encoding="utf-8").replace("__ICONE__", VERSAO_ICONE)
                return self.responder(pagina.encode("utf-8"), TIPOS[".html"])
            if rota == "/api/gerados":
                return self.responder(gerados())
            if rota == "/api/escritorio":
                return self.responder(gs.carregar_json(gs.ESCRITORIO))
            if rota.startswith("/baixar/"):
                arq = dentro(SAIDA, rota[8:])
                if arq.is_file():
                    tipo = "text/csv; charset=utf-8" if arq.suffix == ".csv" else "text/plain; charset=iso-8859-1"
                    return self.responder(arq.read_bytes(), tipo, extra={
                        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(arq.name)}"})
            if rota.startswith("/api/ver/"):
                arq = dentro(SAIDA, rota[9:])
                if arq.is_file():
                    de = (q.get("de") or ["0"])[0]
                    de = int(de) if de.isdigit() else 0
                    linhas = arq.read_text(encoding="latin-1").splitlines()
                    return self.responder({"total": len(linhas), "de": de, "linhas": linhas[de:de + 1000]})
            self.responder({"erro": "não encontrado"}, status=404)
        except PedidoInvalido as e:
            self.responder({"erro": str(e)}, status=400)
        except Exception as e:
            self.falha(e)

    def do_POST(self):
        if self.de_fora():
            return
        if not self.autorizado():
            return self.recusar(urlparse(self.path).path)
        url = urlparse(self.path)
        rota, q = url.path, parse_qs(url.query)
        try:
            if rota == "/api/lote":
                lote = uuid.uuid4().hex[:12]
                pasta_lote(lote).mkdir(parents=True, exist_ok=True)
                return self.responder({"lote": lote})
            if rota == "/api/enviar":
                pasta = pasta_lote(q["lote"][0])
                pasta.mkdir(parents=True, exist_ok=True)
                destino = dentro(pasta, q["nome"][0])
                destino.write_bytes(self.corpo())
                return self.responder({"nome": destino.name})
            if rota == "/api/converter":
                return self.responder(converter(json.loads(self.corpo() or b"{}")))
            if rota == "/api/encerrar":  # usado pelo instalador antes de trocar os arquivos
                self.responder({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if rota == "/api/abrir-pasta":
                SAIDA.mkdir(parents=True, exist_ok=True)
                abrir_pasta(SAIDA)
                return self.responder({"ok": True})
            if rota == "/api/escritorio":
                dados = {k: str(v).strip() for k, v in json.loads(self.corpo() or b"{}").items() if isinstance(k, str)}
                with trava:
                    gs.DADOS.mkdir(parents=True, exist_ok=True)
                    gs.ESCRITORIO.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
                return self.responder({"ok": True})
            self.responder({"erro": "não encontrado"}, status=404)
        except PedidoInvalido as e:
            self.responder({"erro": str(e)}, status=400)
        except Exception as e:
            self.falha(e)

    def do_DELETE(self):
        if self.de_fora():
            return
        if not self.autorizado():
            return self.recusar(urlparse(self.path).path)
        url = urlparse(self.path)
        rota, q = url.path, parse_qs(url.query)
        try:
            if rota == "/api/enviar":
                arq = dentro(pasta_lote(q["lote"][0]), q["nome"][0])
                arq.unlink(missing_ok=True)
                return self.responder({"ok": True})
            if rota.startswith("/api/gerado/"):
                arq = dentro(SAIDA, rota[12:])
                arq.unlink(missing_ok=True)
                arq.with_name(arq.stem + "_pendencias.csv").unlink(missing_ok=True)
                arq.with_name("DOMINIO_" + arq.name.removeprefix("SPED_")).unlink(missing_ok=True)
                return self.responder({"ok": True})
            self.responder({"erro": "não encontrado"}, status=404)
        except PedidoInvalido as e:
            self.responder({"erro": str(e)}, status=400)
        except Exception as e:
            self.falha(e)


def abrir_pasta(pasta):
    if sys.platform == "win32":
        os.startfile(pasta)
    else:  # só para rodar o painel em desenvolvimento fora do Windows
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(pasta)])


def limpar_entradas_antigas():
    if not ENTRADAS.exists():
        return
    agora = datetime.now().timestamp()
    for p in ENTRADAS.iterdir():
        if p.is_dir() and agora - p.stat().st_mtime > 7 * 86400:
            shutil.rmtree(p, ignore_errors=True)


def main():
    if sys.stdout:  # pythonw roda sem console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is None:
        gs.DADOS.mkdir(parents=True, exist_ok=True)
        sys.stderr = open(gs.DADOS / "painel.log", "a", encoding="utf-8", buffering=1)
    ap = argparse.ArgumentParser(description="Painel do Estoque SPED")
    ap.add_argument("--porta", type=int, default=int(os.environ.get("ESTOQUE_PORTA") or PORTA),
                    help="porta local (0 = qualquer porta livre, usado pelo app desktop)")
    ap.add_argument("--sem-navegador", action="store_true", help="não abre o navegador")
    opc = ap.parse_args()

    limpar_entradas_antigas()
    # O app desktop passa o token pelo ambiente; rodando sozinho, o painel cria um e guarda na
    # pasta de dados (que é só do usuário), para a segunda abertura achar a janela já aberta
    arq_token = gs.DADOS / "painel.token"
    token = os.environ.get("ESTOQUE_TOKEN") or ""
    try:
        servidor = ThreadingHTTPServer(("127.0.0.1", opc.porta), Painel)
    except OSError:
        endereco = f"http://127.0.0.1:{opc.porta}"
        print(f"O painel já está aberto em {endereco}", flush=True)
        if not opc.sem_navegador:
            salvo = arq_token.read_text(encoding="utf-8").strip() if arq_token.exists() else ""
            webbrowser.open(f"{endereco}/?t={token or salvo}")
        sys.exit(3)
    if not token:
        token = secrets.token_urlsafe(32)
        gs.DADOS.mkdir(parents=True, exist_ok=True)
        arq_token.write_text(token, encoding="utf-8")
    servidor.token = token
    porta = servidor.server_address[1]
    endereco = f"http://127.0.0.1:{porta}"
    print(f"PORTA={porta}", flush=True)
    print(f"Painel aberto em {endereco}  (feche esta janela para encerrar)", flush=True)
    if not opc.sem_navegador:
        threading.Timer(0.6, webbrowser.open, [f"{endereco}/?t={token}"]).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
