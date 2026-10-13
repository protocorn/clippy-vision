const path = require('path')
const fs   = require('fs')
const os   = require('os')
const http = require('http')
const { pythonVenvDir, venvPythonPath, isHealthyVenv } = require('./paths')

const MIN_PYTHON_MAJOR = 3
const MIN_PYTHON_MINOR = 11

function evaluatePythonVersion(output) {
    const text = String(output == null ? '' : output)
    const match = text.match(/Python\s+(\d+)\.(\d+)(?:\.(\d+))?/i)
    if (!match) {
        const preview = text.trim() || '(empty)'
        return {
            ok: false,
            version: null,
            message: `Could not parse Python version from '${preview}'. Python 3.11+ is required.`,
        }
    }
    const major = Number(match[1])
    const minor = Number(match[2])
    const patch = match[3] === undefined ? 0 : Number(match[3])
    const version = match[3] === undefined ? `${major}.${minor}` : `${major}.${minor}.${patch}`
    if (major > MIN_PYTHON_MAJOR || (major === MIN_PYTHON_MAJOR && minor >= MIN_PYTHON_MINOR)) {
        return { ok: true, version, message: null }
    }
    return {
        ok: false,
        version,
        message: `Python ${version} was found, but Python 3.11+ is required.`,
    }
}

const LINUX_OLLAMA_INSTALL_COMMAND = 'curl -fsSL https://ollama.com/install.sh | sh'
const LINUX_OLLAMA_DOWNLOAD_URL = 'https://ollama.com/download/linux'
const OLLAMA_DOWNLOAD_URL = 'https://ollama.com/download'
const PYTHON_DOWNLOAD_URL = 'https://www.python.org/downloads/'
const WINGET_PYTHON_ARGS = [
    'install', 'Python.Python.3.11',
    '--silent',
    '--accept-package-agreements',
    '--accept-source-agreements',
]
const WINGET_OLLAMA_ARGS = [
    'install', 'Ollama.Ollama',
    '--silent',
    '--accept-package-agreements',
    '--accept-source-agreements',
]
const ALLOWED_SETUP_DOWNLOAD_URLS = new Set([
    LINUX_OLLAMA_DOWNLOAD_URL,
    OLLAMA_DOWNLOAD_URL,
    'https://ollama.com/download/mac',
    'https://ollama.com/download/windows',
    PYTHON_DOWNLOAD_URL,
])

function pathModFor(platform) {
    return platform === 'win32' ? path.win32 : path.posix
}

function decideOllamaInstall({ platform, userConsent, brewAvailable, wingetAvailable } = {}) {
    if (platform === 'win32') {
        if (wingetAvailable === false) {
            return {
                action: 'manual',
                runInstall: false,
                command: null,
                args: null,
                downloadUrl: OLLAMA_DOWNLOAD_URL,
                instructions: `Ollama was not found and winget is not available. Install it from ${OLLAMA_DOWNLOAD_URL}, then retry.`,
            }
        }
        return {
            action: 'winget',
            runInstall: true,
            command: 'winget',
            args: WINGET_OLLAMA_ARGS.slice(),
            downloadUrl: OLLAMA_DOWNLOAD_URL,
        }
    }
    if (platform === 'darwin') {
        if (brewAvailable === false) {
            return {
                action: 'manual',
                runInstall: false,
                command: null,
                args: null,
                downloadUrl: OLLAMA_DOWNLOAD_URL,
                instructions: `Ollama was not found and Homebrew is not installed. Install it from ${OLLAMA_DOWNLOAD_URL}, then retry.`,
            }
        }
        return {
            action: 'homebrew',
            runInstall: true,
            command: 'brew',
            args: ['install', 'ollama'],
            downloadUrl: OLLAMA_DOWNLOAD_URL,
        }
    }
    if (platform === 'linux') {
        if (userConsent) {
            return {
                action: 'linux-install',
                runInstall: true,
                command: 'pkexec',
                args: ['sh', '-c', LINUX_OLLAMA_INSTALL_COMMAND],
                officialCommand: LINUX_OLLAMA_INSTALL_COMMAND,
                downloadUrl: LINUX_OLLAMA_DOWNLOAD_URL,
            }
        }
        return {
            action: 'linux-manual',
            runInstall: false,
            command: null,
            args: null,
            officialCommand: LINUX_OLLAMA_INSTALL_COMMAND,
            downloadUrl: LINUX_OLLAMA_DOWNLOAD_URL,
            instructions: [
                'Ollama was not found on PATH.',
                `Install it with: ${LINUX_OLLAMA_INSTALL_COMMAND}`,
                `Or download from ${LINUX_OLLAMA_DOWNLOAD_URL}`,
                'The wizard runs that installer only after you click Install and confirm it needs sudo/admin rights.',
            ].join(' '),
        }
    }
    return {
        action: 'unsupported',
        runInstall: false,
        command: null,
        args: null,
    }
}

function decidePythonInstall({ platform, brewAvailable, wingetAvailable } = {}) {
    if (platform === 'linux') {
        return {
            action: 'linux-manual',
            runInstall: false,
            command: null,
            args: null,
            downloadUrl: PYTHON_DOWNLOAD_URL,
            instructions: `Python 3.11+ was not found. Install it from ${PYTHON_DOWNLOAD_URL}, then retry. The wizard does not install Python on Linux.`,
        }
    }
    if (platform === 'darwin') {
        if (brewAvailable === false) {
            return {
                action: 'manual',
                runInstall: false,
                command: null,
                args: null,
                downloadUrl: PYTHON_DOWNLOAD_URL,
                instructions: `Python 3.11+ was not found and Homebrew is not installed. Install it from ${PYTHON_DOWNLOAD_URL}, then retry.`,
            }
        }
        return {
            action: 'homebrew',
            runInstall: true,
            command: 'brew',
            args: ['install', 'python'],
            downloadUrl: PYTHON_DOWNLOAD_URL,
        }
    }
    if (platform === 'win32') {
        if (wingetAvailable === false) {
            return {
                action: 'manual',
                runInstall: false,
                command: null,
                args: null,
                downloadUrl: PYTHON_DOWNLOAD_URL,
                instructions: `Python 3.11+ was not found and winget is not available. Install it from ${PYTHON_DOWNLOAD_URL}, then retry.`,
            }
        }
        return {
            action: 'winget',
            runInstall: true,
            command: 'winget',
            args: WINGET_PYTHON_ARGS.slice(),
            downloadUrl: PYTHON_DOWNLOAD_URL,
        }
    }
    return {
        action: 'unsupported',
        runInstall: false,
        command: null,
        args: null,
        downloadUrl: PYTHON_DOWNLOAD_URL,
    }
}

function installCommandToRun(plan) {
    if (!plan || !plan.runInstall || !plan.command) return null
    return { command: plan.command, args: Array.isArray(plan.args) ? plan.args : [] }
}

function ollamaInstallCommandToRun(plan) {
    return installCommandToRun(plan)
}

function knownOllamaInstallPaths({ platform, env = {}, home = '' } = {}) {
    const p = pathModFor(platform)
    if (platform === 'win32') {
        const localAppData = env.LOCALAPPDATA || p.join(home || 'C:\\Users\\Default', 'AppData', 'Local')
        const programFiles = env.ProgramFiles || 'C:\\Program Files'
        return [
            p.join(localAppData, 'Programs', 'Ollama', 'ollama.exe'),
            p.join(programFiles, 'Ollama', 'ollama.exe'),
        ]
    }
    if (platform === 'darwin') {
        return [
            '/opt/homebrew/bin/ollama',
            '/usr/local/bin/ollama',
            p.join(home, '.local', 'bin', 'ollama'),
        ]
    }
    return [
        '/usr/local/bin/ollama',
        '/usr/bin/ollama',
        p.join(home, '.local', 'bin', 'ollama'),
    ]
}

function knownPythonInstallPaths({ platform, env = {}, home = '' } = {}) {
    const p = pathModFor(platform)
    if (platform === 'win32') {
        const localAppData = env.LOCALAPPDATA || p.join(home || 'C:\\Users\\Default', 'AppData', 'Local')
        const programFiles = env.ProgramFiles || 'C:\\Program Files'
        const versions = ['Python313', 'Python312', 'Python311']
        const out = []
        for (const ver of versions) {
            out.push(p.join(localAppData, 'Programs', 'Python', ver, 'python.exe'))
            out.push(p.join(programFiles, ver, 'python.exe'))
        }
        out.push('C:\\Python313\\python.exe', 'C:\\Python312\\python.exe', 'C:\\Python311\\python.exe')
        return out
    }
    if (platform === 'darwin') {
        return [
            '/opt/homebrew/bin/python3',
            '/usr/local/bin/python3',
            '/usr/bin/python3',
        ]
    }
    return [
        '/usr/bin/python3',
        '/usr/local/bin/python3',
        p.join(home, '.local', 'bin', 'python3'),
    ]
}

function firstExistingPath(candidates, existsFn) {
    if (typeof existsFn !== 'function') return null
    for (const candidate of candidates || []) {
        if (candidate && existsFn(candidate)) return candidate
    }
    return null
}

function parseWindowsRegPathValue(output) {
    const match = String(output || '').match(/Path\s+REG_(?:EXPAND_)?SZ\s+(.*)$/im)
    return match ? match[1].trim() : ''
}

function mergePathDirectories(chunks, delimiter) {
    const sep = delimiter || path.delimiter
    const seen = new Set()
    const out = []
    for (const chunk of chunks || []) {
        for (const part of String(chunk || '').split(sep)) {
            const trimmed = part.trim()
            if (!trimmed) continue
            const key = trimmed.toLowerCase()
            if (seen.has(key)) continue
            seen.add(key)
            out.push(trimmed)
        }
    }
    return out.join(sep)
}

function looksLikeMissingPythonVenvPackage(output) {
    const text = String(output || '').toLowerCase()
    return (
        text.includes('ensurepip is not available') ||
        text.includes('python3-venv') ||
        text.includes('python3-full') ||
        text.includes('no module named \'venv\'') ||
        text.includes('no module named "venv"') ||
        (text.includes('ensurepip') && text.includes('venv'))
    )
}

function missingPythonVenvPackageMessage() {
    return [
        'Could not create a Python virtual environment.',
        'On Debian/Ubuntu install python3-venv (or python3-full), then retry.',
        'Example: sudo apt-get update && sudo apt-get install python3-venv',
    ].join(' ')
}

function looksLikeMissingZstd(output) {
    const text = String(output || '').toLowerCase()
    return text.includes('requires zstd')
        || (text.includes('zstd') && text.includes('extraction'))
}

function formatMissingZstdMessage({ hasAptGet, hasDnf, hasPacman } = {}) {
    const commands = []
    if (hasAptGet) commands.push('sudo apt-get update && sudo apt-get install zstd')
    if (hasDnf) commands.push('sudo dnf install zstd')
    if (hasPacman) commands.push('sudo pacman -S zstd')
    if (commands.length === 0) {
        commands.push(
            'sudo apt-get update && sudo apt-get install zstd',
            'sudo dnf install zstd',
            'sudo pacman -S zstd',
        )
    }
    return `Ollama's Linux installer requires zstd for extraction. Install it with: ${commands.join(' — or — ')}`
}

function summarizeCommandFailure(result, fallback = 'Command failed') {
    if (!result) return fallback
    const stderr = String(result.stderr || '').trim()
    const stdout = String(result.stdout || '').trim()
    const detail = stderr || stdout
    const codePart = result.code == null || result.code === 0 ? '' : ` (exit ${result.code})`
    if (!detail) return `${fallback}${codePart}`
    const firstLine = detail.split(/\r?\n/).map((line) => line.trim()).find(Boolean) || detail
    // Keep wizard messages readable while still surfacing the real installer error.
    const clipped = firstLine.length > 240 ? `${firstLine.slice(0, 237)}...` : firstLine
    return `${clipped}${codePart}`
}

function setupManualError(code, stepSub, extra = {}) {
    const err = new Error(code)
    err.stepSub = stepSub
    if (extra.offerLinuxInstall) err.offerLinuxInstall = true
    if (extra.downloadUrl) err.downloadUrl = extra.downloadUrl
    return err
}

function linuxOllamaMissingError(message) {
    return setupManualError(
        'ollama-install-required',
        message || `Install Ollama first. Official command: ${LINUX_OLLAMA_INSTALL_COMMAND} — ${LINUX_OLLAMA_DOWNLOAD_URL}`,
        { offerLinuxInstall: true, downloadUrl: LINUX_OLLAMA_DOWNLOAD_URL },
    )
}

function createSetup({ paths, llmConfig, api, state, app, windowBridge, onLaunched, dialog, shell }) {
    const {
        ROOT,
        DATA_DIR,
        USER_DATA,
        REQUIREMENTS,
        SETUP_FLAG,
        RESIDENCY_FILE,
        SYSTEM_PYTHON_COMMAND,
        PYTHON_VENV_DIR,
        OLLAMA_COMMAND,
        OLLAMA_MAX_LOADED_MODELS,
    } = paths
    // Always read paths.PYTHON_COMMAND so the packages step can switch to the venv.
    function pythonCommand() {
        return paths.PYTHON_COMMAND
    }
    function systemPythonCommand() {
        return paths.SYSTEM_PYTHON_COMMAND || SYSTEM_PYTHON_COMMAND || pythonCommand()
    }
    function venvDir() {
        return paths.PYTHON_VENV_DIR || PYTHON_VENV_DIR || pythonVenvDir(USER_DATA)
    }
    const {
        readLLMConfig,
        configuredOllamaBaseURL,
        usesManagedOllama,
    } = llmConfig
    const {
        spawnHidden,
        runCommand,
        pollUntilAlive,
        httpPost,
        apiUrl,
        startServer,
        ensureOllamaParallelConfig,
        ensureOllamaServing,
        ollamaReachable,
    } = api

    function sendSetup(channel, data) {
        if (state.setupWindow && !state.setupWindow.isDestroyed()) {
            state.setupWindow.webContents.send(channel, data)
        }
    }

    function log(line, level = 'info') {
        sendSetup('setup-log', { line, level })
        console.log(`[setup/${level}]`, line)
    }

    function stepUpdate(key, stateVal, sub, extra) {
        sendSetup('step-update', {
            key,
            state: stateVal,
            sub,
            ...(extra && typeof extra === 'object' ? extra : {}),
        })
    }

    function stepProgress(key, percent) {
        sendSetup('step-progress', { key, percent })
    }

    function markDone(key, sub) {
        // The renderer owns the visual step state; Electron only sends monotonic
        // completion updates after each asynchronous installer step finishes.
        state.doneSoFar++
        stepUpdate(key, 'done', sub)
        sendSetup('setup-overall', { done: state.doneSoFar, text: `${state.doneSoFar} / ${state.setupStepTotal} steps` })
    }

    function ollamaListHasModel(output, required) {
        const expected = String(required || '').trim()
        if (!expected) return false
        return String(output || '').split(/\r?\n/).some((line) => {
            const actual = line.trim().split(/\s+/)[0]
            return actual === expected || (!expected.includes(':') && actual === `${expected}:latest`)
        })
    }

    function requiredChatModels() {
        // Only the local AI model the user chose in setup — never force a default pull.
        const chatModel = String(readLLMConfig().chat_model || '').trim()
        return chatModel ? [chatModel] : []
    }

    function tagsHaveModel(tags, required) {
        const expected = String(required || '').trim()
        if (!expected) return false
        const names = (tags && Array.isArray(tags.models) ? tags.models : []).map((row) => (
            String((row && (row.name || row.model)) || '').trim()
        ))
        return names.some((actual) => (
            actual === expected || (!expected.includes(':') && actual === `${expected}:latest`)
        ))
    }

    function fetchOllamaTags(timeoutMs = 4000) {
        return new Promise((resolve) => {
            try {
                const url = new URL('/api/tags', `${configuredOllamaBaseURL().replace(/\/+$/, '')}/`)
                const req = http.get({
                    hostname: url.hostname,
                    port: url.port || (url.protocol === 'https:' ? 443 : 80),
                    path: url.pathname + url.search,
                }, (res) => {
                    let body = ''
                    res.setEncoding('utf8')
                    res.on('data', (chunk) => { body += chunk })
                    res.on('end', () => {
                        try { resolve(JSON.parse(body)) } catch (_) { resolve(null) }
                    })
                })
                req.on('error', () => resolve(null))
                req.setTimeout(timeoutMs, () => { req.destroy(); resolve(null) })
            } catch (_) {
                resolve(null)
            }
        })
    }

    async function ollamaHasModel(name) {
        const tags = await fetchOllamaTags()
        if (tags && tagsHaveModel(tags, name)) return true
        const list = await runCommand(OLLAMA_COMMAND, ['list'])
        return ollamaListHasModel(list.stdout, name) || ollamaListHasModel(list.stderr, name)
    }

    function describePreflightFailure(step, reason) {
        const model = String(readLLMConfig().chat_model || 'your local AI model').trim()
        const copy = {
            python: {
                title: 'Python is missing',
                detail: 'Clippy needs Python 3.11+ on PATH. Install it, then continue.',
            },
            ollama: {
                title: 'Ollama is not installed',
                detail: 'Install Ollama so Clippy can run the local AI model.',
            },
            'ollama-service': {
                title: 'Ollama is not running',
                detail: 'Start Ollama, then continue. Clippy uses it for chat and backend LLM jobs.',
            },
            models: {
                title: `Local AI model ${model} is missing`,
                detail: `${model} is not installed in Ollama. It may have been deleted. Re-download it to continue.`,
            },
            packages: {
                title: 'Python packages are missing',
                detail: 'One or more required packages need to be reinstalled.',
            },
            warmup: {
                title: 'The local AI model could not be loaded',
                detail: 'Ollama is reachable but the model failed to load. Free some memory, then try again.',
            },
        }
        const known = copy[step] || {
            title: 'Clippy is not ready',
            detail: 'A startup check failed. Fix the issue below, then continue.',
        }
        return {
            ok: false,
            step,
            reason: reason || known.detail,
            title: known.title,
            detail: known.detail,
        }
    }

    function classifyChatError(rawError) {
        const text = String(rawError || '')
        const folded = text.toLowerCase()
        if (!folded) return null
        if (folded.includes('not found') || (folded.includes('http 404') && folded.includes('model'))) {
            return describePreflightFailure('models', text)
        }
        if (folded.includes('not reachable') || folded.includes('econnrefused')) {
            return describePreflightFailure('ollama-service', text)
        }
        if (folded.includes('model requires more') || folded.includes('error loading model')) {
            return describePreflightFailure('warmup', text)
        }
        return null
    }

    async function installWithBrew(formula) {
        if (process.platform === 'win32') return false
        const brew = await runCommand('brew', ['--prefix'])
        if (brew.code !== 0) return false
        log(`> brew install ${formula}`, 'dim')
        const install = await runCommand('brew', ['install', formula])
        if (install.stdout) log(install.stdout, 'dim')
        if (install.code !== 0) {
            log(install.stderr || 'brew install failed', 'err')
            return false
        }
        return true
    }

    function addDirToPath(dir) {
        if (!dir) return
        process.env.PATH = mergePathDirectories([dir, process.env.PATH], path.delimiter)
    }

    async function brewIsAvailable() {
        const brew = await runCommand('brew', ['--prefix'])
        return brew.code === 0
    }

    async function wingetIsAvailable() {
        const winget = await runCommand('winget', ['--version'])
        return winget.code === 0
    }

    async function refreshWindowsPathFromRegistry() {
        if (process.platform !== 'win32') return
        const machine = await runCommand('reg', [
            'query',
            'HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment',
            '/v',
            'Path',
        ])
        const user = await runCommand('reg', ['query', 'HKCU\\Environment', '/v', 'Path'])
        const merged = mergePathDirectories([
            parseWindowsRegPathValue(user.stdout || ''),
            parseWindowsRegPathValue(machine.stdout || ''),
            process.env.PATH,
        ], ';')
        if (merged) process.env.PATH = merged
    }

    async function verifyWithKnownPaths(command, extraCandidates, args = ['--version']) {
        let result = await runCommand(command, args)
        if (result.code === 0) return result
        for (const candidate of extraCandidates || []) {
            if (!candidate) continue
            try {
                if (!fs.existsSync(candidate)) continue
            } catch (_) { continue }
            addDirToPath(path.dirname(candidate))
            result = await runCommand(candidate, args)
            if (result.code === 0) return result
        }
        return result
    }

    function runtimeOllamaPaths() {
        return knownOllamaInstallPaths({
            platform: process.platform,
            env: process.env,
            home: os.homedir(),
        })
    }

    function runtimePythonPaths() {
        return knownPythonInstallPaths({
            platform: process.platform,
            env: process.env,
            home: os.homedir(),
        })
    }

    async function verifyOllamaAfterInstall() {
        if (process.platform === 'win32') await refreshWindowsPathFromRegistry()
        const extra = runtimeOllamaPaths()
        extra.forEach((candidate) => addDirToPath(path.dirname(candidate)))
        return verifyWithKnownPaths(OLLAMA_COMMAND, extra)
    }

    async function verifyPythonAfterInstall() {
        if (process.platform === 'win32') await refreshWindowsPathFromRegistry()
        const extra = runtimePythonPaths()
        extra.forEach((candidate) => addDirToPath(path.dirname(candidate)))
        return verifyWithKnownPaths(systemPythonCommand(), extra)
    }

    function acceptPythonVersion(raw) {
        const check = evaluatePythonVersion(raw)
        if (check.ok) {
            const label = String(raw || '').trim() || `Python ${check.version}`
            log(label, 'ok')
            markDone('python', label)
            return true
        }
        log(check.message, 'err')
        stepUpdate('python', 'error', check.message)
        return false
    }

    async function stepCheckPython() {
        // Windows installs a missing Python with winget. macOS uses Homebrew
        // when Homebrew is already installed. Linux stays a manual install.
        stepUpdate('python', 'running', 'Checking for Python 3.11+...')
        log('> python --version', 'dim')

        const found = await verifyWithKnownPaths(systemPythonCommand(), runtimePythonPaths())
        const version = found.stdout || found.stderr

        if (found.code === 0 && version) {
            if (acceptPythonVersion(version)) return
            throw new Error('python-version-required')
        }

        const plan = decidePythonInstall({
            platform: process.platform,
            brewAvailable: process.platform === 'darwin' ? await brewIsAvailable() : undefined,
            wingetAvailable: process.platform === 'win32' ? await wingetIsAvailable() : undefined,
        })
        const invocation = installCommandToRun(plan)

        if (!invocation) {
            log(plan.instructions || 'Python 3.11+ was not found.', 'err')
            if (plan.downloadUrl) log(`Download: ${plan.downloadUrl}`, 'info')
            throw setupManualError(
                'python-install-required',
                plan.instructions || `Install Python 3.11+ from ${PYTHON_DOWNLOAD_URL}, then retry.`,
                { downloadUrl: plan.downloadUrl || PYTHON_DOWNLOAD_URL },
            )
        }

        if (plan.action === 'homebrew') {
            log('Python not found. Trying Homebrew...', 'info')
            stepUpdate('python', 'running', 'Installing Python via Homebrew...')
            const installed = await installWithBrew('python')
            if (installed) {
                const verify = await verifyPythonAfterInstall()
                const verifiedVersion = verify.stdout || verify.stderr
                if (verify.code === 0 && verifiedVersion) {
                    if (acceptPythonVersion(verifiedVersion)) return
                    throw new Error('python-version-required')
                }
            }
            log(`Python was not found on PATH. Install Python 3.11+ from ${PYTHON_DOWNLOAD_URL}, then retry.`, 'err')
            throw setupManualError(
                'python-install-required',
                `Install Python 3.11+ from ${PYTHON_DOWNLOAD_URL}, then retry.`,
                { downloadUrl: PYTHON_DOWNLOAD_URL },
            )
        }

        log('Python not found. Installing via winget...', 'info')
        stepUpdate('python', 'running', 'Installing Python 3.11 via winget...')
        log('> winget install Python.Python.3.11 --silent', 'dim')

        const install = await runCommand(invocation.command, invocation.args)

        if (install.code !== 0) {
            log(install.stderr || 'winget failed', 'err')
            log(`Install Python 3.11+ from ${PYTHON_DOWNLOAD_URL}, then retry.`, 'err')
            throw setupManualError(
                'python-install-failed',
                `Could not install Python via winget. Install it from ${PYTHON_DOWNLOAD_URL}, then retry.`,
                { downloadUrl: PYTHON_DOWNLOAD_URL },
            )
        }

        const verify = await verifyPythonAfterInstall()
        if (verify.code !== 0) {
            throw setupManualError(
                'python-path',
                `Python installed but is not on PATH. Install location was probed; if it still fails, install from ${PYTHON_DOWNLOAD_URL} and retry.`,
                { downloadUrl: PYTHON_DOWNLOAD_URL },
            )
        }

        const verifiedVersion = verify.stdout || verify.stderr || 'Python installed.'
        if (acceptPythonVersion(verifiedVersion)) return
        throw new Error('python-version-required')
    }

    async function confirmLinuxOllamaInstall() {
        if (!dialog || typeof dialog.showMessageBox !== 'function') return false
        const parent = state.setupWindow && !state.setupWindow.isDestroyed()
            ? state.setupWindow
            : undefined
        const options = {
            type: 'warning',
            buttons: ['Cancel', 'Install'],
            defaultId: 1,
            cancelId: 0,
            noLink: true,
            title: 'Install Ollama',
            message: 'Install Ollama with the official Linux installer?',
            detail: 'This needs sudo/admin rights. You will be asked for your password. Clippy will not run the installer automatically.',
        }
        const result = parent
            ? await dialog.showMessageBox(parent, options)
            : await dialog.showMessageBox(options)
        return result.response === 1
    }

    async function detectLinuxPackageManagers() {
        const [apt, dnf, pacman] = await Promise.all([
            runCommand('sh', ['-c', 'command -v apt-get']),
            runCommand('sh', ['-c', 'command -v dnf']),
            runCommand('sh', ['-c', 'command -v pacman']),
        ])
        return {
            hasAptGet: apt.code === 0,
            hasDnf: dnf.code === 0,
            hasPacman: pacman.code === 0,
        }
    }

    async function ensureLinuxZstdAvailable() {
        const zstd = await runCommand('sh', ['-c', 'command -v zstd'])
        if (zstd.code === 0) return
        const managers = await detectLinuxPackageManagers()
        const message = formatMissingZstdMessage(managers)
        log(message, 'err')
        throw setupManualError('ollama-zstd-missing', message, {
            offerLinuxInstall: true,
            downloadUrl: LINUX_OLLAMA_DOWNLOAD_URL,
        })
    }

    async function throwLinuxOllamaInstallFailure(install, fallback) {
        const combined = `${install && install.stdout || ''}\n${install && install.stderr || ''}`
        if (looksLikeMissingZstd(combined)) {
            const managers = await detectLinuxPackageManagers()
            const message = formatMissingZstdMessage(managers)
            log(message, 'err')
            throw setupManualError('ollama-zstd-missing', message, {
                offerLinuxInstall: true,
                downloadUrl: LINUX_OLLAMA_DOWNLOAD_URL,
            })
        }
        const detail = summarizeCommandFailure(install, fallback)
        log(detail, 'err')
        throw setupManualError(
            'ollama-install-failed',
            detail,
            { offerLinuxInstall: true, downloadUrl: LINUX_OLLAMA_DOWNLOAD_URL },
        )
    }

    async function handleLinuxOllamaMissing(plan) {
        const invocation = ollamaInstallCommandToRun(plan)
        if (!invocation) {
            log('Ollama not found. Homebrew is not used on Linux.', 'info')
            if (plan && plan.instructions) log(plan.instructions, 'err')
            log(`Official install: ${LINUX_OLLAMA_INSTALL_COMMAND}`, 'info')
            log(`Download: ${LINUX_OLLAMA_DOWNLOAD_URL}`, 'info')
            throw linuxOllamaMissingError()
        }

        await ensureLinuxZstdAvailable()

        log('Ollama not found. Running the official Linux installer with your consent...', 'info')
        stepUpdate('ollama', 'running', 'Installing Ollama (sudo/admin prompt)...')
        log(`> ${invocation.command} ${invocation.args.join(' ')}`, 'dim')

        let install = await runCommand(invocation.command, invocation.args)
        if (install.stdout) log(install.stdout, 'dim')
        if (install.stderr) log(install.stderr, install.code === 0 ? 'dim' : 'err')
        if (install.code !== 0) {
            const missingPkexec = /not found|ENOENT|spawn pkexec/i.test(install.stderr || '')
            if (missingPkexec) {
                log('pkexec not available. Opening a visible terminal for the official installer...', 'info')
                stepUpdate('ollama', 'running', 'Installing Ollama in a terminal...')
                const script = `${LINUX_OLLAMA_INSTALL_COMMAND}; echo; echo Press Enter to return to Clippy; read`
                log(`> x-terminal-emulator -e bash -c "${LINUX_OLLAMA_INSTALL_COMMAND}"`, 'dim')
                install = await runCommand('x-terminal-emulator', ['-e', 'bash', '-c', script])
                if (install.code !== 0) {
                    log('Trying xterm...', 'info')
                    install = await runCommand('xterm', ['-e', 'bash', '-c', script])
                }
                if (install.stdout) log(install.stdout, 'dim')
                if (install.stderr) log(install.stderr, install.code === 0 ? 'dim' : 'err')
            }
            if (install.code !== 0) {
                log('Ollama installer did not succeed. Run the official command in a terminal, then retry.', 'err')
                await throwLinuxOllamaInstallFailure(
                    install,
                    `Ollama install failed. Run: ${LINUX_OLLAMA_INSTALL_COMMAND}`,
                )
            }
        }

        const combinedOutput = `${install.stdout || ''}\n${install.stderr || ''}`
        if (looksLikeMissingZstd(combinedOutput)) {
            await ensureLinuxZstdAvailable()
        }

        const verify = await verifyOllamaAfterInstall()
        if (verify.code !== 0) {
            const installDetail = summarizeCommandFailure(
                install,
                'Ollama install finished but ollama is not on PATH',
            )
            log(`Ollama installer finished, but ollama is still not on PATH. ${installDetail}`, 'err')
            throw setupManualError(
                'ollama-path',
                `${installDetail}. Open a new terminal, then retry.`,
                { offerLinuxInstall: true, downloadUrl: LINUX_OLLAMA_DOWNLOAD_URL },
            )
        }

        const verifiedVersion = verify.stdout || verify.stderr || 'Ollama is available.'
        log(verifiedVersion.split('\n')[0], 'ok')

        stepUpdate('ollama', 'running', 'Checking whether Ollama is reachable...')
        if (!(await ollamaReachable(15, 1000))) {
            log('Ollama is installed but not reachable at ' + configuredOllamaBaseURL(), 'err')
            throw setupManualError(
                'ollama-not-reachable',
                `Ollama installed but is not reachable at ${configuredOllamaBaseURL()}. Start it with ollama serve, then retry.`,
                { offerLinuxInstall: true, downloadUrl: LINUX_OLLAMA_DOWNLOAD_URL },
            )
        }

        markDone('ollama', verifiedVersion.split('\n')[0])
    }

    async function stepCheckOllama() {
        // Ollama is the only active provider in this release, so setup always
        // verifies the local runtime before downloading any model weights.
        stepUpdate('ollama', 'running', 'Checking for Ollama...')
        log('> ollama --version', 'dim')

        const found = await verifyWithKnownPaths(OLLAMA_COMMAND, runtimeOllamaPaths())
        if (found.code === 0) {
            const foundVersion = found.stdout || found.stderr || 'Ollama is available.'
            log(foundVersion.split('\n')[0], 'ok')
            markDone('ollama', foundVersion.split('\n')[0])
            return
        }

        if (process.platform === 'linux') {
            const plan = decideOllamaInstall({
                platform: 'linux',
                userConsent: Boolean(state.ollamaLinuxInstallConsented),
            })
            state.ollamaLinuxInstallConsented = false
            await handleLinuxOllamaMissing(plan)
            return
        }

        const plan = decideOllamaInstall({
            platform: process.platform,
            brewAvailable: process.platform === 'darwin' ? await brewIsAvailable() : undefined,
            wingetAvailable: process.platform === 'win32' ? await wingetIsAvailable() : undefined,
        })
        const invocation = installCommandToRun(plan)

        if (!invocation) {
            log(plan.instructions || 'Ollama was not found on PATH.', 'err')
            if (plan.downloadUrl) log(`Download: ${plan.downloadUrl}`, 'info')
            throw setupManualError(
                'ollama-install-required',
                plan.instructions || `Install Ollama from ${OLLAMA_DOWNLOAD_URL}, then retry.`,
                { downloadUrl: plan.downloadUrl || OLLAMA_DOWNLOAD_URL },
            )
        }

        if (plan.action === 'homebrew') {
            log('Ollama not found. Trying Homebrew...', 'info')
            stepUpdate('ollama', 'running', 'Installing Ollama via Homebrew...')
            const installed = await installWithBrew('ollama')
            if (installed) {
                const verify = await verifyOllamaAfterInstall()
                const verifiedVersion = verify.stdout || verify.stderr
                if (verify.code === 0) {
                    log((verifiedVersion || 'Ollama is available.').split('\n')[0], 'ok')
                    markDone('ollama', (verifiedVersion || 'Ollama is available.').split('\n')[0])
                    return
                }
            }
            log(`Ollama was not found on PATH. Install it from ${OLLAMA_DOWNLOAD_URL}, then retry.`, 'err')
            throw setupManualError(
                'ollama-install-required',
                `Install Ollama from ${OLLAMA_DOWNLOAD_URL}, then retry.`,
                { downloadUrl: OLLAMA_DOWNLOAD_URL },
            )
        }

        log('Ollama not found. Installing via winget...', 'info')
        stepUpdate('ollama', 'running', 'Installing Ollama via winget...')
        log('> winget install Ollama.Ollama --silent', 'dim')

        const install = await runCommand(invocation.command, invocation.args)

        if (install.code !== 0) {
            log(install.stderr || 'winget failed', 'err')
            log(`Install Ollama from ${OLLAMA_DOWNLOAD_URL}, then retry.`, 'err')
            throw setupManualError(
                'ollama-install-failed',
                `Could not install Ollama via winget. Install it from ${OLLAMA_DOWNLOAD_URL}, then retry.`,
                { downloadUrl: OLLAMA_DOWNLOAD_URL },
            )
        }

        const verify = await verifyOllamaAfterInstall()
        if (verify.code !== 0) {
            throw setupManualError(
                'ollama-path',
                `Ollama installed but is not on PATH. Install location was probed; if it still fails, install from ${OLLAMA_DOWNLOAD_URL} and retry.`,
                { downloadUrl: OLLAMA_DOWNLOAD_URL },
            )
        }

        const verifiedVersion = verify.stdout || verify.stderr || 'Ollama is available.'
        log(verifiedVersion.split('\n')[0], 'ok')
        markDone('ollama', verifiedVersion.split('\n')[0])
    }

    async function stepStartOllamaService() {
        // Capture is model-free, so Ollama only needs room for the text model.
        stepUpdate('ollama-service', 'running', 'Configuring & starting Ollama...')
        log('> ollama serve', 'dim')


        log('Setting OLLAMA_MAX_LOADED_MODELS=1...', 'info')
        await ensureOllamaParallelConfig({ persist: true })

        log('Waiting for Ollama service...', 'dim')
        stepProgress('ollama-service', -1)

        // Reuses a running server and starts one only when the port is free, so
        // setup can never trigger a bind conflict with the Ollama tray app.
        if (await ensureOllamaServing({ waitTries: 30 })) {
            log('Ollama ready.', 'ok')
            markDone('ollama-service', 'Ollama service running')
            return
        }

        stepUpdate('ollama-service', 'error', 'Ollama service did not start in time.')
        log('Ollama did not become reachable on ' + configuredOllamaBaseURL(), 'err')
        throw new Error('ollama-service-timeout')
    }

    async function probeVenvPython(venvPy) {
        const probe = await runCommand(venvPy, ['-c', 'import sys; print(sys.executable)'])
        return probe.code === 0
    }

    async function ensureAppPythonVenv() {
        // Install into <userData>/python-venv so Debian/Ubuntu PEP 668 hosts never
        // need --break-system-packages, and so every platform shares one runtime.
        const dir = venvDir()
        const basePython = systemPythonCommand()
        let venvPy = venvPythonPath(dir)

        if (isHealthyVenv(dir) && await probeVenvPython(venvPy)) {
            log(`Reusing Python venv at ${dir}`, 'ok')
            paths.PYTHON_COMMAND = venvPy
            return venvPy
        }

        if (fs.existsSync(dir)) {
            log('Existing Python venv looks broken; recreating...', 'info')
            try {
                fs.rmSync(dir, { recursive: true, force: true })
            } catch (err) {
                log(`Could not remove broken venv: ${err.message}`, 'err')
            }
        }

        stepUpdate('packages', 'running', 'Creating Python virtual environment...')
        log(`> ${basePython} -m venv ${dir}`, 'dim')
        const created = await runCommand(basePython, ['-m', 'venv', dir])
        if (created.stdout) log(created.stdout, 'dim')
        if (created.stderr) log(created.stderr, created.code === 0 ? 'dim' : 'err')

        venvPy = venvPythonPath(dir)
        if (created.code !== 0 || !(await probeVenvPython(venvPy))) {
            const combined = `${created.stdout || ''}\n${created.stderr || ''}`
            if (looksLikeMissingPythonVenvPackage(combined)) {
                const message = missingPythonVenvPackageMessage()
                log(message, 'err')
                throw setupManualError('python-venv-missing', message)
            }
            const detail = summarizeCommandFailure(created, 'Failed to create Python virtual environment')
            log(detail, 'err')
            throw setupManualError('python-venv-failed', detail)
        }

        log(`Python venv ready at ${dir}`, 'ok')
        paths.PYTHON_COMMAND = venvPy
        return venvPy
    }

    async function stepInstallPackages() {
        // pip output is streamed to the onboarding log while the renderer receives
        // an approximate progress value for long installs. Packages always go into
        // the app venv — never the system interpreter.
        stepUpdate('packages', 'running', 'Preparing Python environment...')
        stepProgress('packages', -1)
        const venvPy = await ensureAppPythonVenv()

        stepUpdate('packages', 'running', 'Installing Python packages...')
        log(`> ${venvPy} -m pip install -r requirements.txt`, 'dim')

        return new Promise((resolve, reject) => {
            const proc = spawnHidden(venvPy, ['-m', 'pip', 'install', '-r', REQUIREMENTS], { cwd: ROOT })

            const PKG_TOTAL = 20
            let installed = 0
            let pipLog = ''

            proc.stdout.on('data', (chunk) => {
                const text = chunk.toString()
                pipLog += text
                const lines = text.split('\n').filter(l => l.trim())
                for (const line of lines) {
                    log(line, line.toLowerCase().includes('error') ? 'err' : 'dim')
                    if (line.startsWith('Installing') || line.startsWith('Successfully installed')) {
                        installed++
                        stepProgress('packages', Math.min(95, Math.round((installed / PKG_TOTAL) * 100)))
                        const match = line.match(/Installing collected packages:\s*(.+)/)
                        if (match) {
                            stepUpdate('packages', 'running', `Installing ${match[1].split(',')[0].trim()}...`)
                        }
                    }
                }
            })

            proc.stderr.on('data', (chunk) => {
                const text = chunk.toString()
                pipLog += text
                const lines = text.split('\n').filter(l => l.trim())
                for (const line of lines) {
                    const isError = line.toLowerCase().startsWith('error')
                    log(line, isError ? 'err' : 'dim')
                }
            })

            proc.on('exit', (code) => {
                if (code === 0) {
                    stepProgress('packages', 100)
                    log('All packages installed.', 'ok')
                    markDone('packages', 'All packages installed')
                    resolve()
                    return
                }
                if (/externally-managed-environment/i.test(pipLog)) {
                    const message = 'pip refused an externally-managed environment. Clippy should install into its app venv — retry setup, or recreate the venv.'
                    stepUpdate('packages', 'error', message)
                    reject(setupManualError('pip-externally-managed', message))
                    return
                }
                const detail = summarizeCommandFailure(
                    { code, stderr: pipLog },
                    'pip install failed. Check the log.',
                )
                stepUpdate('packages', 'error', detail)
                reject(setupManualError('pip-failed', detail))
            })

            proc.on('error', (e) => {
                stepUpdate('packages', 'error', e.message)
                reject(e)
            })
        })
    }

    async function stepPullModels() {
        // Download MiniLM embeddings from Hugging Face, then pull the user's local AI model
        // from Ollama. Capture uses accessibility + OCR (no vision model).
        stepUpdate('models', 'running', 'Downloading local ML models (MiniLM)...')
        log('> python -m core.model_download', 'dim')
        try {
            const { code, stdout, stderr } = await runCommand(pythonCommand(), ['-m', 'core.model_download'], { cwd: ROOT })
            if (stdout) log(stdout, 'dim')
            if (stderr) log(stderr, code === 0 ? 'dim' : 'err')
            if (code !== 0) {
                log('HF model download failed — embeddings will retry on first use.', 'err')
            } else {
                log('Local ML models ready.', 'ok')
            }
        } catch (error) {
            log(`HF model download error: ${error.message}`, 'err')
        }

        const chatModel = String(readLLMConfig().chat_model || '').trim()
        if (!chatModel) {
            stepProgress('models', 100)
            log('No local AI model selected — skipping Ollama pull.', 'info')
            markDone('models', 'Local ML models ready (no model pull)')
            return
        }

        const models = [{ name: chatModel, label: chatModel }]

        stepUpdate('models', 'running', `Checking for ${chatModel}...`)
        log('> ollama list', 'dim')

        const { stdout: listOut } = await runCommand(OLLAMA_COMMAND, ['list'])
        log(listOut || '(no models yet)', 'dim')

        const hasModel = (name) => ollamaListHasModel(listOut, name)
        const needed = models.filter(m => !hasModel(m.name))
        const alreadyHave = models.filter(m => hasModel(m.name))

        for (const m of alreadyHave) {
            log(`Already have ${m.name} — skipping.`, 'ok')
        }

        if (needed.length === 0) {
            stepProgress('models', 100)
            log('Selected local AI model already downloaded.', 'ok')
            markDone('models', `${chatModel} ready`)
            return
        }

        for (let i = 0; i < needed.length; i++) {
            const model = needed[i]
            stepUpdate('models', 'running', `Downloading ${model.label}...`)
            log(`> python -m core.ollama_pull ${model.name}`, 'dim')
            stepProgress('models', -1)

            await new Promise((resolve, reject) => {
                const proc = spawnHidden(pythonCommand(), ['-m', 'core.ollama_pull', model.name], { cwd: ROOT })
                let lastError = null
                let lineBuffer = ''
                let lastLoggedStatus = ''
                let lastLoggedPercent = -10

                proc.stdout.on('data', (chunk) => {
                    // Buffer partial lines across chunks
                    lineBuffer += chunk.toString()
                    const lines = lineBuffer.split('\n')
                    // Keep the last partial line in the buffer
                    lineBuffer = lines.pop() || ''
                    
                    for (const line of lines) {
                        if (!line.trim()) continue
                        
                        try {
                            const progress = JSON.parse(line)
                            
                            // Check for errors
                            if (progress.error) {
                                lastError = progress.error
                                log(`Error: ${progress.error}`, 'err')
                                continue
                            }
                            
                            // Update progress bar
                            if (progress.percent !== undefined) {
                                stepProgress('models', Math.round(progress.percent))
                            }
                            
                            // Update status text with MB progress
                            if (progress.downloaded_mb !== undefined && progress.total_mb > 0) {
                                const downloadedMB = Math.round(progress.downloaded_mb)
                                const totalMB = Math.round(progress.total_mb)
                                const pct = Math.round(progress.percent)
                                stepUpdate('models', 'running', `${model.label}: ${pct}% (${downloadedMB} / ${totalMB} MB)`)
                            } else {
                                stepUpdate('models', 'running', `${model.label}: ${progress.status}`)
                            }
                            
                            // Throttle logging: only log on status changes or every ~10%
                            const pct = Math.round(progress.percent)
                            const statusChanged = progress.status !== lastLoggedStatus
                            const percentJump = pct - lastLoggedPercent >= 10
                            
                            if (statusChanged || percentJump || progress.complete || progress.error) {
                                lastLoggedStatus = progress.status
                                lastLoggedPercent = pct
                                
                                // Only show MB when total is known
                                if (progress.total_mb > 0) {
                                    log(`${progress.status} - ${Math.round(progress.downloaded_mb)} / ${Math.round(progress.total_mb)} MB`, 'dim')
                                } else {
                                    log(progress.status, 'dim')
                                }
                            }
                        } catch (e) {
                            // Not JSON, just log it
                            log(line, 'dim')
                        }
                    }
                })

                proc.stderr.on('data', (chunk) => {
                    chunk.toString().split('\n').filter(l => l.trim()).forEach(l => log(l, 'err'))
                })

                proc.on('exit', (code) => {
                    // Process any remaining buffered line
                    if (lineBuffer.trim()) {
                        try {
                            const progress = JSON.parse(lineBuffer)
                            if (progress.error) {
                                lastError = progress.error
                            }
                        } catch (e) {
                            // Ignore parse errors on final flush
                        }
                    }
                    
                    if (code === 0) {
                        log(`${model.name} ready.`, 'ok')
                        resolve()
                    } else {
                        const errorMsg = lastError || `Model pull failed with exit code ${code}`
                        reject(new Error(errorMsg))
                    }
                })

                proc.on('error', reject)
            })
        }

        stepProgress('models', 100)
        markDone('models', `${chatModel} downloaded`)
    }

    async function stepWarmup() {
        // Warm the configured local AI model during onboarding for a fast first reply.
        const chatModel = readLLMConfig().chat_model
        stepUpdate('warmup', 'running', 'Loading models into memory...')
        stepProgress('warmup', -1)

        log('Starting API server for warmup...', 'dim')
        await startServer()

        try {
            await pollUntilAlive(apiUrl('/health'), 1000, 90)
            log('API server ready.', 'ok')
        } catch (_) {
            log('API server slow to start — continuing anyway.', 'info')
        }

        // Ensure Ollama is reachable before asking the API to load weights.
        try {
            await pollUntilAlive(configuredOllamaBaseURL(), 1000, 30)
        } catch (_) {
            log('Ollama not responding — skipping model warm.', 'info')
        }


        // Capture uses accessibility text with OCR fallback and warms no model.
        log('Warming the local AI model...', 'info')
        stepUpdate('warmup', 'running', `Loading ${chatModel}...`)
        try {
            await httpPost(apiUrl('/residency/startup'), {}, 120000)
            log('Local AI model ready — capture remains model-free.', 'ok')
        } catch (e) {
            log(`Model warm skipped or timed out (${e.message}) — continuing.`, 'info')

            // Leave an explicit idle marker so a later capture can retry the warm.
            try {
                fs.writeFileSync(RESIDENCY_FILE, JSON.stringify({
                    vision: 'idle',
                    reason: 'warmup_timeout',
                    updated_at: new Date().toISOString(),
                }, null, 2))
            } catch (_) {              }
        }

        const dirs = [
            DATA_DIR,
            path.join(DATA_DIR, 'screenshots'),
            path.join(USER_DATA, 'logs'),
        ]
        for (const d of dirs) {
            if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true })
        }

        writeSetupFlag()

        log('Setup complete!', 'ok')
        markDone('warmup', 'Ready!')
    }

    const stepFns = {
        'python':         stepCheckPython,
        'ollama':         stepCheckOllama,
        'ollama-service': stepStartOllamaService,
        'packages':       stepInstallPackages,
        'models':         stepPullModels,
        'warmup':         stepWarmup,
    }

    async function runSetup(startFrom = 'python') {
        // Retry starts at the failed step, while a fresh install runs the complete
        // ordered chain from Python discovery through model warmup.
        const order = usesManagedOllama()
            ? ['python', 'ollama', 'ollama-service', 'packages', 'models', 'warmup']
            : ['python', 'packages', 'warmup']
        state.setupStepTotal = order.length
        const requestedIndex = order.indexOf(startFrom)
        const startIdx = requestedIndex >= 0 ? requestedIndex : 0

        for (let i = startIdx; i < order.length; i++) {
            const key = order[i]
            try {
                await stepFns[key]()
            } catch (err) {
                console.error(`[setup] step "${key}" failed:`, err.message)
                const extra = {}
                if (err && err.offerLinuxInstall) extra.offerLinuxInstall = true
                if (err && err.downloadUrl) extra.downloadUrl = err.downloadUrl
                stepUpdate(key, 'error', (err && err.stepSub) || err.message, extra)
                // Stop here so the user can retry the failed step from onboarding.
                return
            }
        }



        // Give the renderer time to paint the final step before switching screens.
        setTimeout(() => sendSetup('setup-complete'), 800)
    }

    async function runPreflightChecks() {
        // Every normal launch verifies the local runtime before starting the API,
        // which turns missing models or permissions into a recoverable setup step.
        const managedOllama = usesManagedOllama()
        const alreadyConfigured = process.env.OLLAMA_MAX_LOADED_MODELS === OLLAMA_MAX_LOADED_MODELS
        if (managedOllama) {
            await ensureOllamaParallelConfig({ persist: !alreadyConfigured })
        }

        const py = await runCommand(pythonCommand(), ['--version'])
        if (py.code !== 0) {
            // Fall back to system Python so a missing/broken venv still surfaces a
            // recoverable "packages" step instead of a hard Python failure.
            const systemPy = await runCommand(systemPythonCommand(), ['--version'])
            if (systemPy.code !== 0) {
                return describePreflightFailure('python', 'Python not found or not on PATH.')
            }
            const systemCheck = evaluatePythonVersion(systemPy.stdout || systemPy.stderr)
            if (!systemCheck.ok) {
                return describePreflightFailure('python', systemCheck.message)
            }
            return describePreflightFailure('packages', 'Python virtual environment is missing or broken.')
        }
        const pyCheck = evaluatePythonVersion(py.stdout || py.stderr)
        if (!pyCheck.ok) {
            return describePreflightFailure('python', pyCheck.message)
        }

        if (!managedOllama) {
            if (!(await ollamaReachable(3, 500))) {
                return describePreflightFailure('warmup', 'The configured local API is not reachable.')
            }
        } else {
            const ol = await runCommand(OLLAMA_COMMAND, ['--version'])
            if (ol.code !== 0) {
                return describePreflightFailure('ollama', 'Ollama not found or not on PATH.')
            }

            // Residency limits apply to whichever server owns the port; a running
            // server is adopted as-is rather than restarted, because killing it
            // starts a bind war with the Ollama tray app.
            if (!(await ensureOllamaServing({ waitTries: 20 }))) {
                return describePreflightFailure('ollama-service', 'Ollama service could not be started.')
            }
        }

        const missing = []
        for (const name of requiredChatModels()) {
            if (!(await ollamaHasModel(name))) missing.push(name)
        }
        if (missing.length > 0) {
            return describePreflightFailure('models', `Missing models: ${missing.join(', ')}`)
        }

        // Import checks are a cheap proxy for the full capture dependency set.
        const pkgCheck = await runCommand(pythonCommand(), [
            '-c',
            'import fastapi, uvicorn, pynput, mss, PIL, psutil, imagehash, transformers, torch',
        ])
        if (pkgCheck.code !== 0) {
            return describePreflightFailure('packages', 'One or more Python packages are missing.')
        }

        return { ok: true, step: null, reason: null, title: null, detail: null }
    }

    function writeSetupFlag(extra = {}) {
        let previous = {}
        try {
            if (fs.existsSync(SETUP_FLAG)) {
                previous = JSON.parse(fs.readFileSync(SETUP_FLAG, 'utf8'))
            }
        } catch (_) {}

        fs.writeFileSync(SETUP_FLAG, JSON.stringify({
            ...previous,
            ...extra,
            version: app.getVersion(),
            completedAt: previous.completedAt || new Date().toISOString(),
        }, null, 2))
    }

    /** Keep setup_complete.json version in sync after upgrades without re-running setup. */
    function syncSetupFlagVersion() {
        if (!fs.existsSync(SETUP_FLAG)) return
        try {
            const raw = JSON.parse(fs.readFileSync(SETUP_FLAG, 'utf8'))
            if (raw.version === app.getVersion()) return
            console.log(`[setup] Version changed ${raw.version || '?'} → ${app.getVersion()}; updating setup flag`)
            writeSetupFlag({ upgradedAt: new Date().toISOString() })
        } catch (err) {
            console.warn('[setup] Could not read setup flag; rewriting:', err.message)
            writeSetupFlag()
        }
    }

    function redirectToSetup(fromStep, recovery = null) {
        try { fs.unlinkSync(SETUP_FLAG) } catch (_) {}
        state.setupStartFrom = fromStep || 'python'
        state.setupRecovery = recovery && typeof recovery === 'object'
            ? { ...recovery, step: state.setupStartFrom }
            : describePreflightFailure(state.setupStartFrom)
        windowBridge.closeMainAndTray()
        windowBridge.createSetupWindow()
    }

    function registerSetupIpc(ipcMain, getHardwareCheck) {
        ipcMain.handle('confirm-hardware-and-start', async (_event, { override } = {}) => {
            const check = await getHardwareCheck()
            if (check.level === 'block') {
                return { ok: false, reason: 'below_minimum', check }
            }
            if (check.level === 'warn' && !override) {
                return { ok: false, reason: 'override_required', check }
            }
            if (state.setupInstallStarted) {
                return { ok: true, alreadyStarted: true }
            }
            state.setupInstallStarted = true
            state.doneSoFar = 0

            setImmediate(() => runSetup(state.setupStartFrom))
            return { ok: true, check }
        })

        ipcMain.handle('retry-step', async (_event, keyOrOpts) => {
            const payload = typeof keyOrOpts === 'string' || keyOrOpts == null
                ? { key: keyOrOpts }
                : keyOrOpts
            const key = payload.key
            const userConsent = Boolean(payload.userConsent)
            if (key === 'ollama' && userConsent && process.platform === 'linux') {
                const confirmed = await confirmLinuxOllamaInstall()
                if (!confirmed) return { ok: false, cancelled: true }
                state.ollamaLinuxInstallConsented = true
            } else if (key === 'ollama') {
                state.ollamaLinuxInstallConsented = false
            }
            state.doneSoFar = Math.max(0, state.doneSoFar - 1)
            runSetup(key)
            return { ok: true }
        })

        ipcMain.handle('open-setup-download', (_event, url) => {
            const value = String(url || '')
            if (!ALLOWED_SETUP_DOWNLOAD_URLS.has(value)) return false
            if (!shell || typeof shell.openExternal !== 'function') return false
            return shell.openExternal(value)
        })

        ipcMain.handle('open-ollama-linux-download', () => {
            if (!shell || typeof shell.openExternal !== 'function') return false
            return shell.openExternal(LINUX_OLLAMA_DOWNLOAD_URL)
        })

        ipcMain.handle('get-setup-context', () => ({
            recover: Boolean(state.setupRecovery),
            fromStep: state.setupStartFrom,
            title: state.setupRecovery && state.setupRecovery.title,
            detail: state.setupRecovery && state.setupRecovery.detail,
        }))

        ipcMain.handle('start-recovery-setup', () => {
            if (state.setupInstallStarted) {
                return { ok: true, alreadyStarted: true }
            }
            state.setupInstallStarted = true
            state.doneSoFar = 0
            setImmediate(() => runSetup(state.setupStartFrom))
            return { ok: true }
        })

        ipcMain.handle('check-runtime-health', async (_event, rawError) => {
            const check = await runPreflightChecks()
            if (!check.ok) return check
            return classifyChatError(rawError) || { ok: true, step: null, reason: null, title: null, detail: null }
        })

        ipcMain.handle('fix-runtime-issue', (_event, payload = {}) => {
            const issue = payload && typeof payload === 'object' ? payload : {}
            const step = issue.step || 'models'
            redirectToSetup(step, {
                title: issue.title,
                detail: issue.detail,
                reason: issue.reason,
                step,
            })
            return { ok: true }
        })

        ipcMain.handle('launch-app', () => {
            state.setupRecovery = null
            windowBridge.launchFromSetup()
            if (typeof onLaunched === 'function') onLaunched()
        })
    }

    return {
        runSetup,
        runPreflightChecks,
        describePreflightFailure,
        classifyChatError,
        writeSetupFlag,
        syncSetupFlagVersion,
        redirectToSetup,
        registerSetupIpc,
    }
}

module.exports = {
    createSetup,
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
    LINUX_OLLAMA_INSTALL_COMMAND,
    LINUX_OLLAMA_DOWNLOAD_URL,
    OLLAMA_DOWNLOAD_URL,
    PYTHON_DOWNLOAD_URL,
}
