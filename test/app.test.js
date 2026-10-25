'use strict'

const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { test } = require('node:test')
const { pastaDados, pastaVersaoAntiga, migrarDadosAntigos, limparVersaoAntiga } = require('../electron/dados')
const { pastaMotor, caminhoPython, iniciarMotor } = require('../electron/motor')
const { criarAtualizacao } = require('../electron/atualizar')

const RAIZ = path.resolve(__dirname, '..')
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'estoque-app-'))

/** Monta uma instalação da versão antiga (zip + Instalar.cmd). */
function versaoAntiga(local, { comDados = true } = {}) {
  const antiga = path.join(local, 'Programs', 'Estoque SPED')
  fs.mkdirSync(path.join(antiga, 'app', 'python'), { recursive: true })
  fs.writeFileSync(path.join(antiga, 'app', 'painel.py'), '')
  fs.writeFileSync(path.join(antiga, 'Estoque SPED.cmd'), '')
  fs.writeFileSync(path.join(antiga, 'LEIA-ME.txt'), '')
  if (comDados) {
    fs.mkdirSync(path.join(antiga, 'dados', 'saida'), { recursive: true })
    fs.writeFileSync(path.join(antiga, 'dados', 'empresas.json'), '{"11222333000181":{"uf":"CE"}}')
    fs.writeFileSync(path.join(antiga, 'dados', 'saida', 'SPED_1.txt'), '|0000|')
  }
  return antiga
}

test('pasta de dados fica fora da pasta do programa', () => {
  assert.equal(pastaDados({ env: {}, localAppData: 'C:\\L' }), path.join('C:\\L', 'Estoque SPED', 'dados'))
  assert.equal(pastaDados({ env: { ESTOQUE_DADOS: '/x/dados' } }), path.resolve('/x/dados'))
  assert.throws(() => pastaDados({ env: {} }), /LOCALAPPDATA/)
})

test('acha a versão antiga só se o lançador dela existir', () => {
  const local = tmp()
  assert.equal(pastaVersaoAntiga({ localAppData: local }), null)
  const antiga = versaoAntiga(local)
  assert.equal(pastaVersaoAntiga({ localAppData: local }), antiga)
})

test('traz os dados da versão antiga e depois limpa o que ela deixou', () => {
  const local = tmp()
  const antiga = versaoAntiga(local)
  const nova = path.join(local, 'Estoque SPED', 'dados')
  const r = migrarDadosAntigos({ antiga, nova })
  assert.equal(r.modo, 'movido')
  assert.equal(fs.readFileSync(path.join(nova, 'saida', 'SPED_1.txt'), 'utf8'), '|0000|')
  assert.ok(fs.existsSync(path.join(nova, 'empresas.json')))
  assert.deepEqual(limparVersaoAntiga(antiga).sort(), ['Estoque SPED.cmd', 'LEIA-ME.txt', 'app'])
  assert.equal(pastaVersaoAntiga({ localAppData: local }), null)
})

test('nunca sobrescreve dados que já estão na pasta nova', () => {
  const local = tmp()
  const antiga = versaoAntiga(local)
  const nova = path.join(local, 'Estoque SPED', 'dados')
  fs.mkdirSync(nova, { recursive: true })
  fs.writeFileSync(path.join(nova, 'escritorio.json'), '{"nome":"NOVO"}')
  assert.equal(migrarDadosAntigos({ antiga, nova }), null)
  assert.equal(fs.readFileSync(path.join(nova, 'escritorio.json'), 'utf8'), '{"nome":"NOVO"}')
  assert.ok(fs.existsSync(path.join(antiga, 'dados', 'empresas.json')))
})

test('pasta nova só com o log não impede a migração', () => {
  const local = tmp()
  const antiga = versaoAntiga(local)
  const nova = path.join(local, 'Estoque SPED', 'dados')
  fs.mkdirSync(nova, { recursive: true })
  fs.writeFileSync(path.join(nova, 'painel.log'), 'x')
  assert.equal(migrarDadosAntigos({ antiga, nova }).modo, 'movido')
})

test('arquivo preso: copia os dados em vez de mover', () => {
  const local = tmp()
  const antiga = versaoAntiga(local)
  const nova = path.join(local, 'Estoque SPED', 'dados')
  fs.mkdirSync(`${nova}.migrando`, { recursive: true }) // restos de uma tentativa anterior
  fs.writeFileSync(path.join(`${nova}.migrando`, 'lixo.txt'), 'x')
  const preso = () => { throw Object.assign(new Error('EBUSY'), { code: 'EBUSY' }) }
  const r = migrarDadosAntigos({ antiga, nova, mover: preso })
  assert.equal(r.modo, 'copiado')
  assert.equal(fs.readFileSync(path.join(nova, 'empresas.json'), 'utf8'), '{"11222333000181":{"uf":"CE"}}')
  assert.ok(!fs.existsSync(path.join(nova, 'lixo.txt')))
  assert.ok(!fs.existsSync(`${nova}.migrando`))
  assert.ok(fs.existsSync(path.join(antiga, 'dados', 'empresas.json'))) // a cópia não apaga a origem
})

test('cópia interrompida não deixa a pasta nova pela metade e a próxima abertura migra', () => {
  const local = tmp()
  const antiga = versaoAntiga(local)
  const nova = path.join(local, 'Estoque SPED', 'dados')
  const preso = () => { throw new Error('EBUSY') }
  const quebra = (_de, para) => {
    fs.mkdirSync(para, { recursive: true })
    fs.writeFileSync(path.join(para, 'empresas.json'), '{') // metade do arquivo
    throw new Error('ENOSPC')
  }
  assert.throws(() => migrarDadosAntigos({ antiga, nova, mover: preso, copiar: quebra }), /ENOSPC/)
  assert.ok(!fs.existsSync(nova))
  assert.ok(!fs.existsSync(`${nova}.migrando`))
  assert.equal(migrarDadosAntigos({ antiga, nova }).modo, 'movido')
  assert.ok(fs.existsSync(path.join(nova, 'saida', 'SPED_1.txt')))
})

test('não apaga uma pasta app que não é da versão antiga', () => {
  const local = tmp()
  const antiga = versaoAntiga(local, { comDados: false })
  fs.rmSync(path.join(antiga, 'app', 'painel.py'))
  assert.deepEqual(limparVersaoAntiga(antiga).sort(), ['Estoque SPED.cmd', 'LEIA-ME.txt'])
  assert.ok(fs.existsSync(path.join(antiga, 'app')))
})

test('motor: liga numa porta livre, responde e desliga', async () => {
  const motor = pastaMotor({ empacotado: false, raiz: RAIZ })
  assert.equal(pastaMotor({ empacotado: true, recursos: '/r' }), path.join('/r', 'estoque'))
  const dados = tmp()
  const m = await iniciarMotor({ python: caminhoPython({ motor }), motor, dados, log: path.join(dados, 'painel.log') })
  try {
    assert.match(m.url, /^http:\/\/127\.0\.0\.1:\d+$/)
    assert.equal(m.entrada, `${m.url}/?t=${m.token}`)
    const r = await fetch(`${m.url}/api/gerados`, { headers: { 'X-Estoque-Token': m.token } })
    assert.deepEqual(await r.json(), [])
    // sem o token desta execução (outro programa, outro usuário do computador) é recusado
    assert.equal((await fetch(`${m.url}/api/gerados`)).status, 401)
    assert.equal((await fetch(`${m.url}/api/gerados`, { headers: { 'X-Estoque-Token': 'outro' } })).status, 401)
    // a janela abre /?t=<token>: o painel guarda o token num cookie só dele e tira do endereço
    const entrada = await fetch(m.entrada, { redirect: 'manual' })
    assert.equal(entrada.status, 303)
    assert.equal(entrada.headers.get('location'), '/')
    const cookie = entrada.headers.get('set-cookie')
    assert.match(cookie, /HttpOnly/)
    assert.match(cookie, /SameSite=Strict/)
    const pagina = await fetch(`${m.url}/`, { headers: { Cookie: cookie.split(';')[0] } })
    assert.equal(pagina.status, 200)
    assert.match(pagina.headers.get('content-security-policy'), /default-src 'self'/)
    // pedido de outro site é recusado
    const fora = await fetch(`${m.url}/api/encerrar`, { method: 'POST', headers: { Origin: 'https://site-qualquer.com', 'X-Estoque-Token': m.token } })
    assert.equal(fora.status, 403)
  } finally {
    await m.parar()
  }
  assert.notEqual(m.processo.exitCode ?? m.processo.signalCode, null)
})

test('motor: falha ao ligar explica o motivo', async () => {
  const motor = tmp() // sem painel.py
  await assert.rejects(
    iniciarMotor({ python: caminhoPython({ motor: pastaMotor({ empacotado: false, raiz: RAIZ }) }), motor, dados: tmp(), espera: 10e3 }),
    /fechou ao ligar[\s\S]*painel\.py/,
  )
})

test('painel: número montado em HTML passa pelo esc() (total de um SPED mexido à mão não vira script)', () => {
  const js = fs.readFileSync(path.join(RAIZ, 'app', 'painel', 'app.js'), 'utf8')
  const semEsc = js.split('\n').filter((l) => /<(td|p|span)\b/.test(l) && /\$\{milhar\(/.test(l))
  assert.deepEqual(semEsc, [])
})

/** autoUpdater de mentira: guarda os ouvintes e as chamadas. */
function atualizadorFalso({ falhar = false } = {}) {
  const ouvintes = {}
  return {
    ouvintes,
    instalou: null,
    procurou: 0,
    on(evento, fn) { ouvintes[evento] = fn },
    async checkForUpdates() {
      this.procurou++
      if (falhar) throw new Error('sem internet')
    },
    quitAndInstall(silencioso, reabrir) { this.instalou = { silencioso, reabrir } },
  }
}

test('atualização: sem versão baixada, fechar o app não instala nada', async () => {
  const atualizador = atualizadorFalso()
  const a = criarAtualizacao({ atualizador, perguntar: async () => ({ response: 1 }), sair: () => {} })
  await a.procurar()
  assert.equal(atualizador.procurou, 1)
  assert.equal(atualizador.autoInstallOnAppQuit, false)
  assert.equal(a.instalarAoSair(), false)
  assert.equal(atualizador.instalou, null)
})

test('atualização: "Reiniciar agora" fecha o app e instala reabrindo', async () => {
  const atualizador = atualizadorFalso()
  let saiu = 0
  let pergunta
  const a = criarAtualizacao({ atualizador, perguntar: async (o) => { pergunta = o; return { response: 0 } }, sair: () => saiu++ })
  await atualizador.ouvintes['update-downloaded']({ version: '1.2.0' })
  assert.match(pergunta.message, /1\.2\.0/)
  assert.equal(saiu, 1)
  assert.equal(a.instalarAoSair(), true)
  assert.deepEqual(atualizador.instalou, { silencioso: true, reabrir: true })
})

test('atualização: "Depois" instala só ao fechar, sem reabrir', async () => {
  const atualizador = atualizadorFalso()
  let saiu = 0
  const a = criarAtualizacao({ atualizador, perguntar: async () => ({ response: 1 }), sair: () => saiu++ })
  await atualizador.ouvintes['update-downloaded']({ version: '1.2.0' })
  assert.equal(saiu, 0)
  assert.equal(a.instalarAoSair(), true)
  assert.deepEqual(atualizador.instalou, { silencioso: true, reabrir: false })
})

test('atualização: sem internet, só anota no log', async () => {
  const log = path.join(tmp(), 'atualizacao.log')
  const atualizador = atualizadorFalso({ falhar: true })
  const a = criarAtualizacao({ atualizador, perguntar: async () => ({ response: 1 }), sair: () => {}, log })
  await a.procurar()
  assert.match(fs.readFileSync(log, 'utf8'), /erro .*sem internet/)
  assert.equal(a.instalarAoSair(), false)
})

test('publicação: o pacote publica no GitHub deste repositório e leva o electron-updater', () => {
  const pacote = require('../package.json')
  assert.deepEqual(pacote.build.publish, [{ provider: 'github', owner: 'gilmariolima', repo: 'estoque-sped', releaseType: 'release' }])
  assert.ok(pacote.dependencies['electron-updater'], 'electron-updater tem de ir em dependencies, não em devDependencies')
  assert.match(pacote.scripts.pacote, /--publish never/)
})
