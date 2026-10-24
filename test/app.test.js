'use strict'

const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { test } = require('node:test')
const { pastaDados, pastaVersaoAntiga, migrarDadosAntigos, limparVersaoAntiga } = require('../electron/dados')
const { pastaMotor, caminhoPython, iniciarMotor } = require('../electron/motor')

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
    const r = await fetch(`${m.url}/api/gerados`)
    assert.deepEqual(await r.json(), [])
    // pedido de outro site é recusado
    const fora = await fetch(`${m.url}/api/encerrar`, { method: 'POST', headers: { Origin: 'https://site-qualquer.com' } })
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
