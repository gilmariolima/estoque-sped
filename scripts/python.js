'use strict'

// Roda o mesmo Python que o app usa (o embutido, se já preparado). Ex.: os testes do conversor:
//   node scripts/python.js -m unittest discover -s tests

const { spawnSync } = require('node:child_process')
const path = require('node:path')
const { caminhoPython } = require('../electron/motor')

const motor = path.resolve(__dirname, '..', 'app')
const r = spawnSync(caminhoPython({ motor }), process.argv.slice(2), { cwd: motor, stdio: 'inherit' })
process.exit(r.status ?? 1)
