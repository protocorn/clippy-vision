/**
 * Setup preflight must require Python 3.11+.
 * Run with: node --test electron/lib/setup.test.js
 */
const path = require('path')
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
    looksLikeMissingPythonVenvPackage,
    missingPythonVenvPackageMessage,
    looksLikeMissingZstd,
    formatMissingZstdMessage,
    summarizeCommandFailure,
    isIgnorablePipNotice,
    summarizePipFailure,
    linuxPynputInstallArgs,
    LINUX_OLLAMA_INSTALL_COMMAND,
    LINUX_OLLAMA_DOWNLOAD_URL,
    OLLAMA_DOWNLOAD_URL,
    PYTHON_DOWNLOAD_URL,
} = require('./setup')
const {
    pythonVenvDir,
    venvPythonPath,
    isHealthyVenv,
    PYTHON_VENV_DIRNAME,
    DEFAULT_CHAT_MODEL,
    DEFAULT_CHAT_MODEL_SIZE_LABEL,
} = require('./paths')
const { archiveSpec } = require('./managed-python')

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

test('linux x64 downloads standalone CPython instead of requiring system Python', () => {
    const plan = decidePythonInstall({ platform: 'linux', arch: 'x64' })
    const spec = archiveSpec('linux', 'x64')
    assert.equal(plan.action, 'standalone')
    assert.equal(plan.runInstall, true)
    assert.equal(plan.command, null)
    assert.equal(plan.downloadUrl, spec.url)
    assert.equal(plan.sha256, spec.sha256)
    assert.match(plan.instructions, /relocatable CPython/)
    assert.equal(installCommandToRun(plan), null)
})

test('win32 x64 downloads standalone CPython instead of winget-installing system Python', () => {
    const plan = decidePythonInstall({ platform: 'win32', arch: 'x64' })
    const spec = archiveSpec('win32', 'x64')
    assert.equal(plan.action, 'standalone')
    assert.equal(plan.runInstall, true)
    assert.equal(plan.downloadUrl, spec.url)
    assert.equal(plan.filename, spec.filename)
    assert.equal(installCommandToRun(plan), null)
})

test('darwin arm64 downloads standalone CPython instead of requiring Homebrew', () => {
    const plan = decidePythonInstall({ platform: 'darwin', arch: 'arm64' })
    assert.equal(plan.action, 'standalone')
    assert.equal(plan.runInstall, true)
    assert.match(plan.downloadUrl, /aarch64-apple-darwin-install_only_stripped/)
})

test('unsupported Python platform falls back to a python.org link', () => {
    const plan = decidePythonInstall({ platform: 'freebsd', arch: 'x64' })
    assert.equal(plan.action, 'manual')
    assert.equal(plan.runInstall, false)
    assert.equal(plan.downloadUrl, PYTHON_DOWNLOAD_URL)
    assert.match(plan.instructions, /https:\/\/www\.python\.org\/downloads\//)
    assert.equal(installCommandToRun(plan), null)
})

test('default chat model is the small llama3.2:3b summary model', () => {
    assert.equal(DEFAULT_CHAT_MODEL, 'llama3.2:3b')
    assert.equal(DEFAULT_CHAT_MODEL_SIZE_LABEL, '~2 GB')
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

test('python venv lives under userData/python-venv', () => {
    assert.equal(PYTHON_VENV_DIRNAME, 'python-venv')
    assert.equal(pythonVenvDir('/tmp/clippy-data'), path.join('/tmp/clippy-data', 'python-venv'))
})

test('venvPythonPath uses Scripts on Windows and bin elsewhere', () => {
    assert.equal(
        venvPythonPath('C:\\Users\\ada\\AppData\\Roaming\\Clippy\\python-venv', 'win32'),
        'C:\\Users\\ada\\AppData\\Roaming\\Clippy\\python-venv\\Scripts\\python.exe',
    )
    assert.equal(
        venvPythonPath('/home/ada/.config/clippy/python-venv', 'linux'),
        '/home/ada/.config/clippy/python-venv/bin/python',
    )
    assert.equal(
        venvPythonPath('/Users/ada/Library/Application Support/clippy/python-venv', 'darwin'),
        '/Users/ada/Library/Application Support/clippy/python-venv/bin/python',
    )
})

test('isHealthyVenv requires the platform interpreter path', () => {
    const linuxPy = venvPythonPath('/tmp/venv', 'linux')
    const winPy = venvPythonPath('C:\\Users\\ada\\venv', 'win32')
    assert.equal(isHealthyVenv('/tmp/venv', 'linux', (file) => file === linuxPy), true)
    assert.equal(isHealthyVenv('/tmp/venv', 'linux', () => false), false)
    assert.equal(isHealthyVenv('C:\\Users\\ada\\venv', 'win32', (file) => file === winPy), true)
    assert.equal(isHealthyVenv('C:\\Users\\ada\\venv', 'win32', () => false), false)
})

test('missing python3-venv package errors are detected', () => {
    assert.equal(
        looksLikeMissingPythonVenvPackage('The virtual environment was not created successfully because ensurepip is not available'),
        true,
    )
    assert.equal(
        looksLikeMissingPythonVenvPackage('On Debian/Ubuntu systems, you need to install the python3-venv package'),
        true,
    )
    assert.equal(looksLikeMissingPythonVenvPackage('Successfully created virtual environment'), false)
    assert.match(missingPythonVenvPackageMessage(), /python3-venv/)
    assert.match(missingPythonVenvPackageMessage(), /python3-full/)
})

test('missing zstd installer errors are detected', () => {
    assert.equal(
        looksLikeMissingZstd('ERROR: This version requires zstd for extraction'),
        true,
    )
    assert.equal(looksLikeMissingZstd('ollama installed successfully'), false)
})

test('formatMissingZstdMessage prefers the available package manager', () => {
    assert.match(
        formatMissingZstdMessage({ hasAptGet: true }),
        /sudo apt-get update && sudo apt-get install zstd/,
    )
    assert.match(
        formatMissingZstdMessage({ hasDnf: true }),
        /sudo dnf install zstd/,
    )
    assert.match(
        formatMissingZstdMessage({ hasPacman: true }),
        /sudo pacman -S zstd/,
    )
    const fallback = formatMissingZstdMessage({})
    assert.match(fallback, /apt-get install zstd/)
    assert.match(fallback, /dnf install zstd/)
    assert.match(fallback, /pacman -S zstd/)
})

test('summarizeCommandFailure surfaces stderr and exit code', () => {
    assert.equal(
        summarizeCommandFailure({ code: 1, stderr: 'This version requires zstd for extraction\nmore' }, 'failed'),
        'This version requires zstd for extraction (exit 1)',
    )
    assert.equal(
        summarizeCommandFailure({ code: 2, stdout: '', stderr: '' }, 'Ollama install failed'),
        'Ollama install failed (exit 2)',
    )
})

test('ignorable pip notices skip environment-marker Ignoring lines', () => {
    assert.equal(
        isIgnorablePipNotice("Ignoring pywin32: markers 'platform_system == \"Windows\"' don't match your environment"),
        true,
    )
    assert.equal(isIgnorablePipNotice('ERROR: Failed to build evdev'), false)
})

test('summarizePipFailure prefers ERROR lines over Ignoring notices', () => {
    const log = [
        "Ignoring pywin32: markers 'platform_system == \"Windows\"' don't match your environment",
        "Ignoring uiautomation: markers 'platform_system == \"Windows\"' don't match your environment",
        'Collecting evdev',
        'ERROR: Failed building wheel for evdev',
        'error: subprocess-exited-with-error',
    ].join('\n')
    assert.equal(
        summarizePipFailure({ code: 1, stderr: log }, 'pip install failed'),
        'ERROR: Failed building wheel for evdev (exit 1)',
    )
})

test('summarizePipFailure falls back when no ERROR line exists', () => {
    assert.equal(
        summarizePipFailure({ code: 1, stderr: '', stdout: '' }, 'pip install failed. Check the log.'),
        'pip install failed. Check the log. (exit 1)',
    )
})

test('linux pynput install uses --no-deps to skip evdev', () => {
    assert.deepEqual(linuxPynputInstallArgs(), ['install', '--no-deps', 'pynput'])
})
