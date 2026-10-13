/**
 * Tag checkouts make electron-builder implicitly publish to GitHub unless
 * --publish never is passed. Run with: node --test electron/lib/packaging.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert/strict')
const pkg = require('../../package.json')

const installerScripts = [
    'dist',
    'dist:all',
    'dist:win',
    'dist:mac',
    'dist:mac:arm64',
    'dist:mac:x64',
    'dist:mac:all',
    'dist:linux',
    'dist:dir',
    'dist:mac:dir',
    'pack',
]

for (const name of installerScripts) {
    test(`${name} passes --publish never so a git tag cannot auto-upload`, () => {
        assert.ok(pkg.scripts[name], `missing script ${name}`)
        assert.match(pkg.scripts[name], /--publish never/)
    })
}
