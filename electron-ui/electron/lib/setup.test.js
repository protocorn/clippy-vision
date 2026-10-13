/**
 * Setup preflight must require Python 3.11+.
 * Run with: node --test electron/lib/setup.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert/strict')

const {
    evaluatePythonVersion,
    decideOllamaInstall,
    decidePythonInstall,
    installCommandToRun,
    ollamaInstallCommandToRun,
    knownOllamaInstallPaths,
    knownPythonInstallPaths,
    firstExistingPath,
    parseWindowsRegPathValue,
    mergePathDirectories,
    LINUX_OLLAMA_INSTALL_COMMAND,
    LINUX_OLLAMA_DOWNLOAD_URL,
    OLLAMA_DOWNLOAD_URL,
    PYTHON_DOWNLOAD_URL,
} = require('./setup')

function collectInstallRuns(plan) {
    const invocation = installCommandToRun(plan)
    const runs = []
    if (invocation) runs.push([invocation.command, invocation.args])
    return runs
}

test('Python 3.9 is too old', () => {
    const result = evaluatePythonVersion('Python 3.9.18')
    assert.equal(result.ok, false)
    assert.equal(result.version, '3.9.18')
    assert.match(result.message, /Python 3\.9\.18 was found/)
    assert.match(result.message, /3\.11\+ is required/)
})

test('Python 3.10.x is too old', () => {
    const result = evaluatePythonVersion('Python 3.10.4')
    assert.equal(result.ok, false)
    assert.equal(result.version, '3.10.4')
    assert.match(result.message, /Python 3\.10\.4 was found/)
    assert.match(result.message, /3\.11\+ is required/)
})

test('Python 3.11.0 meets the minimum', () => {
    const result = evaluatePythonVersion('Python 3.11.0')
    assert.equal(result.ok, true)
    assert.equal(result.version, '3.11.0')
    assert.equal(result.message, null)
})

test('Python 3.12 meets the minimum', () => {
    const result = evaluatePythonVersion('Python 3.12')
    assert.equal(result.ok, true)
    assert.equal(result.version, '3.12')
})

test('Python 3.13 meets the minimum', () => {
    const result = evaluatePythonVersion('Python 3.13.1')
    assert.equal(result.ok, true)
    assert.equal(result.version, '3.13.1')
})

test('unparseable output reports a clear error', () => {
    const result = evaluatePythonVersion('not a version string')
    assert.equal(result.ok, false)
    assert.equal(result.version, null)
    assert.match(result.message, /Could not parse Python version/)
    assert.match(result.message, /3\.11\+ is required/)
})

test('linux without consent does not run an install command', () => {
    const plan = decideOllamaInstall({ platform: 'linux', userConsent: false })
    assert.equal(plan.action, 'linux-manual')
    assert.equal(plan.runInstall, false)
    assert.equal(plan.command, null)
    assert.equal(plan.officialCommand, LINUX_OLLAMA_INSTALL_COMMAND)
    assert.equal(plan.downloadUrl, LINUX_OLLAMA_DOWNLOAD_URL)
    assert.match(plan.instructions, /curl -fsSL https:\/\/ollama.com\/install.sh \| sh/)
    assert.match(plan.instructions, /https:\/\/ollama.com\/download\/linux/)
    assert.deepEqual(collectInstallRuns(plan), [])
    assert.equal(ollamaInstallCommandToRun(plan), null)
})

test('linux with consent plans pkexec for the official installer', () => {
    const plan = decideOllamaInstall({ platform: 'linux', userConsent: true })
    assert.equal(plan.action, 'linux-install')
    assert.equal(plan.runInstall, true)
    assert.equal(plan.command, 'pkexec')
    assert.equal(plan.officialCommand, LINUX_OLLAMA_INSTALL_COMMAND)
    assert.equal(plan.downloadUrl, LINUX_OLLAMA_DOWNLOAD_URL)
    const runs = collectInstallRuns(plan)
    assert.equal(runs.length, 1)
    assert.equal(runs[0][0], 'pkexec')
    assert.deepEqual(runs[0][1], ['sh', '-c', LINUX_OLLAMA_INSTALL_COMMAND])
    const invocation = ollamaInstallCommandToRun(plan)
    assert.equal(invocation.command, 'pkexec')
    assert.ok(invocation.args.join(' ').includes(LINUX_OLLAMA_INSTALL_COMMAND))
})

test('darwin uses Homebrew for a missing Ollama when brew is present', () => {
    const plan = decideOllamaInstall({ platform: 'darwin', brewAvailable: true })
    assert.equal(plan.action, 'homebrew')
    assert.equal(plan.runInstall, true)
    assert.equal(plan.command, 'brew')
    assert.deepEqual(plan.args, ['install', 'ollama'])
    const runs = collectInstallRuns(plan)
    assert.equal(runs.length, 1)
    assert.equal(runs[0][0], 'brew')
    assert.deepEqual(runs[0][1], ['install', 'ollama'])
})

test('darwin without Homebrew shows a manual Ollama download link', () => {
    const plan = decideOllamaInstall({ platform: 'darwin', brewAvailable: false })
    assert.equal(plan.action, 'manual')
    assert.equal(plan.runInstall, false)
    assert.equal(plan.downloadUrl, OLLAMA_DOWNLOAD_URL)
    assert.match(plan.instructions, /https:\/\/ollama.com\/download/)
    assert.equal(installCommandToRun(plan), null)
})

test('win32 uses winget for a missing Ollama when winget is present', () => {
    const plan = decideOllamaInstall({ platform: 'win32', wingetAvailable: true })
    assert.equal(plan.action, 'winget')
    assert.equal(plan.runInstall, true)
    assert.equal(plan.command, 'winget')
    assert.ok(plan.args.includes('Ollama.Ollama'))
    assert.ok(plan.args.includes('--silent'))
    const runs = collectInstallRuns(plan)
    assert.equal(runs.length, 1)
    assert.equal(runs[0][0], 'winget')
    assert.ok(runs[0][1].includes('Ollama.Ollama'))
})

test('win32 without winget shows a manual Ollama download link', () => {
    const plan = decideOllamaInstall({ platform: 'win32', wingetAvailable: false })
    assert.equal(plan.action, 'manual')
    assert.equal(plan.runInstall, false)
    assert.equal(plan.downloadUrl, OLLAMA_DOWNLOAD_URL)
    assert.match(plan.instructions, /https:\/\/ollama.com\/download/)
    assert.equal(installCommandToRun(plan), null)
})

test('linux Python stays a manual install with a python.org link', () => {
    const plan = decidePythonInstall({ platform: 'linux', brewAvailable: true })
    assert.equal(plan.action, 'linux-manual')
    assert.equal(plan.runInstall, false)
    assert.equal(plan.downloadUrl, PYTHON_DOWNLOAD_URL)
    assert.match(plan.instructions, /https:\/\/www\.python\.org\/downloads\//)
    assert.equal(installCommandToRun(plan), null)
})

test('darwin uses Homebrew for a missing Python when brew is present', () => {
    const plan = decidePythonInstall({ platform: 'darwin', brewAvailable: true })
    assert.equal(plan.action, 'homebrew')
    assert.equal(plan.runInstall, true)
    assert.equal(plan.command, 'brew')
    assert.deepEqual(plan.args, ['install', 'python'])
})

test('darwin without Homebrew shows a python.org link', () => {
    const plan = decidePythonInstall({ platform: 'darwin', brewAvailable: false })
    assert.equal(plan.action, 'manual')
    assert.equal(plan.runInstall, false)
    assert.equal(plan.downloadUrl, PYTHON_DOWNLOAD_URL)
    assert.match(plan.instructions, /https:\/\/www\.python\.org\/downloads\//)
    assert.equal(installCommandToRun(plan), null)
})

test('win32 uses winget for a missing Python when winget is present', () => {
    const plan = decidePythonInstall({ platform: 'win32', wingetAvailable: true })
    assert.equal(plan.action, 'winget')
    assert.equal(plan.runInstall, true)
    assert.equal(plan.command, 'winget')
    assert.ok(plan.args.includes('Python.Python.3.11'))
})

test('win32 without winget shows a python.org link', () => {
    const plan = decidePythonInstall({ platform: 'win32', wingetAvailable: false })
    assert.equal(plan.action, 'manual')
    assert.equal(plan.runInstall, false)
    assert.equal(plan.downloadUrl, PYTHON_DOWNLOAD_URL)
    assert.match(plan.instructions, /https:\/\/www\.python\.org\/downloads\//)
    assert.equal(installCommandToRun(plan), null)
})

test('darwin Ollama known paths include Homebrew prefixes', () => {
    const paths = knownOllamaInstallPaths({ platform: 'darwin', home: '/Users/ada' })
    assert.ok(paths.includes('/opt/homebrew/bin/ollama'))
    assert.ok(paths.includes('/usr/local/bin/ollama'))
})

test('linux Ollama known paths include /usr/local/bin/ollama', () => {
    const paths = knownOllamaInstallPaths({ platform: 'linux', home: '/home/ada' })
    assert.ok(paths.includes('/usr/local/bin/ollama'))
})

test('windows Ollama known path is LocalAppData Programs Ollama', () => {
    const paths = knownOllamaInstallPaths({
        platform: 'win32',
        env: { LOCALAPPDATA: 'C:\\Users\\ada\\AppData\\Local', ProgramFiles: 'C:\\Program Files' },
        home: 'C:\\Users\\ada',
    })
    assert.ok(paths.includes('C:\\Users\\ada\\AppData\\Local\\Programs\\Ollama\\ollama.exe'))
    assert.ok(paths.includes('C:\\Program Files\\Ollama\\ollama.exe'))
})

test('windows Python known path includes Python311 under LocalAppData', () => {
    const paths = knownPythonInstallPaths({
        platform: 'win32',
        env: { LOCALAPPDATA: 'C:\\Users\\ada\\AppData\\Local', ProgramFiles: 'C:\\Program Files' },
        home: 'C:\\Users\\ada',
    })
    assert.ok(paths.includes('C:\\Users\\ada\\AppData\\Local\\Programs\\Python\\Python311\\python.exe'))
})

test('firstExistingPath returns the first candidate that exists', () => {
    const found = firstExistingPath(
        ['missing', 'C:\\real\\Ollama\\ollama.exe', 'other'],
        (file) => file === 'C:\\real\\Ollama\\ollama.exe',
    )
    assert.equal(found, 'C:\\real\\Ollama\\ollama.exe')
})

test('parseWindowsRegPathValue reads REG_EXPAND_SZ Path', () => {
    const output = [
        'HKEY_CURRENT_USER\\Environment',
        '    Path    REG_EXPAND_SZ    C:\\Users\\ada\\bin;%USERPROFILE%\\AppData\\Local\\Programs\\Ollama',
    ].join('\n')
    assert.equal(
        parseWindowsRegPathValue(output),
        'C:\\Users\\ada\\bin;%USERPROFILE%\\AppData\\Local\\Programs\\Ollama',
    )
})

test('mergePathDirectories prepends new install dirs without duplicates', () => {
    const merged = mergePathDirectories([
        'C:\\Users\\ada\\AppData\\Local\\Programs\\Ollama',
        'C:\\Windows\\System32;C:\\Users\\ada\\AppData\\Local\\Programs\\Ollama',
    ], ';')
    assert.equal(
        merged,
        'C:\\Users\\ada\\AppData\\Local\\Programs\\Ollama;C:\\Windows\\System32',
    )
})
