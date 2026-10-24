'use strict'

// Pasta de dados do app desktop e migração da instalação antiga (zip + Instalar.cmd + janela do Edge).
// Fica separado do main.js para os testes rodarem sem o Electron.

const fs = require('node:fs')
const path = require('node:path')

const NOME = 'Estoque SPED'

/**
 * Onde ficam empresas.json, escritorio.json, saida/ (os SPEDs gerados), entradas/ e o log.
 * Fica FORA da pasta do programa (%LOCALAPPDATA%\Programs\Estoque SPED): o instalador apaga a
 * pasta do programa inteira ao atualizar ou desinstalar, e os dados nunca podem ir junto.
 */
function pastaDados({ env = process.env, localAppData = env.LOCALAPPDATA } = {}) {
  const definida = (env.ESTOQUE_DADOS || '').trim()
  if (definida) return path.resolve(definida)
  if (!localAppData) throw new Error('LOCALAPPDATA não definido: defina ESTOQUE_DADOS com a pasta de dados.')
  return path.join(localAppData, NOME, 'dados')
}

/** Pasta da versão antiga, instalada pelo Instalar.cmd (só conta se o lançador dela estiver lá). */
function pastaVersaoAntiga({ localAppData = process.env.LOCALAPPDATA } = {}) {
  if (!localAppData) return null
  const antiga = path.join(localAppData, 'Programs', NOME)
  return fs.existsSync(path.join(antiga, `${NOME}.cmd`)) ? antiga : null
}

const temDados = (pasta) => fs.existsSync(pasta) && fs.readdirSync(pasta).some((n) => n !== 'painel.log')

/**
 * Traz os dados da instalação antiga (<antiga>\dados) para a pasta nova.
 * Só age se a antiga tem dados e a nova ainda não tem. Tenta mover (instantâneo, mesmo disco);
 * se não der (arquivo preso), copia e deixa a antiga onde está.
 * @returns {null | { de, para, modo: 'movido' | 'copiado' }}
 */
function migrarDadosAntigos({ antiga, nova }) {
  if (!antiga) return null
  const origem = path.join(antiga, 'dados')
  if (!temDados(origem) || temDados(nova)) return null

  fs.mkdirSync(path.dirname(nova), { recursive: true })
  if (fs.existsSync(nova)) fs.rmSync(nova, { recursive: true, force: true }) // só tinha o log
  try {
    fs.renameSync(origem, nova)
    return { de: origem, para: nova, modo: 'movido' }
  } catch {
    fs.cpSync(origem, nova, { recursive: true, force: false, errorOnExist: false })
    return { de: origem, para: nova, modo: 'copiado' }
  }
}

/**
 * Apaga o que a versão antiga deixou na pasta dela (o lançador, o LEIA-ME e a pasta app com o
 * Python). Os atalhos "Estoque SPED" da Área de Trabalho e do Menu Iniciar têm o mesmo nome dos
 * atalhos do instalador novo, que já os substitui.
 */
function limparVersaoAntiga(antiga) {
  if (!antiga) return []
  const alvos = [`${NOME}.cmd`, 'LEIA-ME.txt', 'app']
  const apagados = []
  for (const nome of alvos) {
    const alvo = path.join(antiga, nome)
    // A pasta app só é da versão antiga se tiver o painel.py dela
    if (nome === 'app' && !fs.existsSync(path.join(alvo, 'painel.py'))) continue
    try {
      if (fs.existsSync(alvo)) {
        fs.rmSync(alvo, { recursive: true, force: true })
        apagados.push(nome)
      }
    } catch {
      // arquivo preso: fica para a próxima
    }
  }
  return apagados
}

module.exports = { NOME, pastaDados, pastaVersaoAntiga, migrarDadosAntigos, limparVersaoAntiga }
