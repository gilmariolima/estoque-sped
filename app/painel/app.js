const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const milhar = (v) => {
  if (v === "" || v == null) return "";
  const [i, d] = String(v).split(",");
  return i.replace(/\B(?=(\d{3})+(?!\d))/g, ".") + (d !== undefined ? "," + d : "");
};
const POR_PAGINA = 300;

const estado = { lote: null, arquivos: [], principal: null, editados: new Set(), res: null, filtro: "todos", busca: "", limite: POR_PAGINA };

async function api(url, opcoes = {}) {
  const r = await fetch(url, opcoes);
  const dados = await r.json().catch(() => ({ erro: "resposta inválida do servidor" }));
  if (!r.ok && !dados.erro) dados.erro = `erro ${r.status}`;
  return dados;
}

function aviso(msg, tipo = "ok") {
  const el = document.createElement("div");
  el.className = `aviso aviso-${tipo}`;
  el.innerHTML = `<i class="fa-solid ${tipo === "ok" ? "fa-circle-check" : "fa-triangle-exclamation"}"></i><span>${esc(msg)}</span>`;
  $("#avisos").append(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, 4000);
}

function processando(txt) {
  $("#processando").hidden = !txt;
  if (txt) $("#procTxt").textContent = txt;
}

/* ---------------- abas ---------------- */
function abrirAba(nome) {
  $$(".navbtn").forEach((b) => b.classList.toggle("ativo", b.dataset.aba === nome));
  $$("main > section").forEach((s) => (s.hidden = s.id !== `aba-${nome}`));
  if (nome === "gerados") carregarGerados();
  if (nome === "escritorio") carregarEscritorio();
}
$$(".navbtn").forEach((b) => b.addEventListener("click", () => abrirAba(b.dataset.aba)));

/* ---------------- envio de arquivos ---------------- */
async function enviar(lista) {
  const arquivos = [...lista];
  if (!arquivos.length) return;
  processando("Enviando arquivos…");
  try {
    if (!estado.lote) estado.lote = (await api("/api/lote", { method: "POST" })).lote;
    for (const f of arquivos) {
      const r = await api(`/api/enviar?lote=${estado.lote}&nome=${encodeURIComponent(f.name)}`, { method: "POST", body: f });
      if (r.erro) { aviso(`${f.name}: ${r.erro}`, "erro"); continue; }
      if (!estado.arquivos.includes(r.nome)) estado.arquivos.push(r.nome);
    }
  } finally {
    processando(null);
  }
  await converter();
}

async function converter(msg = "Gerando o SPED…") {
  if (!estado.arquivos.length) return limparResultado();
  const campos = {};
  const form = $("#formEmp");
  estado.editados.forEach((k) => (campos[k] = form.elements[k].value));
  processando(msg);
  let r;
  try {
    r = await api("/api/converter", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lote: estado.lote, arquivos: estado.arquivos, principal: estado.principal, campos }),
    });
  } finally {
    processando(null);
  }
  estado.res = r;
  estado.principal = r.principal || estado.principal;
  desenharArquivos(r.arquivos || []);
  $("#erroConv").hidden = !r.erro;
  $("#erroConv").textContent = r.erro || "";
  if (r.erro) {
    $("#resultado").hidden = !r.campos;
    $("#btnBaixar").hidden = true;
    return;
  }
  estado.limite = POR_PAGINA;
  desenharResultado();
  atualizarContaGerados();
}

function desenharArquivos(infos) {
  const tem = estado.arquivos.length > 0;
  $("#cardArquivos").hidden = !tem;
  $("#btnNovo").hidden = !tem;
  $("#solta").classList.toggle("compacta", tem);
  const porNome = Object.fromEntries(infos.map((i) => [i.nome, i]));
  $("#listaArquivos").innerHTML = estado.arquivos.map((nome, n) => {
    const i = porNome[nome] || { nome };
    let uso;
    if (i.erro) uso = `<span class="carimbo c-recus" title="${esc(i.erro)}">Não lido</span>`;
    else if (nome === estado.principal) uso = `<span class="carimbo c-rec">Inventário</span>`;
    else uso = `<span class="tag" title="Completa descrição, NCM, tipo e custo dos itens do inventário">Complemento</span>`;
    const trocar = !i.erro && nome !== estado.principal
      ? `<button class="icon-btn" data-principal="${esc(nome)}" title="Usar este arquivo como inventário"><i class="fa-solid fa-star"></i></button>` : `<span class="vago"></span>`;
    return `<tr>
      <td class="td canhoto"><span class="carbono">${String(n + 1).padStart(2, "0")}</span></td>
      <td class="td"><span class="nome-arq">${esc(nome)}</span></td>
      <td class="td col-larga">${esc(i.tipo || "")}${i.valores === false ? ' <span class="sub">sem valores</span>' : ""}</td>
      <td class="td direita carbono">${i.itens != null ? milhar(i.itens) : ""}</td>
      <td class="td">${uso}</td>
      <td class="td"><div class="acoes">${trocar}<button class="icon-btn perigo" data-tirar="${esc(nome)}" title="Tirar arquivo"><i class="fa-solid fa-xmark"></i></button></div></td>
    </tr>`;
  }).join("");
  const n = estado.arquivos.length;
  const r = estado.res;
  $("#contaConv").textContent = !n ? "Nenhum arquivo"
    : `${n} ${n > 1 ? "arquivos" : "arquivo"}` + (r && r.campos ? ` · inventário em ${r.campos.data_inv}` : "");
}

$("#listaArquivos").addEventListener("click", async (e) => {
  const tirar = e.target.closest("[data-tirar]");
  const principal = e.target.closest("[data-principal]");
  if (tirar) {
    const nome = tirar.dataset.tirar;
    await api(`/api/enviar?lote=${estado.lote}&nome=${encodeURIComponent(nome)}`, { method: "DELETE" });
    estado.arquivos = estado.arquivos.filter((a) => a !== nome);
    if (estado.principal === nome) estado.principal = null;
    if (!estado.arquivos.length) return novoCliente();
    converter();
  }
  if (principal) {
    estado.principal = principal.dataset.principal;
    ["data_inv", "periodo"].forEach((k) => estado.editados.delete(k));
    converter();
  }
});

/* ---------------- resultado ---------------- */
function itensFiltrados() {
  const b = estado.busca.trim().toUpperCase();
  return estado.res.itens.filter((i) =>
    (estado.filtro === "todos" || i.p.length) &&
    (!b || i.c.toUpperCase().includes(b) || i.d.toUpperCase().includes(b)));
}

function desenharItens() {
  const lista = itensFiltrados();
  const mostra = lista.slice(0, estado.limite);
  $("#listaItens").innerHTML = mostra.length ? mostra.map((i) => `<tr>
      <td class="td canhoto"><span class="carbono" style="font-size:17px">${esc(i.c)}</span></td>
      <td class="td d-col">${esc(i.d)}</td>
      <td class="td col-larga carbono" style="font-size:15px">${esc(i.n)}</td>
      <td class="td col-larga">${esc(i.u)}</td>
      <td class="td direita carbono">${milhar(i.q)}</td>
      <td class="td direita col-larga carbono">${milhar(i.vu)}</td>
      <td class="td direita carbono">${milhar(i.vt)}</td>
      <td class="td">${i.p.length ? `<span class="carimbo c-pend" title="${esc(i.p.join("\n"))}">Pendência</span>` : ""}</td>
    </tr>`).join("")
    : `<tr><td colspan="8" class="vazio-lista"><i class="fa-solid fa-magnifying-glass"></i><br>Nenhum item</td></tr>`;
  const resto = lista.length - mostra.length;
  $("#maisItens").hidden = resto <= 0;
  $("#maisItens").textContent = `Mostrar mais ${milhar(Math.min(resto, POR_PAGINA))} de ${milhar(resto)}`;
}

function desenharResultado() {
  const r = estado.res;
  $("#resultado").hidden = false;
  const form = $("#formEmp");
  Object.entries(r.campos).forEach(([k, v]) => { if (form.elements[k]) form.elements[k].value = v; });
  ["nome", "cnpj", "ie", "uf", "cod_mun"].forEach((k) => form.elements[k].classList.toggle("falta", !form.elements[k].value.trim()));
  $("#nTodos").textContent = milhar(r.itens.length);
  $("#nPend").textContent = milhar(r.itens.filter((i) => i.p.length).length);

  const linhas = [["Total do inventário", milhar(r.resumo.total)]];
  if (r.resumo.origem && r.resumo.origem !== r.resumo.total) linhas.push(["Total no arquivo do cliente", milhar(r.resumo.origem)]);
  linhas.push(["Período", r.resumo.periodo], ["Leiaute", r.resumo.leiaute], ["Linhas do arquivo", milhar(r.resumo.linhas)]);
  $("#resumo").innerHTML = linhas.map(([a, b]) => `<p><span>${a}</span><span class="carbono">${esc(b)}</span></p>`).join("");

  const avisos = r.gerais.map((g) => g.msg);
  if (r.fora.length) avisos.push(`${r.fora.length} ${r.fora.length > 1 ? "itens ficaram" : "item ficou"} fora do arquivo`);
  $("#gerais").innerHTML = avisos.map((m, n) => {
    const dica = n === avisos.length - 1 && r.fora.length ? ` title="${esc(r.fora.map((f) => `${f.c}: ${f.p.join("; ")}`).join("\n"))}"` : "";
    return `<p${dica}><i class="fa-solid fa-triangle-exclamation"></i><span>${esc(m)}</span></p>`;
  }).join("");

  $("#btnBaixar").hidden = false;
  $("#btnBaixar").href = `/baixar/${encodeURIComponent(r.arquivo)}`;
  $("#btnPend").hidden = !r.pendencias;
  if (r.pendencias) $("#btnPend").href = `/baixar/${encodeURIComponent(r.pendencias)}`;
  desenharArquivos(r.arquivos);
  desenharItens();
}

function limparResultado() {
  estado.res = null;
  $("#resultado").hidden = true;
  $("#btnBaixar").hidden = true;
  $("#erroConv").hidden = true;
  desenharArquivos([]);
}

function novoCliente() {
  Object.assign(estado, { lote: null, arquivos: [], principal: null, res: null, busca: "", filtro: "todos" });
  estado.editados.clear();
  $("#formEmp").reset();
  $("#busca").value = "";
  $$("#segs .seg").forEach((s) => s.classList.toggle("on", s.dataset.f === "todos"));
  limparResultado();
}

$("#segs").addEventListener("click", (e) => {
  const s = e.target.closest(".seg");
  if (!s) return;
  estado.filtro = s.dataset.f;
  estado.limite = POR_PAGINA;
  $$("#segs .seg").forEach((x) => x.classList.toggle("on", x === s));
  desenharItens();
});
$("#busca").addEventListener("input", (e) => { estado.busca = e.target.value; estado.limite = POR_PAGINA; desenharItens(); });
$("#maisItens").addEventListener("click", () => { estado.limite += POR_PAGINA; desenharItens(); });
$("#formEmp").addEventListener("input", (e) => {
  if (!e.target.name) return;
  estado.editados.add(e.target.name);
  e.target.classList.remove("falta");
});
$("#formEmp").addEventListener("submit", (e) => { e.preventDefault(); converter(); });
$("#btnGerar").addEventListener("click", () => converter().then(() => !estado.res?.erro && aviso("SPED gerado de novo")));
$("#btnNovo").addEventListener("click", novoCliente);
$("#btnAdd").addEventListener("click", () => $("#escolher").click());
$("#solta").addEventListener("click", () => $("#escolher").click());
$("#escolher").addEventListener("change", (e) => { enviar(e.target.files); e.target.value = ""; });

let arrasto = 0;
document.addEventListener("dragenter", (e) => { if (e.dataTransfer.types.includes("Files")) { arrasto++; document.body.classList.add("arrastando"); } });
document.addEventListener("dragleave", () => { if (--arrasto <= 0) { arrasto = 0; document.body.classList.remove("arrastando"); } });
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => {
  e.preventDefault();
  arrasto = 0;
  document.body.classList.remove("arrastando");
  if (!$("#aba-converter").hidden && e.dataTransfer.files.length) enviar(e.dataTransfer.files);
});

/* ---------------- arquivos gerados ---------------- */
let gerados = [];
async function atualizarContaGerados() {
  gerados = await api("/api/gerados");
  const c = $("#contGerados");
  c.hidden = !gerados.length;
  c.textContent = gerados.length;
  return gerados;
}

async function carregarGerados() {
  await atualizarContaGerados();
  $("#contaGer").textContent = gerados.length ? `${gerados.length} ${gerados.length > 1 ? "arquivos" : "arquivo"} na pasta saida` : "Nenhum arquivo gerado";
  $("#listaGerados").innerHTML = gerados.length ? gerados.map((g) => `<tr>
      <td class="td canhoto"><span class="carbono">${esc(g.inventario || "—")}</span><small title="${esc(g.arquivo)}">${esc(g.arquivo.replace(/^SPED_/, "").replace(/\.txt$/, ""))}</small></td>
      <td class="td">${esc(g.nome)}</td>
      <td class="td col-larga carbono" style="font-size:15px">${esc(g.cnpj)}</td>
      <td class="td direita carbono">${milhar(g.itens)}</td>
      <td class="td direita carbono">${milhar(g.total)}</td>
      <td class="td col-larga">${esc(g.data)}</td>
      <td class="td"><div class="acoes">
        <a class="btn btn-primary" style="height:40px" href="/baixar/${encodeURIComponent(g.arquivo)}"><i class="fa-solid fa-download"></i>Baixar</a>
        <button class="icon-btn" data-menu="${esc(g.arquivo)}" title="Mais"><i class="fa-solid fa-ellipsis"></i></button>
      </div></td>
    </tr>`).join("")
    : `<tr><td colspan="7" class="vazio-lista"><i class="fa-solid fa-box-open"></i><br>Os arquivos gerados aparecem aqui</td></tr>`;
}

function fecharMenu() { $(".menu-pop")?.remove(); }
document.addEventListener("click", (e) => { if (!e.target.closest(".menu-pop, [data-menu]")) fecharMenu(); });

$("#listaGerados").addEventListener("click", (e) => {
  const b = e.target.closest("[data-menu]");
  if (!b) return;
  fecharMenu();
  const g = gerados.find((x) => x.arquivo === b.dataset.menu);
  const menu = document.createElement("div");
  menu.className = "menu-pop";
  menu.innerHTML = `
    <button class="menu-item" data-acao="ver"><i class="fa-solid fa-eye"></i>Ver arquivo</button>
    ${g.pendencias ? `<a class="menu-item" href="/baixar/${encodeURIComponent(g.pendencias)}"><i class="fa-solid fa-file-csv"></i>Baixar pendências</a>` : ""}
    <button class="menu-item perigo" data-acao="excluir"><i class="fa-solid fa-trash"></i>Excluir</button>`;
  document.body.append(menu);
  const p = b.getBoundingClientRect();
  menu.style.top = `${p.bottom + window.scrollY + 6}px`;
  menu.style.left = `${Math.max(12, p.right + window.scrollX - menu.offsetWidth)}px`;
  menu.addEventListener("click", async (ev) => {
    const acao = ev.target.closest("[data-acao]")?.dataset.acao;
    fecharMenu();
    if (acao === "ver") verArquivo(g.arquivo);
    if (acao === "excluir" && confirm(`Excluir ${g.arquivo}?`)) {
      await api(`/api/gerado/${encodeURIComponent(g.arquivo)}`, { method: "DELETE" });
      aviso("Arquivo excluído");
      carregarGerados();
    }
  });
});

let verAtual = { nome: "", de: 0 };
async function verArquivo(nome, de = 0) {
  const r = await api(`/api/ver/${encodeURIComponent(nome)}?de=${de}`);
  if (r.erro) return aviso(r.erro, "erro");
  verAtual = { nome, de: de + r.linhas.length };
  $("#verTit").textContent = nome;
  const html = r.linhas.map((l, n) => `<span>${de + n + 1}</span>${esc(l)}`).join("\n");
  if (de === 0) $("#verTxt").innerHTML = html; else $("#verTxt").insertAdjacentHTML("beforeend", "\n" + html);
  const resto = r.total - verAtual.de;
  $("#verMais").hidden = resto <= 0;
  $("#verMais").textContent = `Mostrar mais linhas (faltam ${milhar(resto)})`;
  $("#modalVer").hidden = false;
}
$("#verMais").addEventListener("click", () => verArquivo(verAtual.nome, verAtual.de));
$("#modalVer").addEventListener("click", (e) => { if (e.target.id === "modalVer" || e.target.closest("[data-fechar]")) $("#modalVer").hidden = true; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#modalVer").hidden = true; fecharMenu(); } });

$("#btnPasta").addEventListener("click", async () => {
  const r = await api("/api/abrir-pasta", { method: "POST" });
  if (r.erro) aviso(r.erro, "erro");
});

/* ---------------- escritório ---------------- */
async function carregarEscritorio() {
  const d = await api("/api/escritorio");
  const f = $("#formEsc");
  [...f.elements].forEach((el) => { if (el.name) el.value = d[el.name] || ""; });
}
$("#formEsc").addEventListener("submit", async (e) => {
  e.preventDefault();
  const dados = Object.fromEntries(new FormData(e.target));
  const r = await api("/api/escritorio", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(dados) });
  if (r.erro) return aviso(r.erro, "erro");
  aviso("Dados do escritório salvos");
  if (estado.arquivos.length) converter();
});

atualizarContaGerados();
