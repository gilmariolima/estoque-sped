'use strict'

// Monta app\python: o Python "embeddable" oficial para Windows 64 bits + openpyxl, et_xmlfile e
// xlrd. É o que vai dentro do instalador; o computador do escritório não precisa ter Python.
//
//   node scripts/preparar-python.js              baixa de python.org e pypi.org (precisa de internet)
//   node scripts/preparar-python.js --zip <arq>  usa um python-3.12.x-embed-amd64.zip já baixado
//   node scripts/preparar-python.js --refazer    apaga app\python e monta de novo

const crypto = require('node:crypto')
const fs = require('node:fs')
const path = require('node:path')
const AdmZip = require('adm-zip')

const PYTHON = '3.12.10'
const PACOTES = [ // as mesmas versões da instalação antiga (app/requirements.txt)
  ['openpyxl', '3.1.5'],
  ['et_xmlfile', '2.0.0'],
  ['xlrd', '2.0.1'],
]
const DESTINO = path.resolve(__dirname, '..', 'app', 'python')
const SITE = path.join(DESTINO, 'Lib', 'site-packages')

const args = process.argv.slice(2)
const opcao = (nome) => {
  const i = args.indexOf(nome)
  return i >= 0 ? args[i + 1] : undefined
}

async function baixar(url) {
  const r = await fetch(url, { signal: AbortSignal.timeout(120e3) })
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`)
  return Buffer.from(await r.arrayBuffer())
}

function pronto() {
  return fs.existsSync(path.join(DESTINO, 'python.exe'))
    && PACOTES.every(([nome, versao]) => fs.existsSync(path.join(SITE, `${nome}-${versao}.dist-info`)))
}

async function pythonEmbutido() {
  const zipLocal = opcao('--zip')
  if (zipLocal) {
    console.log(`Python: ${zipLocal}`)
    return fs.readFileSync(zipLocal)
  }
  const url = `https://www.python.org/ftp/python/${PYTHON}/python-${PYTHON}-embed-amd64.zip`
  console.log(`Baixando ${url}`)
  return baixar(url)
}

/** Roda do jeito que o painel espera: sem o ._pth certo o Python não acha os pacotes nem o gerar_sped.py. */
function escreverPth() {
  const pth = fs.readdirSync(DESTINO).find((n) => /^python\d+\._pth$/.test(n))
  if (!pth) throw new Error('O zip não parece ser o Python embeddable (falta o arquivo python3xx._pth).')
  const zipStd = pth.replace('._pth', '.zip')
  // ".." é a pasta app: é por ela que o painel.py importa o gerar_sped.py
  fs.writeFileSync(path.join(DESTINO, pth), [zipStd, '.', 'Lib\\site-packages', '..', 'import site', ''].join('\r\n'))
}

async function pacote(nome, versao) {
  const info = JSON.parse((await baixar(`https://pypi.org/pypi/${nome}/${versao}/json`)).toString())
  const roda = info.urls.find((u) => u.packagetype === 'bdist_wheel' && /-(py2\.)?py3-none-any\.whl$/.test(u.filename))
  if (!roda) throw new Error(`${nome} ${versao}: não há wheel "py3-none-any" no PyPI`)
  console.log(`Baixando ${roda.filename}`)
  const dados = await baixar(roda.url)
  const hash = crypto.createHash('sha256').update(dados).digest('hex')
  if (hash !== roda.digests.sha256) throw new Error(`${roda.filename}: o arquivo baixado não confere com o PyPI (sha256)`)
  new AdmZip(dados).extractAllTo(SITE, true)
}

async function main() {
  if (args.includes('--refazer')) fs.rmSync(DESTINO, { recursive: true, force: true })
  if (pronto()) {
    console.log(`app\\python já está pronto (Python ${PYTHON}). Use --refazer para montar de novo.`)
    return
  }
  fs.rmSync(DESTINO, { recursive: true, force: true })
  fs.mkdirSync(SITE, { recursive: true })
  new AdmZip(await pythonEmbutido()).extractAllTo(DESTINO, true)
  escreverPth()
  for (const [nome, versao] of PACOTES) await pacote(nome, versao)
  console.log(`Pronto: ${DESTINO}`)
}

main().catch((e) => {
  console.error(`Não foi possível preparar o Python: ${e.message}`)
  process.exit(1)
})
