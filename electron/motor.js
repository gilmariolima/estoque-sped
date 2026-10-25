'use strict'

// O "motor" é o painel em Python (app/painel.py). O app abre o painel numa porta livre e mostra
// a página dele na janela. Fica separado do main.js para os testes rodarem sem o Electron.

const { spawn, spawnSync } = require('node:child_process')
const crypto = require('node:crypto')
const fs = require('node:fs')
const path = require('node:path')

/**
 * Pasta com painel.py, gerar_sped.py e o Python embutido.
 * No app instalado fica em resources\estoque; rodando do código, é a pasta app do repositório.
 */
function pastaMotor({ empacotado, recursos, raiz }) {
  return empacotado ? path.join(recursos, 'estoque') : path.join(raiz, 'app')
}

/**
 * Qual Python usar: o embutido (app\python\python.exe, preparado por `npm run preparar-python`);
 * sem ele (desenvolvimento fora do Windows), ESTOQUE_PYTHON ou o python do sistema.
 * python.exe e não pythonw.exe: o app precisa ler a linha PORTA=; a janela de console não
 * aparece porque o processo é aberto com windowsHide.
 */
function caminhoPython({ motor, plataforma = process.platform, env = process.env }) {
  const embutido = path.join(motor, 'python', plataforma === 'win32' ? 'python.exe' : 'bin/python3')
  if (fs.existsSync(embutido)) return embutido
  if (env.ESTOQUE_PYTHON) return env.ESTOQUE_PYTHON
  for (const nome of plataforma === 'win32' ? ['python', 'py'] : ['python3', 'python']) {
    const r = spawnSync(nome, ['--version'], { windowsHide: true, encoding: 'utf8' })
    if (r.status === 0) return nome
  }
  throw new Error(`Python não encontrado em ${embutido}. Rode "npm run preparar-python" antes de abrir o app.`)
}

/**
 * Liga o painel e espera a linha "PORTA=<n>".
 * O painel só atende quem tem o token desta execução (outro programa ou outro usuário do mesmo
 * computador não consegue usar a porta): vai pelo ambiente, que só o próprio usuário enxerga.
 * A janela abre `entrada` (/?t=<token>), e o painel guarda o token num cookie.
 * @returns {Promise<{ porta: number, url: string, entrada: string, token: string, processo: import('node:child_process').ChildProcess, parar: () => Promise<void> }>}
 */
function iniciarMotor({ python, motor, dados, log, espera = 30e3 }) {
  fs.mkdirSync(dados, { recursive: true })
  const token = crypto.randomBytes(32).toString('base64url')
  const saidaLog = log ? fs.createWriteStream(log, { flags: 'a' }) : null
  const processo = spawn(python, [path.join(motor, 'painel.py'), '--porta', '0', '--sem-navegador'], {
    cwd: motor,
    env: { ...process.env, ESTOQUE_DADOS: dados, ESTOQUE_TOKEN: token, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  let ultimos = '' // fim do stderr, para explicar uma falha ao ligar
  processo.stderr.on('data', (b) => {
    ultimos = (ultimos + b.toString()).slice(-4000)
    saidaLog?.write(b)
  })

  let porta = null
  const vivo = () => processo.exitCode === null && processo.signalCode === null
  const esperarSaida = (ms) => Promise.race([
    new Promise((r) => (vivo() ? processo.once('exit', r) : r())),
    new Promise((r) => setTimeout(r, ms).unref()),
  ])
  // Primeiro pede para o painel encerrar (não corta um SPED no meio da gravação); depois mata.
  const parar = async () => {
    if (vivo() && porta) {
      try {
        await fetch(`http://127.0.0.1:${porta}/api/encerrar`, {
          method: 'POST', headers: { 'X-Estoque-Token': token }, signal: AbortSignal.timeout(2000),
        })
        await esperarSaida(3000)
      } catch {
        // painel travado: vai no kill
      }
    }
    if (vivo()) {
      processo.kill()
      await esperarSaida(5000)
    }
    saidaLog?.end()
  }

  return new Promise((resolve, reject) => {
    let buffer = ''
    let pronto = false
    const falhar = (msg) => {
      if (pronto) return
      pronto = true
      clearTimeout(relogio)
      parar().finally(() => reject(new Error(msg + (ultimos.trim() ? `\n\n${ultimos.trim()}` : ''))))
    }
    const relogio = setTimeout(() => falhar('O motor do Estoque SPED demorou demais para ligar.'), espera)

    processo.stdout.on('data', (b) => {
      buffer += b.toString()
      const m = /^PORTA=(\d+)\s*$/m.exec(buffer)
      if (m && !pronto) {
        pronto = true
        clearTimeout(relogio)
        porta = Number(m[1])
        const url = `http://127.0.0.1:${porta}`
        resolve({ porta, url, entrada: `${url}/?t=${token}`, token, processo, parar })
      }
    })
    processo.once('error', (e) => falhar(`Não foi possível abrir o Python (${python}): ${e.message}`))
    processo.once('exit', (codigo) => falhar(`O motor do Estoque SPED fechou ao ligar (código ${codigo}).`))
  })
}

module.exports = { pastaMotor, caminhoPython, iniciarMotor }
