'use strict'

// Atualização automática: ao abrir, o app procura uma versão nova em Releases do GitHub
// (o latest.yml que o workflow "Publicar versão" publica) e baixa em segundo plano. Baixada,
// pergunta se reinicia agora; se a pessoa deixar para depois, instala quando o app fechar.
// Sem internet ou sem versão nova, não aparece nada.

const fs = require('node:fs')

/**
 * @param {object} o
 * @param {import('electron-updater').AppUpdater} o.atualizador  o autoUpdater do electron-updater
 * @param {(opcoes: object) => Promise<{ response: number }>} o.perguntar  dialog.showMessageBox já com a janela
 * @param {() => void} o.sair  fecha o app (app.quit), que para o motor e chama instalarAoSair
 * @param {string} [o.log]  arquivo onde anotar o que aconteceu
 */
function criarAtualizacao({ atualizador, perguntar, sair, log }) {
  let baixada = null // versão pronta para instalar
  let reabrir = false

  const anotar = (nivel, msg) => {
    if (!log) return
    try {
      fs.appendFileSync(log, `${new Date().toISOString()} ${nivel} ${msg instanceof Error ? msg.stack || msg.message : msg}\n`)
    } catch {
      // sem log não impede nada
    }
  }
  atualizador.logger = {
    info: (m) => anotar('info', m),
    warn: (m) => anotar('aviso', m),
    error: (m) => anotar('erro', m),
    debug: () => {},
  }
  atualizador.autoDownload = true
  // O app para o motor antes de sair (before-quit) e sai com app.exit, que não dispara o
  // "quit" de que o autoInstallOnAppQuit depende: quem instala ao sair é instalarAoSair
  atualizador.autoInstallOnAppQuit = false

  atualizador.on('error', (e) => anotar('erro', e))
  atualizador.on('update-downloaded', async (info) => {
    baixada = info.version
    const { response } = await perguntar({
      type: 'info',
      title: 'Estoque SPED',
      message: `A versão ${info.version} do Estoque SPED está pronta para instalar.`,
      detail: 'Reinicie agora para usar a versão nova. Se preferir continuar, ela é instalada quando você fechar o programa. '
        + 'Os cadastros e os arquivos gerados continuam.',
      buttons: ['Reiniciar agora', 'Depois'],
      defaultId: 0,
      cancelId: 1,
      noLink: true,
    })
    if (response === 0) {
      reabrir = true
      sair()
    }
  })

  return {
    procurar() {
      return atualizador.checkForUpdates().catch((e) => anotar('erro', e))
    },
    /** Chamado no fim da saída, com o motor já parado. Devolve true se o instalador assumiu. */
    instalarAoSair() {
      if (!baixada) return false
      anotar('info', `instalando a versão ${baixada}${reabrir ? ' e reabrindo' : ''}`)
      // silencioso (o instalador não faz perguntas); reabre só se a pessoa pediu "Reiniciar agora"
      atualizador.quitAndInstall(true, reabrir)
      return true
    },
  }
}

module.exports = { criarAtualizacao }
