'use strict'

// Estoque SPED como app desktop (Electron): o painel em Python (o "motor") roda escondido numa
// porta livre e a página dele abre numa janela própria, com ícone na barra de tarefas.
// Fechar a janela fecha o app (não há nada rodando em segundo plano, ao contrário do SmartDoc).

const { app, BrowserWindow, Menu, dialog, shell } = require('electron')
const { execFileSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')
const { NOME, pastaDados, pastaVersaoAntiga, migrarDadosAntigos, limparVersaoAntiga } = require('./dados')
const { pastaMotor, caminhoPython, iniciarMotor } = require('./motor')

const APP_ID = 'com.gilmariolima.estoquesped'
const RAIZ = path.resolve(__dirname, '..')
const NO_WINDOWS = process.platform === 'win32'

let motor = null
let janela = null
let pasta = null
let saindo = false
let encerrado = false
let prefs = {}

// ---------- preferências da janela ----------

const arquivoPrefs = () => path.join(app.getPath('userData'), 'preferencias.json')
function lerPrefs() {
  try {
    return JSON.parse(fs.readFileSync(arquivoPrefs(), 'utf8'))
  } catch {
    return {}
  }
}
function salvarPrefs(mudancas) {
  prefs = { ...prefs, ...mudancas }
  try {
    fs.mkdirSync(path.dirname(arquivoPrefs()), { recursive: true })
    fs.writeFileSync(arquivoPrefs(), JSON.stringify(prefs, null, 2))
  } catch {
    // preferência perdida não impede o app de funcionar
  }
}

// ---------- ícone ----------

function caminhoIcone() {
  const app_ = path.join(RAIZ, 'app')
  const ico = fs.existsSync(app_) && fs.readdirSync(app_).find((n) => /^icone-.*\.ico$/.test(n))
  if (NO_WINDOWS && ico) return path.join(app_, ico)
  return path.join(pastaMotor({ empacotado: app.isPackaged, recursos: process.resourcesPath, raiz: RAIZ }), 'painel', 'icone.png')
}

// ---------- versão antiga (zip + Instalar.cmd + janela do Edge) ----------

/**
 * Desliga o painel da versão antiga, traz os dados dela e apaga o que ela deixou.
 * Sem versão antiga, não faz nada.
 */
async function aposentarVersaoAntiga(novaPasta) {
  if (!NO_WINDOWS || process.env.ESTOQUE_DADOS) return null
  const antiga = pastaVersaoAntiga()
  if (!antiga) return null

  // A versão antiga sempre usou a porta 8765 e aceita o pedido de encerrar do instalador.
  try {
    await fetch('http://127.0.0.1:8765/api/encerrar', { method: 'POST', signal: AbortSignal.timeout(2000) })
    await new Promise((r) => setTimeout(r, 800))
  } catch {
    // não estava aberta
  }
  const pythonAntigo = path.join(antiga, 'app', 'python', 'pythonw.exe').replace(/'/g, "''")
  try {
    execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
      `$n = '${pythonAntigo}'; Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.ExecutablePath -eq $n } | ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }`],
    { timeout: 30000, windowsHide: true, stdio: 'ignore' })
  } catch {
    // se não conseguir, a migração copia em vez de mover
  }

  const migracao = migrarDadosAntigos({ antiga, nova: novaPasta })
  // Só apaga a versão antiga depois que os dados dela estão a salvo na pasta nova
  if (migracao || !fs.existsSync(path.join(antiga, 'dados'))) limparVersaoAntiga(antiga)
  return migracao
}

// ---------- janela ----------

function abrirJanela() {
  if (janela) {
    if (janela.isMinimized()) janela.restore()
    janela.show()
    janela.focus()
    return
  }
  janela = new BrowserWindow({
    title: NOME,
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    show: false,
    icon: caminhoIcone(),
    autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false, spellcheck: false },
  })
  if (prefs.maximizada) janela.maximize()

  // Links para fora do painel abrem no navegador padrão.
  const doPainel = (url) => url === motor.url || url.startsWith(motor.url + '/')
  janela.webContents.setWindowOpenHandler(({ url }) => {
    if (doPainel(url)) return { action: 'allow' }
    if (/^https?:\/\//.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })
  janela.webContents.on('will-navigate', (e, url) => {
    if (doPainel(url)) return
    e.preventDefault()
    if (/^https?:\/\//.test(url)) shell.openExternal(url)
  })

  // O painel não usa câmera, microfone, notificações etc.: nada é concedido
  janela.webContents.session.setPermissionRequestHandler((_wc, _permissao, responder) => responder(false))
  janela.webContents.session.setPermissionCheckHandler(() => false)

  // "Baixar SPED": pergunta onde salvar, já sugerindo a pasta Downloads e o nome do arquivo.
  janela.webContents.session.on('will-download', (_e, item) => {
    item.setSaveDialogOptions({
      title: 'Salvar arquivo',
      defaultPath: path.join(app.getPath('downloads'), item.getFilename()),
    })
  })

  janela.once('ready-to-show', () => janela.show())
  janela.on('maximize', () => salvarPrefs({ maximizada: true }))
  janela.on('unmaximize', () => salvarPrefs({ maximizada: false }))
  janela.on('closed', () => {
    janela = null
  })

  janela.loadURL(motor.entrada)
}

// ---------- erros ----------

async function falha(titulo, erro) {
  const botoes = ['Abrir pasta de dados', 'Fechar']
  const { response } = await dialog.showMessageBox({
    type: 'error',
    title: NOME,
    message: titulo,
    detail: `${erro.message}\n\nPasta de dados: ${pasta}`,
    buttons: botoes,
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  })
  if (response === 0 && pasta) await shell.openPath(pasta)
  encerrado = true
  app.exit(1)
}

// ---------- ciclo de vida ----------

async function iniciar() {
  app.setAppUserModelId(APP_ID)
  Menu.setApplicationMenu(null)
  prefs = lerPrefs()

  try {
    pasta = pastaDados()
    await aposentarVersaoAntiga(pasta)

    const dirMotor = pastaMotor({ empacotado: app.isPackaged, recursos: process.resourcesPath, raiz: RAIZ })
    motor = await iniciarMotor({
      python: caminhoPython({ motor: dirMotor }),
      motor: dirMotor,
      dados: pasta,
      log: path.join(pasta, 'painel.log'),
    })
    // O motor caiu com o app aberto (não foi o app que mandou parar)
    motor.processo.once('exit', (codigo) => {
      if (!saindo) falha('O Estoque SPED parou de funcionar.', new Error(`O motor fechou sozinho (código ${codigo}). Veja o painel.log na pasta de dados.`))
    })
    abrirJanela()
  } catch (e) {
    await falha('O Estoque SPED não conseguiu iniciar.', e)
  }
}

if (!app.requestSingleInstanceLock()) {
  // Já está aberto: o outro processo mostra a janela (evento second-instance).
  app.quit()
} else {
  app.on('second-instance', () => {
    if (motor) abrirJanela()
  })

  app.on('window-all-closed', () => app.quit())

  app.on('before-quit', (e) => {
    saindo = true
    if (encerrado) return
    e.preventDefault()
    // Motor parado: sai direto (um segundo app.quit() às vezes não termina o processo).
    Promise.resolve(motor?.parar()).catch(() => {}).finally(() => {
      encerrado = true
      app.exit(0)
    })
  })

  app.whenReady().then(iniciar)
}
