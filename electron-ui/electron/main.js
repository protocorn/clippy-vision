const {
    app, BrowserWindow, Tray, Menu,
    nativeImage, ipcMain, Notification, shell, dialog, globalShortcut
} = require('electron')
const { spawn }  = require('child_process')
const path       = require('path')
const fs         = require('fs')
const http       = require('http')
const https      = require('https')
const net        = require('net')
const os         = require('os')

// Keep one desktop process alive so a second launch focuses the existing tray app.
if (!app.requestSingleInstanceLock()) {
    app.quit()
    process.exit(0)
}
// Packaged builds keep mutable state in Electron's user-data directory. During
// development the repository data directory is used so local runs behave the
// same way without copying state into an installation folder.
const IS_PACKAGED     = app.isPackaged
const ROOT            = IS_PACKAGED
    ? path.join(process.resourcesPath, 'clippy')
    : path.join(__dirname, '../..')
const USER_DATA       = IS_PACKAGED ? app.getPath('userData') : ROOT
const DATA_DIR        = IS_PACKAGED
    ? path.join(USER_DATA, 'data')
    : path.join(ROOT, 'core', 'data')
const ASSETS          = path.join(__dirname, '../assets')
const ICON_INACTIVE   = path.join(ASSETS, 'logo_inactive.png')
const ICON_ACTIVE     = path.join(ASSETS, 'logo_active.png')
const CAPTURE_SCRIPT  = path.join(ROOT, 'core', 'screen_capture.py')
const API_SCRIPT      = path.join(ROOT, 'api_server.py')
const REQUIREMENTS    = path.join(ROOT, 'requirements.txt')
const SETUP_FLAG      = path.join(USER_DATA, 'setup_complete.json')
const RESIDENCY_FILE  = path.join(DATA_DIR, 'model_residency.json')
const CAPTURE_STATE_FILE = path.join(DATA_DIR, 'capture_status.json')
const LLM_CONFIG_FILE = path.join(DATA_DIR, 'llm_config.json')
const API_STATE_FILE  = path.join(USER_DATA, 'api_process.json')
const DESKTOP_SETTINGS_FILE = path.join(USER_DATA, 'desktop_settings.json')
// Electron does not always inherit the interactive shell PATH. These common
// macOS locations cover Homebrew, npm/pnpm, Volta, and nvm installations.
const PATH_HINTS = process.platform === 'darwin'
    ? [
        '/opt/homebrew/bin',
        '/usr/local/bin',
        path.join(os.homedir(), '.local/bin'),
        path.join(os.homedir(), '.npm-global/bin'),
        path.join(os.homedir(), 'Library/pnpm'),
        path.join(os.homedir(), '.volta/bin'),
        path.join(os.homedir(), '.nvm/current/bin'),
    ]
    : []

function commandFromKnownPaths(name, fallback) {
    // Prefer an explicitly discoverable executable before falling back to the
    // shell name, which lets spawn() still resolve system installations.
    const candidate = PATH_HINTS.map((dir) => path.join(dir, name)).find((file) => fs.existsSync(file))
    return candidate || fallback
}

const PYTHON_COMMAND = process.env.CLIPPY_PYTHON || commandFromKnownPaths(
    'python3',
    process.platform === 'win32' ? 'python' : 'python3',
)
const OLLAMA_COMMAND = process.env.CLIPPY_OLLAMA || commandFromKnownPaths('ollama', 'ollama')
// 8000 collides with the dev servers Clippy's own audience tends to run, so the
// API claims a free loopback port at launch and every caller resolves it here.
const DEFAULT_API_PORT = 8000
let apiPort = Number(process.env.CLIPPY_API_PORT) || 0
const OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
const GEMINI_API_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta/openai'
const GEMINI_DEFAULT_MODEL = 'gemini-2.5-flash'
const RELEASE_REPOSITORY = 'protocorn/clippy-vision'
const LOCAL_EMBEDDING_MODEL = 'local:sentence-transformers/all-MiniLM-L6-v2'
// External providers are opt-in. Their prompts can include retrieved activity
// context, while the activity database and embedding requests remain local.
const SUBSCRIPTION_PROVIDERS = new Set(['codex_cli', 'claude_cli'])
const CLI_DEFAULTS = {
    codex_cli: { command: 'codex', login_args: ['login'], chat_model: 'default', vision_model: 'default' },
    claude_cli: { command: 'claude', login_args: [], chat_model: 'sonnet', vision_model: 'sonnet' },
}
const OLLAMA_MAX_LOADED_MODELS = '1'
const OLLAMA_NUM_PARALLEL = '1'

const DEFAULT_LLM_CONFIG = {
    provider: 'ollama',
    base_url: OLLAMA_BASE_URL,
    api_key: '',
    cli_command: '',
    chat_model: 'qwen3:8b',
    vision_model: 'qwen3-vl:4b',
    // Capture uses accessibility + OCR; no vision model is downloaded or required.
    embedding_model: LOCAL_EMBEDDING_MODEL,
}

function normalizeLLMConfig(values = {}) {
    const merged = { ...DEFAULT_LLM_CONFIG, ...values }
    const requested = String(merged.provider || 'ollama').trim().toLowerCase()
    // Accept stable aliases from environment variables and older hand-edited
    // config files, then persist one canonical provider ID.
    const aliases = {
        openai: 'openai_compatible',
        'openai-compatible': 'openai_compatible',
        openai_compatible: 'openai_compatible',
        local: 'openai_compatible',
        'local-api': 'openai_compatible',
        local_api: 'openai_compatible',
        codex: 'codex_cli',
        'codex-cli': 'codex_cli',
        codex_cli: 'codex_cli',
        claude: 'claude_cli',
        'claude-code': 'claude_cli',
        claude_cli: 'claude_cli',
        gemini: 'gemini_api',
        'gemini-api': 'gemini_api',
        gemini_api: 'gemini_api',
    }
    merged.provider = aliases[requested] || (requested === 'ollama' ? 'ollama' : 'ollama')
    if (!values.base_url || (merged.provider === 'gemini_api' && merged.base_url === 'cli://local')) {
        merged.base_url = SUBSCRIPTION_PROVIDERS.has(merged.provider)
            ? 'cli://local'
            : merged.provider === 'gemini_api'
            ? GEMINI_API_BASE_URL
            : merged.provider === 'openai_compatible'
            ? 'http://127.0.0.1:1234/v1'
            : OLLAMA_BASE_URL
    }
    merged.base_url = String(merged.base_url || '').trim().replace(/\/+$/, '')
    merged.api_key = String(merged.api_key || '').trim()
    merged.cli_command = String(merged.cli_command || '').trim()
    for (const field of ['chat_model', 'vision_model']) {
        merged[field] = String(merged[field] || DEFAULT_LLM_CONFIG[field]).trim()
    }
    if (SUBSCRIPTION_PROVIDERS.has(merged.provider)) {
        const defaults = CLI_DEFAULTS[merged.provider]
        if (!values.chat_model || ['qwen3:8b', ''].includes(merged.chat_model)) merged.chat_model = defaults.chat_model
        if (!values.vision_model || ['qwen3-vl:4b', ''].includes(merged.vision_model)) merged.vision_model = defaults.vision_model
        if (!merged.cli_command) merged.cli_command = defaults.command
    }
    if (merged.provider === 'gemini_api') {
        if (!values.chat_model || ['qwen3:8b', 'auto', ''].includes(merged.chat_model)) merged.chat_model = GEMINI_DEFAULT_MODEL
        if (!values.vision_model || ['qwen3-vl:4b', 'auto', ''].includes(merged.vision_model)) merged.vision_model = GEMINI_DEFAULT_MODEL
        merged.cli_command = ''
    }
    // Bundled MiniLM keeps retrieval local for every chat provider.
    merged.embedding_model = LOCAL_EMBEDDING_MODEL
    return merged
}

function validateLLMConfig(values = {}) {
    for (const field of ['base_url', 'chat_model', 'vision_model']) {
        if (Object.prototype.hasOwnProperty.call(values, field) && !String(values[field] || '').trim()) {
            throw new Error(`${field} cannot be empty.`)
        }
    }
    if (!SUBSCRIPTION_PROVIDERS.has(values.provider) && !/^https?:\/\/[^\s]+$/i.test(values.base_url)) {
        throw new Error('Base URL must be a valid HTTP or HTTPS URL.')
    }
    if (SUBSCRIPTION_PROVIDERS.has(values.provider) && values.cli_command.length > 240) {
        throw new Error('CLI command is too long.')
    }
    for (const field of ['chat_model', 'vision_model']) {
        if (values[field].length > 240) throw new Error(`${field} is too long.`)
    }
    return values
}

function readLLMConfig() {
    let saved = {}
    try { saved = JSON.parse(fs.readFileSync(LLM_CONFIG_FILE, 'utf8')) || {} } catch (_) {                 }
    const env = {
        provider: process.env.CLIPPY_LLM_PROVIDER,
        base_url: process.env.CLIPPY_LLM_BASE_URL,
        api_key: process.env.CLIPPY_LLM_API_KEY,
        cli_command: process.env.CLIPPY_CLI_COMMAND,
        chat_model: process.env.CLIPPY_CHAT_MODEL,
        vision_model: process.env.CLIPPY_VISION_MODEL,
    }
    return normalizeLLMConfig({ ...saved, ...Object.fromEntries(Object.entries(env).filter(([, value]) => value)) })
}

function publicLLMConfig() {
    const config = readLLMConfig()
    const { api_key: _apiKey, ...safe } = config
    const environment_overrides = Object.entries({
        provider: 'CLIPPY_LLM_PROVIDER',
        base_url: 'CLIPPY_LLM_BASE_URL',
        api_key: 'CLIPPY_LLM_API_KEY',
        cli_command: 'CLIPPY_CLI_COMMAND',
        chat_model: 'CLIPPY_CHAT_MODEL',
        vision_model: 'CLIPPY_VISION_MODEL',
    }).filter(([, envName]) => String(process.env[envName] || '').trim()).map(([field]) => field)
    return { ...safe, api_key_set: Boolean(config.api_key), environment_overrides }
}

function saveLLMConfig(values = {}) {
    if (Object.prototype.hasOwnProperty.call(values, 'provider')) {
        const provider = String(values.provider || '').trim().toLowerCase()
        const validProviders = new Set(['ollama', 'openai', 'openai-compatible', 'openai_compatible', 'local', 'local-api', 'local_api', 'gemini', 'gemini-api', 'gemini_api', ...SUBSCRIPTION_PROVIDERS])
        if (!validProviders.has(provider)) throw new Error('Unsupported AI provider.')
    }
    const current = readLLMConfig()
    const targetProvider = Object.prototype.hasOwnProperty.call(values, 'provider')
        ? normalizeLLMConfig({ ...current, provider: values.provider }).provider
        : current.provider
    const providerChanged = Object.prototype.hasOwnProperty.call(values, 'provider') &&
        targetProvider !== current.provider
    const nextValues = { ...current, ...values }
    if (providerChanged && !Object.prototype.hasOwnProperty.call(values, 'base_url')) {
        nextValues.base_url = SUBSCRIPTION_PROVIDERS.has(targetProvider)
            ? 'cli://local'
            : targetProvider === 'gemini_api'
            ? GEMINI_API_BASE_URL
            : targetProvider === 'ollama'
            ? OLLAMA_BASE_URL
            : 'http://127.0.0.1:1234/v1'
    }
    if (providerChanged && !Object.prototype.hasOwnProperty.call(values, 'api_key')) nextValues.api_key = ''
    if (providerChanged && !Object.prototype.hasOwnProperty.call(values, 'cli_command')) nextValues.cli_command = ''
    if (providerChanged) {
        const defaults = {
            ollama: ['qwen3:8b', 'qwen3-vl:4b'],
            openai_compatible: ['local-chat', 'local-vision'],
            gemini_api: [GEMINI_DEFAULT_MODEL, GEMINI_DEFAULT_MODEL],
            codex_cli: [CLI_DEFAULTS.codex_cli.chat_model, CLI_DEFAULTS.codex_cli.vision_model],
            claude_cli: [CLI_DEFAULTS.claude_cli.chat_model, CLI_DEFAULTS.claude_cli.vision_model],
        }[targetProvider]
        if (!Object.prototype.hasOwnProperty.call(values, 'chat_model')) nextValues.chat_model = defaults[0]
        if (!Object.prototype.hasOwnProperty.call(values, 'vision_model')) nextValues.vision_model = defaults[1]
    }
    const next = normalizeLLMConfig(nextValues)

    if (!providerChanged && !String(values.api_key || '').trim() && current.api_key) next.api_key = current.api_key
    validateLLMConfig(next)
    fs.mkdirSync(DATA_DIR, { recursive: true })
    fs.writeFileSync(LLM_CONFIG_FILE, JSON.stringify(next, null, 2) + '\n', { mode: 0o600 })
    if (process.platform !== 'win32') fs.chmodSync(LLM_CONFIG_FILE, 0o600)
    return publicLLMConfig()
}

function isExternalProvider() {
    return readLLMConfig().provider !== 'ollama'
}

function isSubscriptionProvider(provider = readLLMConfig().provider) {
    return SUBSCRIPTION_PROVIDERS.has(provider)
}

function providerDisplayName(provider = readLLMConfig().provider) {
    return {
        codex_cli: 'Codex subscription',
        claude_cli: 'Claude subscription',
        gemini_api: 'Google Gemini API',
        openai_compatible: 'OpenAI-compatible API',
        ollama: 'Ollama',
    }[provider] || provider
}

function shellQuote(value) {
    return `'${String(value).replace(/'/g, "'\\''")}'`
}

function windowsQuote(value) {
    const text = String(value)
    return /[\s"]/u.test(text) ? `"${text.replace(/(["\\])/gu, '\\$1')}"` : text
}

function openProviderAuth(provider) {
    // Authentication stays inside each provider's official CLI. Electron only
    // opens a terminal and never reads or stores subscription credentials.
    if (!isSubscriptionProvider(provider)) {
        return { ok: false, error: 'Choose a subscription CLI provider first.' }
    }
    const metadata = CLI_DEFAULTS[provider]
    const config = readLLMConfig()
    const executable = String(config.cli_command || metadata.command).trim()
    if (!executable) return { ok: false, error: 'No CLI command is configured.' }
    const args = metadata.login_args || []
    let commandLine
    if (process.platform === 'win32') {
        commandLine = [executable, ...args].map(windowsQuote).join(' ')
        const terminal = spawn(process.env.ComSpec || 'cmd.exe', ['/d', '/k', commandLine], {
            detached: true, stdio: 'ignore', windowsHide: false,
        })
        terminal.unref()
    } else {
        const pathPrefix = process.platform === 'darwin' && PATH_HINTS.length
            ? `export PATH=${shellQuote(PATH_HINTS.join(path.delimiter))}:$PATH; `
            : ''
        commandLine = `${pathPrefix}${[executable, ...args].map(shellQuote).join(' ')}`
        if (process.platform === 'darwin') {
            const script = `tell application "Terminal" to do script ${JSON.stringify(commandLine)}`
            const terminal = spawn('osascript', ['-e', script], { detached: true, stdio: 'ignore' })
            terminal.unref()
        } else {
            const terminal = spawn(process.env.TERMINAL || 'x-terminal-emulator', ['-e', 'sh', '-lc', commandLine], {
                detached: true, stdio: 'ignore',
            })
            terminal.unref()
        }
    }
    return { ok: true, provider, message: `Opened a terminal for ${providerDisplayName(provider)} sign-in.` }
}

function configuredOllamaBaseURL() {
    const config = readLLMConfig()
    return config.provider === 'ollama' ? config.base_url : OLLAMA_BASE_URL
}

function usesManagedOllama() {
    if (readLLMConfig().provider !== 'ollama') return false
    const baseURL = configuredOllamaBaseURL().toLowerCase().replace(/\/+$/, '')
    return new Set([
        'http://127.0.0.1:11434',
        'http://localhost:11434',
        'http://[::1]:11434',
    ]).has(baseURL)
}

function apiUrl(pathname = '') {
    return `http://127.0.0.1:${apiPort || DEFAULT_API_PORT}${pathname}`
}

function findFreePort() {
    // Binding port 0 lets the OS hand back a port it knows is unused, which is
    // far more reliable than probing a fixed candidate list.
    return new Promise((resolve, reject) => {
        const probe = net.createServer()
        probe.unref()
        probe.on('error', reject)
        probe.listen({ host: '127.0.0.1', port: 0 }, () => {
            const address = probe.address()
            probe.close(() => resolve(address.port))
        })
    })
}

async function ensureApiPort() {
    // Reserved before any window exists so the renderer never has to guess.
    if (apiPort) return apiPort
    try {
        apiPort = await findFreePort()
    } catch (error) {
        console.log('[API] free port lookup failed, using default:', error.message)
        apiPort = DEFAULT_API_PORT
    }
    return apiPort
}

function buildPythonEnv(extra = {}) {
    // Every child process receives the same data directory, import path, and
    // tool-manager hints so packaged and development launches are consistent.
    const parts = [ROOT]
    if (process.env.PYTHONPATH) parts.push(process.env.PYTHONPATH)
    return {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
        CLIPPY_DATA_DIR: DATA_DIR,
        PYTHONPATH: parts.join(path.delimiter),
        PATH: [...PATH_HINTS, process.env.PATH || ''].filter(Boolean).join(path.delimiter),
        OLLAMA_MAX_LOADED_MODELS,
        OLLAMA_NUM_PARALLEL,
        CLIPPY_OLLAMA_URL: configuredOllamaBaseURL(),
        ...extra,
    }
}


async function ensureOllamaParallelConfig({ persist = true } = {}) {
    // Keep text-model residency predictable on machines with limited RAM.
    // Windows needs setx because Ollama may be started outside Electron.
    process.env.OLLAMA_MAX_LOADED_MODELS = OLLAMA_MAX_LOADED_MODELS
    process.env.OLLAMA_NUM_PARALLEL = OLLAMA_NUM_PARALLEL

    if (persist && process.platform === 'win32') {
        await runCommand('setx', ['OLLAMA_MAX_LOADED_MODELS', OLLAMA_MAX_LOADED_MODELS])
        await runCommand('setx', ['OLLAMA_NUM_PARALLEL', OLLAMA_NUM_PARALLEL])
    }
}

async function ollamaReachable(tries = 1, intervalMs = 250) {
    return pollUntilAlive(configuredOllamaBaseURL(), intervalMs, tries).then(() => true).catch(() => false)
}


// Only one caller may launch a server at a time, otherwise two concurrent
// spawns race for port 11434 and the loser dies with a bind error.
let ollamaStartPromise = null

async function ensureOllamaServing({ waitTries = 30 } = {}) {
    /*
     * Adopt any server that is already listening on the Ollama port.
     *
     * The desktop app and the Ollama tray app share one port. Restarting or
     * force-killing `ollama.exe` makes the tray app relaunch its own server,
     * and the two instances then fight over the socket: the loser exits with
     * "bind: Only one usage of each socket address", while in-flight requests
     * fail as HTTP 500 or connection refused. So Clippy never kills a server it
     * does not own, and starts one only when the port is genuinely free.
     */
    if (!usesManagedOllama()) {
        return ollamaReachable(3, 500)
    }
    if (await ollamaReachable(1, 250)) return true
    if (ollamaStartPromise) return ollamaStartPromise

    ollamaStartPromise = (async () => {
        if (ollamaProcess && !ollamaProcess.killed) {
            return ollamaReachable(waitTries, 1000)
        }
        console.log('[ollama] no server on', configuredOllamaBaseURL(), '— starting one')
        const proc = spawnHidden(OLLAMA_COMMAND, ['serve'], { cwd: ROOT, detached: false })
        ollamaProcess = proc
        // Ollama reports load failures and bind conflicts on stderr. Surfacing
        // them here is what turns an opaque HTTP 500 into a readable cause.
        proc.stdout.on('data', (d) => console.log('[ollama]', d.toString().trim()))
        proc.stderr.on('data', (d) => console.error('[ollama ERR]', d.toString().trim()))
        proc.on('exit', (code) => {
            console.log('[ollama] serve exited', code)
            if (ollamaProcess === proc) ollamaProcess = null
        })
        const alive = await ollamaReachable(waitTries, 1000)
        if (!alive) console.error('[ollama] server did not become reachable')
        return alive
    })()

    try {
        return await ollamaStartPromise
    } finally {
        ollamaStartPromise = null
    }
}


let mainWindow    = null
let setupWindow   = null
let tray          = null
let captureProcess = null
let apiProcess    = null
let ollamaProcess = null
let isQuitting    = false

app.on('second-instance', () => {
    if (setupWindow && !setupWindow.isDestroyed()) {
        setupWindow.show()
        setupWindow.focus()
    } else {
        showMainWindow()
    }
})





// Child processes inherit this environment so Python and Ollama resolve the
// same paths whether Electron was launched from Finder, a shell, or a DMG.
function spawnHidden(cmd, args, opts = {}) {
    const { env: envExtra, ...rest } = opts
    return spawn(cmd, args, {
        windowsHide: true,
        ...rest,
        env: buildPythonEnv(envExtra),
    })
}

function sendSetup(channel, data) {
    if (setupWindow && !setupWindow.isDestroyed()) {
        setupWindow.webContents.send(channel, data)
    }
}

function log(line, level = 'info') {
    sendSetup('setup-log', { line, level })
    console.log(`[setup/${level}]`, line)
}

function stepUpdate(key, state, sub) {
    sendSetup('step-update', { key, state, sub })
}

function stepProgress(key, percent) {
    sendSetup('step-progress', { key, percent })
}


let doneSoFar = 0
let setupStepTotal = 6
function markDone(key, sub) {
    // The renderer owns the visual step state; Electron only sends monotonic
    // completion updates after each asynchronous installer step finishes.
    doneSoFar++
    stepUpdate(key, 'done', sub)
    sendSetup('setup-overall', { done: doneSoFar, text: `${doneSoFar} / ${setupStepTotal} steps` })
}


function pollUntilAlive(url, intervalMs, maxTries) {
    // Setup and preflight use short HTTP probes instead of fixed sleeps so a
    // fast local service finishes immediately while slower Macs still work.
    return new Promise((resolve, reject) => {
        let tries = 0
        const check = () => {
            http.get(url, (res) => {
                const alive = (res.statusCode || 0) < 500
                res.resume()
                if (alive) resolve()
                else schedule()
            }).on('error', () => schedule())
        }
        const schedule = () => {
            if (++tries >= maxTries) return reject(new Error(`Timed out waiting for ${url}`))
            setTimeout(check, intervalMs)
        }
        check()
    })
}


function runCommand(cmd, args, opts = {}) {
    // Resolve every command through the shared child-process environment and
    // return a structured result so setup can show actionable errors in the UI.
    return new Promise((resolve) => {
        const proc = spawnHidden(cmd, args, { cwd: ROOT, ...opts })
        let out = '', err = ''
        proc.stdout.on('data', d => { out += d.toString() })
        proc.stderr.on('data', d => { err += d.toString() })
        proc.on('exit', code => resolve({ code: code === null ? 1 : code, stdout: out.trim(), stderr: err.trim() }))
        proc.on('error', e => resolve({ code: 1, stdout: '', stderr: e.message }))
    })
}

function ollamaListHasModel(output, required) {
    const expected = String(required || '').trim()
    if (!expected) return false
    return String(output || '').split(/\r?\n/).some((line) => {
        const actual = line.trim().split(/\s+/)[0]
        return actual === expected || (!expected.includes(':') && actual === `${expected}:latest`)
    })
}





async function stepCheckPython() {
    // macOS and Linux require a user-managed Python installation. Windows can
    // offer the same setup flow through winget when Python is missing.
    stepUpdate('python', 'running', 'Checking for Python 3.9+...')
    log('> python --version', 'dim')

    const { code, stdout, stderr } = await runCommand(PYTHON_COMMAND, ['--version'])
    const version = stdout || stderr

    if (code === 0 && version) {
        log(version, 'ok')
        markDone('python', version)
        return
    }

    if (process.platform !== 'win32') {
        log('Python was not found on PATH. Install Python 3.11+ from python.org or Homebrew, then retry.', 'err')
        stepUpdate('python', 'error', 'Install Python 3.11+ and make sure it is on PATH.')
        throw new Error('python-install-required')
    }


    log('Python not found. Installing via winget...', 'info')
    stepUpdate('python', 'running', 'Installing Python 3.11 via winget...')
    log('> winget install Python.Python.3.11 --silent', 'dim')

    const install = await runCommand('winget', [
        'install', 'Python.Python.3.11',
        '--silent',
        '--accept-package-agreements',
        '--accept-source-agreements',
    ])

    if (install.code !== 0) {
        log(install.stderr || 'winget failed', 'err')
        stepUpdate('python', 'error', 'Could not install Python. Please install manually from python.org')
        throw new Error('python-install-failed')
    }

    const verify = await runCommand(PYTHON_COMMAND, ['--version'])
    if (verify.code !== 0) {
        stepUpdate('python', 'error', 'Python installed but not on PATH. Restart required.')
        throw new Error('python-path')
    }

    const verifiedVersion = verify.stdout || verify.stderr || 'Python installed.'
    log(verifiedVersion, 'ok')
    markDone('python', verifiedVersion)
}

async function stepCheckOllama() {
    // Ollama is the only active provider in this release, so setup always
    // verifies the local runtime before downloading any model weights.
    stepUpdate('ollama', 'running', 'Checking for Ollama...')
    log('> ollama --version', 'dim')

    const { code, stdout, stderr } = await runCommand(OLLAMA_COMMAND, ['--version'])
    const version = stdout || stderr

    if (code === 0) {
        log(version || 'Ollama is available.', 'ok')
        markDone('ollama', (version || 'Ollama is available.').split('\n')[0])
        return
    }

    if (process.platform !== 'win32') {
        log('Ollama was not found on PATH. Install it from ollama.com, then retry.', 'err')
        stepUpdate('ollama', 'error', 'Install Ollama from ollama.com and make sure it is on PATH.')
        throw new Error('ollama-install-required')
    }

    log('Ollama not found. Installing via winget...', 'info')
    stepUpdate('ollama', 'running', 'Installing Ollama via winget...')
    log('> winget install Ollama.Ollama --silent', 'dim')

    const install = await runCommand('winget', [
        'install', 'Ollama.Ollama',
        '--silent',
        '--accept-package-agreements',
        '--accept-source-agreements',
    ])

    if (install.code !== 0) {
        log(install.stderr || 'winget failed', 'err')
        stepUpdate('ollama', 'error', 'Could not install Ollama. Please install from ollama.com')
        throw new Error('ollama-install-failed')
    }

    const verify = await runCommand(OLLAMA_COMMAND, ['--version'])
    if (verify.code !== 0) {
        stepUpdate('ollama', 'error', 'Ollama installed but not on PATH. Restart may be required.')
        throw new Error('ollama-path')
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

async function stepInstallPackages() {
    // pip output is streamed to the onboarding log while the renderer receives
    // an approximate progress value for long installs.
    stepUpdate('packages', 'running', 'Installing Python packages...')
    log('> pip install -r requirements.txt', 'dim')
    stepProgress('packages', -1)

    return new Promise((resolve, reject) => {
        const proc = spawnHidden(PYTHON_COMMAND, ['-m', 'pip', 'install', '-r', REQUIREMENTS], { cwd: ROOT })

        const PKG_TOTAL = 20
        let installed = 0

        proc.stdout.on('data', (chunk) => {
            const lines = chunk.toString().split('\n').filter(l => l.trim())
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
            const lines = chunk.toString().split('\n').filter(l => l.trim())
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
            } else {
                stepUpdate('packages', 'error', 'pip install failed. Check the log.')
                reject(new Error('pip-failed'))
            }
        })

        proc.on('error', (e) => {
            stepUpdate('packages', 'error', e.message)
            reject(e)
        })
    })
}

async function stepPullModels() {
    // MiniLM ships with the app. Setup only pulls the chat model — capture uses
    // accessibility text + OCR and never downloads a vision model.
    const models = [
        { name: 'qwen3:8b', label: 'qwen3:8b (~4.7 GB)' },
    ]

    stepUpdate('models', 'running', 'Checking existing models...')
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
        log('All models already downloaded.', 'ok')
        markDone('models', 'All models ready')
        return
    }

    for (let i = 0; i < needed.length; i++) {
        const model = needed[i]
        stepUpdate('models', 'running', `Downloading ${model.label}...`)
        log(`> ollama pull ${model.name}`, 'dim')
        stepProgress('models', -1)

        await new Promise((resolve, reject) => {
            const proc = spawnHidden(OLLAMA_COMMAND, ['pull', model.name], { cwd: ROOT })

            proc.stdout.on('data', (chunk) => {
                const lines = chunk.toString().split('\n').filter(l => l.trim())
                for (const line of lines) {
                    log(line, 'dim')

                    // Ollama emits human-readable GB or MB progress lines; the
                    // two parsers keep the progress bar useful for both sizes.
                    const match = line.match(/(\d+(?:\.\d+)?)\s*GB\s*\/\s*(\d+(?:\.\d+)?)\s*GB/)
                    if (match) {
                        const pct = Math.round((parseFloat(match[1]) / parseFloat(match[2])) * 100)
                        stepProgress('models', pct)
                        stepUpdate('models', 'running', `${model.label}: ${pct}%`)
                    }

                    const matchMB = line.match(/(\d+(?:\.\d+)?)\s*MB\s*\/\s*(\d+(?:\.\d+)?)\s*MB/)
                    if (matchMB) {
                        const pct = Math.round((parseFloat(matchMB[1]) / parseFloat(matchMB[2])) * 100)
                        stepProgress('models', pct)
                    }
                }
            })

            proc.stderr.on('data', (chunk) => {
                chunk.toString().split('\n').filter(l => l.trim()).forEach(l => log(l, 'dim'))
            })

            proc.on('exit', (code) => {
                if (code === 0) {
                    log(`${model.name} ready.`, 'ok')
                    resolve()
                } else {
                    reject(new Error(`ollama pull ${model.name} failed`))
                }
            })

            proc.on('error', reject)
        })
    }

    stepProgress('models', 100)
    markDone('models', 'All models downloaded')
}

async function stepWarmup() {
    // Warm the configured chat model during onboarding for a fast first reply.
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

    const external = isExternalProvider()
    if (!external) {
        try {
            await pollUntilAlive(configuredOllamaBaseURL(), 1000, 30)
        } catch (_) {
            log('Ollama not responding — skipping model warm.', 'info')
        }
    }

    log(external ? `Checking ${providerDisplayName()}...` : 'Warming the text model...', 'info')
    stepUpdate('warmup', 'running', external ? 'Checking provider connection...' : `Loading ${chatModel}...`)
    try {
        await httpPost(apiUrl('/residency/startup'), {}, 120000)
        if (external) {
            const status = await httpPost(apiUrl('/settings/provider/test'), {}, 10000)
            if (!status?.ok) throw new Error(status?.error || `${providerDisplayName()} could not be reached.`)
            log(status.message || `${providerDisplayName()} is ready.`, 'ok')
        } else {
            log('Text model ready — capture remains model-free.', 'ok')
        }
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

    writeSetupFlag({ provider: readLLMConfig().provider })

    log('Setup complete!', 'ok')
    markDone('warmup', 'Ready!')
}


function httpPost(url, body, timeoutMs = 30000) {
    // Keep the desktop bridge dependency-free; this helper only talks to the
    // local API server and returns JSON when the endpoint provides it.
    return new Promise((resolve, reject) => {
        const data    = JSON.stringify(body)
        const parsed  = new URL(url)
        const options = {
            hostname: parsed.hostname,
            port:     parsed.port || 80,
            path:     parsed.pathname,
            method:   'POST',
            headers:  { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
        }
        const req = http.request(options, (res) => {
            let responseBody = ''
            res.setEncoding('utf8')
            res.on('data', (chunk) => { responseBody += chunk })
            res.on('end', () => {
                if ((res.statusCode || 0) < 200 || (res.statusCode || 0) >= 300) {
                    return reject(new Error(`HTTP ${res.statusCode || 0}`))
                }
                try {
                    resolve(responseBody ? JSON.parse(responseBody) : undefined)
                } catch (_) {
                    resolve(undefined)
                }
            })
        })
        req.on('error', reject)
        req.setTimeout(timeoutMs, () => { req.destroy(); reject(new Error('timeout')) })
        req.write(data)
        req.end()
    })
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
    setupStepTotal = order.length
    const requestedIndex = order.indexOf(startFrom)
    const startIdx = requestedIndex >= 0 ? requestedIndex : 0

    for (let i = startIdx; i < order.length; i++) {
        const key = order[i]
        try {
            await stepFns[key]()
        } catch (err) {
            console.error(`[setup] step "${key}" failed:`, err.message)
            stepUpdate(key, 'error', err.message)
            // Stop here so the user can retry the failed step from onboarding.
            return
        }
    }



    // Give the renderer time to paint the final step before switching screens.
    setTimeout(() => sendSetup('setup-complete'), 800)
}





const REQUIRED_MODELS = ['qwen3:8b']

async function runPreflightChecks() {
    // Every normal launch verifies the local runtime before starting the API,
    // which turns missing models or permissions into a recoverable setup step.
    const managedOllama = usesManagedOllama()
    const alreadyConfigured = process.env.OLLAMA_MAX_LOADED_MODELS === OLLAMA_MAX_LOADED_MODELS
    if (managedOllama) {
        await ensureOllamaParallelConfig({ persist: !alreadyConfigured })
    }

    const py = await runCommand(PYTHON_COMMAND, ['--version'])
    if (py.code !== 0) {
        return { ok: false, step: 'python', reason: 'Python not found or not on PATH.' }
    }

    if (isExternalProvider()) {
        // The API performs the authenticated provider check after it starts.
    } else if (!managedOllama) {
        if (!(await ollamaReachable(3, 500))) {
            return { ok: false, step: 'warmup', reason: 'The configured local API is not reachable.' }
        }
    } else {
        const ol = await runCommand(OLLAMA_COMMAND, ['--version'])
        if (ol.code !== 0) {
            return { ok: false, step: 'ollama', reason: 'Ollama not found or not on PATH.' }
        }

        // Residency limits apply to whichever server owns the port; a running
        // server is adopted as-is rather than restarted, because killing it
        // starts a bind war with the Ollama tray app.
        if (!(await ensureOllamaServing({ waitTries: 20 }))) {
            return { ok: false, step: 'ollama-service', reason: 'Ollama service could not be started.' }
        }

        const list = await runCommand(OLLAMA_COMMAND, ['list'])
        const missing = REQUIRED_MODELS.filter((name) => !ollamaListHasModel(list.stdout, name))
        if (missing.length > 0) {
            return { ok: false, step: 'models', reason: `Missing models: ${missing.join(', ')}` }
        }
    }


    // Import checks are a cheap proxy for the full capture dependency set.
    const pkgCheck = await runCommand(PYTHON_COMMAND, [
        '-c',
        'import fastapi, uvicorn, pynput, mss, PIL, psutil, imagehash, transformers, torch, sklearn',
    ])
    if (pkgCheck.code !== 0) {
        return { ok: false, step: 'packages', reason: 'One or more Python packages are missing.' }
    }

    return { ok: true, step: null, reason: null }
}





// Capture is a11y + OCR (no VL), so the old 16 GB / 6 GB VRAM floor is gone.
// Chat still wants headroom for qwen3:8b; integrated GPUs are allowed at minimum.
const HW_MIN = { ramGb: 8, vramGb: 0, diskGb: 8 }
const HW_REC = { ramGb: 16, vramGb: 4, diskGb: 10 }
const HW_MIN_MAC = { ramGb: 8, vramGb: 8, diskGb: 8 }
const HW_REC_MAC = { ramGb: 16, vramGb: 16, diskGb: 10 }
const HW_MIN_EXTERNAL = { ramGb: 4, vramGb: 0, diskGb: 2 }
const HW_REC_EXTERNAL = { ramGb: 8, vramGb: 0, diskGb: 4 }

function gradeResource(value, min, rec) {
    if (value < min) return 'fail'
    if (value < rec) return 'warn'
    return 'ok'
}

function detectOsLabel() {
    if (process.platform === 'darwin') {
        return process.arch === 'arm64' ? 'macos-apple-silicon' : 'macos-intel'
    }
    if (process.platform !== 'win32') return process.platform
    const build = parseInt(os.release().split('.')[2] || '0', 10)
    return build >= 22000 ? 'windows11' : 'windows10'
}

async function getFreeDiskGb(dirPath) {
    // statfs is available on modern Node; the command fallback covers older
    // Electron runtimes and keeps the check working on both Windows and macOS.
    try {
        if (typeof fs.promises.statfs === 'function') {
            const s = await fs.promises.statfs(dirPath)
            return (Number(s.bavail) * Number(s.bsize)) / (1024 ** 3)
        }
    } catch (_) {                    }

    try {
        if (process.platform === 'win32') {
            const root = path.parse(path.resolve(dirPath)).root
            const letter = root.replace(/:\\?$/, '').replace('\\', '')
            const r = await runCommand('powershell', [
                '-NoProfile', '-Command',
                `(Get-PSDrive -Name '${letter}').Free`,
            ])
            const bytes = parseFloat(String(r.stdout || '').trim())
            if (!Number.isNaN(bytes) && bytes > 0) return bytes / (1024 ** 3)
        } else {
            const r = await runCommand('df', ['-kP', dirPath])
            const line = String(r.stdout || '').split(/\r?\n/).pop() || ''
            const availableKb = parseFloat(line.trim().split(/\s+/)[3])
            if (!Number.isNaN(availableKb) && availableKb > 0) return availableKb / (1024 ** 1024)
        }
    } catch (_) {              }
    return 0
}

async function getVramGb() {
    // Apple Silicon shares memory with the GPU and has no nvidia-smi value;
    // getHardwareCheck maps that case to unified system memory.
    if (process.platform === 'darwin') return 0
    const r = await runCommand('nvidia-smi', [
        '--query-gpu=memory.total',
        '--format=csv,noheader,nounits',
    ])
    if (r.code !== 0) return 0
    const mb = parseFloat(String(r.stdout || '').trim().split(/\r?\n/)[0])
    if (Number.isNaN(mb) || mb <= 0) return 0
    return mb / 1024
}

async function getHardwareCheck() {
    // Apple Silicon reports shared unified memory rather than discrete VRAM;
    // use total memory for the GPU grade so capable Macs are not blocked.
    // Round RAM to the nearest GB so marketed machines are not blocked by a
    // small amount of reserved memory (e.g. 7.8 GB reported on an 8 GB box).
    const ramGb = Math.round(os.totalmem() / (1024 ** 3))
    const diskRaw = await getFreeDiskGb(USER_DATA)
    const vramRaw = await getVramGb()
    const diskGb = Math.round(diskRaw * 10) / 10
    const osId = detectOsLabel()
    const external = isExternalProvider()
    const requirements = external ? HW_MIN_EXTERNAL : process.platform === 'darwin' ? HW_MIN_MAC : HW_MIN
    const recommended = external ? HW_REC_EXTERNAL : process.platform === 'darwin' ? HW_REC_MAC : HW_REC
    const vramGb = external
        ? 0
        : process.platform === 'darwin'
        ? ramGb
        : Math.round(vramRaw * 10) / 10
    const osOk = process.platform === 'win32' || process.platform === 'darwin'

    const grades = {
        ram:  gradeResource(ramGb, requirements.ramGb, recommended.ramGb),
        vram: gradeResource(vramGb, requirements.vramGb, recommended.vramGb),
        disk: gradeResource(diskGb, requirements.diskGb, recommended.diskGb),
        os:   osOk ? 'ok' : 'fail',
    }

    let level = 'ready'
    if (Object.values(grades).includes('fail')) level = 'block'
    else if (Object.values(grades).includes('warn')) level = 'warn'

    return {
        level,
        grades,
        min: requirements,
        recommended,
        yours: {
            ramGb,
            vramGb,
            diskGb,
            os: osId,
            memoryLabel: external ? 'Provider-hosted' : process.platform === 'darwin' ? 'Unified memory' : 'GPU VRAM',
            osLabel: osId === 'windows11' ? 'Windows 11'
                : osId === 'windows10' ? 'Windows 10'
                : osId === 'macos-apple-silicon' ? 'macOS · Apple Silicon'
                : osId === 'macos-intel' ? 'macOS · Intel'
                : osId,
        },
        mode: external ? (isSubscriptionProvider() ? 'subscription' : 'external') : usesManagedOllama() ? 'ollama' : 'external',
    }
}





let setupStartFrom = 'python'
let setupInstallStarted = false

function createSetupWindow() {
    // Setup is a separate, isolated window so install logs and the hardware
    // gate never compete with the main chat renderer.
    setupInstallStarted = false
    setupWindow = new BrowserWindow({
        width: 640,
        height: 720,
        resizable: false,
        maximizable: false,
        icon: ICON_ACTIVE,
        webPreferences: {
            preload: path.join(__dirname, 'setup-preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
        },
    })

    setupWindow.loadFile(path.join(__dirname, '../src/setup.html'))
    setupWindow.setMenu(null)

    setupWindow.on('closed', () => { setupWindow = null })

}

const RELEASE_CHECK_FILE = path.join(USER_DATA, 'release_check.json')
const RELEASE_CHECK_INTERVAL_MS = 12 * 60 * 60 * 1000

function readDesktopSettings() {
    let saved = {}
    try { saved = JSON.parse(fs.readFileSync(DESKTOP_SETTINGS_FILE, 'utf8')) || {} } catch (_) {                 }
    return { updateCheckEnabled: saved.updateCheckEnabled !== false }
}

function setUpdateCheckEnabled(enabled) {
    // The version lookup is the only outbound request Clippy makes, so it gets
    // an explicit switch rather than being buried in the update code path.
    const settings = { ...readDesktopSettings(), updateCheckEnabled: Boolean(enabled) }
    try {
        fs.mkdirSync(USER_DATA, { recursive: true })
        fs.writeFileSync(DESKTOP_SETTINGS_FILE, JSON.stringify(settings, null, 2) + '\n')
    } catch (error) {
        console.log('[updates] could not persist preference:', error.message)
    }
    return settings.updateCheckEnabled
}

function releaseVersionParts(value) {
    return String(value || '').replace(/^v/i, '').split(/[+-]/)[0].split('.').map((part) => {
        const number = parseInt(part, 10)
        return Number.isFinite(number) ? number : 0
    })
}

function isNewerVersion(candidate, current) {
    const next = releaseVersionParts(candidate)
    const installed = releaseVersionParts(current)
    for (let i = 0; i < Math.max(next.length, installed.length); i++) {
        if ((next[i] || 0) !== (installed[i] || 0)) return (next[i] || 0) > (installed[i] || 0)
    }
    return false
}

function fetchLatestRelease() {
    return new Promise((resolve, reject) => {
        const request = https.get(
            `https://api.github.com/repos/${RELEASE_REPOSITORY}/releases/latest`,
            { headers: { 'User-Agent': 'Clippy-Vision', Accept: 'application/vnd.github+json' } },
            (response) => {
                let body = ''
                response.setEncoding('utf8')
                response.on('data', (chunk) => { body += chunk })
                response.on('end', () => {
                    if (response.statusCode !== 200) return reject(new Error(`GitHub returned ${response.statusCode}`))
                    try { resolve(JSON.parse(body)) } catch (error) { reject(error) }
                })
            },
        )
        request.setTimeout(5000, () => request.destroy(new Error('release check timeout')))
        request.on('error', reject)
    })
}

async function checkForLatestRelease() {
    // Release checks are throttled and best-effort so a network outage never
    // blocks a local app launch.
    if (!mainWindow || mainWindow.isDestroyed()) return
    if (!readDesktopSettings().updateCheckEnabled) return
    try {
        const previous = JSON.parse(fs.readFileSync(RELEASE_CHECK_FILE, 'utf8'))
        if (Date.now() - Number(previous.checkedAt || 0) < RELEASE_CHECK_INTERVAL_MS) return
    } catch (_) {                   }

    try {
        const release = await fetchLatestRelease()
        fs.mkdirSync(USER_DATA, { recursive: true })
        fs.writeFileSync(RELEASE_CHECK_FILE, JSON.stringify({ checkedAt: Date.now() }))
        const version = String(release.tag_name || release.name || '').trim()
        if (!version || !isNewerVersion(version, app.getVersion())) return
        const url = release.html_url || `https://github.com/${RELEASE_REPOSITORY}/releases/tag/${encodeURIComponent(version)}`
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send('release-available', { version, url, name: release.name || version })
        }
    } catch (error) {
        console.log('[updates] release check skipped:', error.message)
    }
}

function createMainWindow() {
    // Closing the chat hides it to the tray; the process must stay alive for
    // capture and background processing until the user explicitly quits.
    mainWindow = new BrowserWindow({
        minWidth: 400,
        width: 800,
        height: 600,
        icon: ICON_ACTIVE,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
        },
    })

    mainWindow.loadFile(path.join(__dirname, '../src/index.html'))
    mainWindow.setMenu(null)
    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        try {
            const parsed = new URL(url)
            if (parsed.protocol === 'https:' || parsed.protocol === 'http:') {
                shell.openExternal(url)
            }
        } catch (_) { /* ignore invalid URLs */ }
        return { action: 'deny' }
    })
    mainWindow.webContents.on('will-navigate', (event, url) => {
        if (url !== mainWindow.webContents.getURL()) event.preventDefault()
    })
    mainWindow.webContents.once('did-finish-load', () => checkForLatestRelease())


    mainWindow.on('close', (event) => {
        if (!isQuitting) {
            event.preventDefault()
            mainWindow.hide()
        }
    })

    mainWindow.on('closed', () => { mainWindow = null })
}

function showMainWindow() {
    if (!mainWindow) {
        createMainWindow()
        return
    }
    mainWindow.show()
    mainWindow.focus()
}





function writeApiState(pid) {
    try {
        fs.mkdirSync(USER_DATA, { recursive: true })
        fs.writeFileSync(API_STATE_FILE, JSON.stringify({ pid, port: apiPort }, null, 2))
    } catch (_) {                          }
}

function clearApiState() {
    try { fs.unlinkSync(API_STATE_FILE) } catch (_) {                          }
}

async function clearStaleApiProcess() {
    // A crash or force quit skips before-quit, leaving the Python API alive and
    // still holding the SQLite database. Only terminate the recorded PID when a
    // Clippy API actually answers on its recorded port, so a reused PID that now
    // belongs to an unrelated process is never targeted.
    let previous = null
    try { previous = JSON.parse(fs.readFileSync(API_STATE_FILE, 'utf8')) } catch (_) { return }
    const pid = Number(previous && previous.pid)
    const port = Number(previous && previous.port)
    if (!pid || !port || pid === process.pid) return clearApiState()

    try {
        await pollUntilAlive(`http://127.0.0.1:${port}/health`, 200, 1)
    } catch (_) {
        return clearApiState()
    }

    try {
        if (process.platform === 'win32') {
            spawnHidden('taskkill', ['/pid', String(pid), '/T', '/F'])
        } else {
            process.kill(pid, 'SIGTERM')
        }
        console.log('[API] terminated orphaned server pid=', pid)
    } catch (error) {
        console.log('[API] could not terminate orphaned server:', error.message)
    }
    clearApiState()
}

async function startServer() {
    // The API stays as a local child process so chat, memory, and capture data
    // never need a hosted relay.
    if (apiProcess) return
    await ensureApiPort()
    const proc = spawnHidden(PYTHON_COMMAND, [API_SCRIPT], {
        cwd: ROOT,
        env: { CLIPPY_API_PORT: String(apiPort) },
    })
    apiProcess = proc
    writeApiState(proc.pid)
    proc.stdout.on('data', d => console.log('[API]', d.toString().trim()))
    proc.stderr.on('data', d => console.error('[API ERR]', d.toString().trim()))
    proc.on('exit', (code) => {
        console.log('[API] exited', code)
        if (apiProcess !== proc) return
        apiProcess = null
        clearApiState()
    })
}





function isCapturing() {
    return captureProcess != null && !captureProcess.killed
}

function writeCaptureState(active) {
    try {
        fs.writeFileSync(CAPTURE_STATE_FILE, JSON.stringify({
            active: Boolean(active),
            pid: active && captureProcess ? captureProcess.pid : null,
            updated_at: Date.now() / 1000,
        }, null, 2))
    } catch (_) {                          }
}

function getTrayIcon(active) {
    const file = active ? ICON_ACTIVE : ICON_INACTIVE
    return nativeImage.createFromPath(file).resize({ width: 16, height: 16 })
}

function updateTrayIcon() {
    if (!tray) return
    tray.setImage(getTrayIcon(isCapturing()))
    tray.setToolTip(isCapturing() ? 'Clippy Vision — Capturing' : 'Clippy Vision — Idle')
}

function broadcastCaptureStatus() {
    const active = isCapturing()
    if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('capture-status-changed', active)
    }
    if (Notification.isSupported()) {
        new Notification({
            title: 'Clippy Vision',
            body: active ? 'Screen capture started' : 'Screen capture stopped',
            silent: false,
        }).show()
    }
}

function startCapture() {
    // Capture is a separate Python process because keyboard hooks and image
    // processing must not block Electron's renderer or tray event loop.
    if (isCapturing()) return
    const proc = spawnHidden(PYTHON_COMMAND, [CAPTURE_SCRIPT], { cwd: ROOT })
    captureProcess = proc
    proc.stdout.on('data', d => console.log('[Capture]', d.toString().trim()))
    proc.stderr.on('data', d => console.error('[Capture ERR]', d.toString().trim()))
    proc.on('exit', (code) => {
        console.log('[Capture] exited', code)
        if (captureProcess !== proc) return
        captureProcess = null
        writeCaptureState(false)
        updateTrayIcon()
        rebuildTrayMenu()
        broadcastCaptureStatus()
    })
    updateTrayIcon()
    rebuildTrayMenu()
    broadcastCaptureStatus()
    writeCaptureState(true)
    console.log('[Capture] started pid=', captureProcess.pid)
}

function stopCapture() {
    // Windows needs a tree kill for child processes; POSIX systems can use the
    // normal termination signal and let the Python shutdown hook clean up.
    if (!captureProcess) return
    const proc = captureProcess
    captureProcess = null
    writeCaptureState(false)
    if (process.platform === 'win32' && proc.pid) {
        spawnHidden('taskkill', ['/pid', String(proc.pid), '/T', '/F'])
    } else {
        try { proc.kill('SIGTERM') } catch (_) { }
    }

    updateTrayIcon()
    rebuildTrayMenu()
    broadcastCaptureStatus()
    console.log('[Capture] stopped')
}

function toggleCapture() {
    if (isCapturing()) stopCapture()
    else startCapture()
}





function rebuildTrayMenu() {
    // Rebuild after every capture transition so the menu label mirrors state.
    if (!tray) return
    tray.setContextMenu(Menu.buildFromTemplate([
        { label: isCapturing() ? 'Stop Capture' : 'Start Capture', click: toggleCapture },
        { type: 'separator' },
        { label: 'Open Chat', click: showMainWindow },
        { type: 'separator' },
        { label: 'Quit', click: () => { isQuitting = true; stopCapture(); app.quit() } },
    ]))
}

function createTray() {
    // The tray is the persistent control surface while the main window is
    // hidden, which keeps screen capture available without a large window.
    // Both the normal launch path and the setup "Launch" handler reach here, so
    // reuse the existing tray instead of leaving a second icon behind.
    if (tray && !tray.isDestroyed()) return
    tray = new Tray(getTrayIcon(false))
    tray.setToolTip('Clippy Vision — Idle')
    rebuildTrayMenu()
    tray.on('click', toggleCapture)
    tray.on('double-click', showMainWindow)
}





// Expose only narrow, validated desktop actions to the isolated renderer.
ipcMain.handle('toggle-capture',     () => { toggleCapture();  return isCapturing() })
ipcMain.handle('get-capture-status', () => isCapturing())
ipcMain.handle('get-login-item', () => app.getLoginItemSettings().openAtLogin)
ipcMain.handle('set-login-item', (_event, enabled) => {
    const openAtLogin = Boolean(enabled)
    app.setLoginItemSettings({ openAtLogin })
    return app.getLoginItemSettings().openAtLogin
})
ipcMain.handle('save-text-file', async (_event, payload = {}) => {
    const content = String(payload.content || '')
    if (content.length > 20_000_000) throw new Error('Export is too large to save from the desktop UI.')
    const requestedName = path.basename(String(payload.filename || 'clippy-export.json'))
    const safeName = requestedName && requestedName !== '.' && requestedName !== '..'
        ? requestedName
        : 'clippy-export.json'
    const filename = safeName.toLowerCase().endsWith('.json') ? safeName : `${safeName}.json`
    const result = await dialog.showSaveDialog(mainWindow, {
        title: 'Export Clippy data',
        defaultPath: path.join(USER_DATA, filename || 'clippy-export.json'),
        filters: [{ name: 'JSON', extensions: ['json'] }],
    })
    if (result.canceled || !result.filePath) return { canceled: true }
    fs.writeFileSync(result.filePath, content, 'utf8')
    return { canceled: false, path: result.filePath }
})
ipcMain.handle('open-screenshot', async (_event, value) => {
    const filename = path.basename(String(value || '').replace(/^\/screenshots\//, ''))
    const target = path.join(DATA_DIR, 'screenshots', filename)
    if (!filename || !fs.existsSync(target)) return false
    return (await shell.openPath(target)) === ''
})
ipcMain.handle('open-external', (_event, url) => {
    const value = String(url || '')
    let parsed
    try { parsed = new URL(value) } catch (_) { return false }
    const expectedPath = `/${RELEASE_REPOSITORY}`
    if (parsed.protocol !== 'https:' || parsed.hostname.toLowerCase() !== 'github.com' ||
        !(parsed.pathname === expectedPath || parsed.pathname.startsWith(`${expectedPath}/`))) return false
    return shell.openExternal(value)
})

ipcMain.handle('get-api-base', async () => apiUrl())
ipcMain.handle('get-update-check', () => readDesktopSettings().updateCheckEnabled)
ipcMain.handle('get-app-version', () => app.getVersion())
ipcMain.handle('set-update-check', (_event, enabled) => setUpdateCheckEnabled(enabled))

ipcMain.handle('get-hardware-check', async () => getHardwareCheck())
ipcMain.handle('get-llm-config', () => publicLLMConfig())
ipcMain.handle('save-llm-config', (_event, values = {}) => saveLLMConfig(values))
ipcMain.handle('open-provider-auth', (_event, provider) => openProviderAuth(String(provider || '')))

ipcMain.handle('confirm-hardware-and-start', async (_event, { override } = {}) => {
    const check = await getHardwareCheck()
    if (check.level === 'block') {
        return { ok: false, reason: 'below_minimum', check }
    }
    if (check.level === 'warn' && !override) {
        return { ok: false, reason: 'override_required', check }
    }
    if (setupInstallStarted) {
        return { ok: true, alreadyStarted: true }
    }
    setupInstallStarted = true
    doneSoFar = 0

    setImmediate(() => runSetup(setupStartFrom))
    return { ok: true, check }
})

ipcMain.handle('retry-step', (_event, key) => {
    doneSoFar = Math.max(0, doneSoFar - 1)
    runSetup(key)
})

ipcMain.handle('launch-app', () => {
    if (setupWindow && !setupWindow.isDestroyed()) setupWindow.close()
    createTray()
    createMainWindow()

})





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

function sendLoadingStatus(title, sub) {
    if (mainWindow && !mainWindow.isDestroyed()) {
        const payload = {}
        if (title) payload.title = title
        if (sub) payload.sub = sub
        mainWindow.webContents.send('loading-status', payload)
    }
}

function redirectToSetup(fromStep) {
    try { fs.unlinkSync(SETUP_FLAG) } catch (_) {}
    setupStartFrom = fromStep
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.close()
    mainWindow = null
    if (tray) { tray.destroy(); tray = null }
    createSetupWindow()
}

function waitForMainWindowLoad() {
    return new Promise((resolve) => {
        if (!mainWindow || mainWindow.isDestroyed()) return resolve()
        if (!mainWindow.webContents.isLoading()) return resolve()
        mainWindow.webContents.once('did-finish-load', () => resolve())
    })
}





app.whenReady().then(async () => {
    // Windows groups the taskbar entry, tray icon, and notifications by this id.
    if (process.platform === 'win32') app.setAppUserModelId('com.clippyvision.app')

    // Register the global shortcut once Electron owns the application session.
    globalShortcut.register('CommandOrControl+Shift+Space', () => toggleCapture())

    for (const d of [DATA_DIR, path.join(DATA_DIR, 'screenshots'), path.join(USER_DATA, 'logs')]) {
        if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true })
    }

    await clearStaleApiProcess()
    await ensureApiPort()
    console.log(`[api] port=${apiPort}`)
    console.log(`[paths] packaged=${IS_PACKAGED}`)
    console.log(`[paths] ROOT=${ROOT}`)
    console.log(`[paths] USER_DATA=${USER_DATA}`)
    console.log(`[paths] DATA_DIR=${DATA_DIR}`)

    syncSetupFlagVersion()
    const setupDone = fs.existsSync(SETUP_FLAG)

    if (setupDone) {

        createTray()
        createMainWindow()
        await waitForMainWindowLoad()
        sendLoadingStatus(
            null,
            'Checking dependencies…'
        )

        console.log('[preflight] Checking all dependencies...')
        const check = await runPreflightChecks()

        if (!check.ok) {
            console.error(`[preflight] Failed at step "${check.step}": ${check.reason}`)
            redirectToSetup(check.step)
            return
        }

        console.log('[preflight] All checks passed — starting API')
        sendLoadingStatus(null, isExternalProvider()
            ? `Starting Clippy with ${providerDisplayName()}…`
            : 'Starting AI server and loading models…')
        await startServer()
        pollUntilAlive(apiUrl('/health'), 1000, 90)
            .then(async () => {
                console.log('[app] API server healthy — preparing provider...')
                sendLoadingStatus(null, isExternalProvider() ? 'Checking provider…' : 'Loading text model…')
                try {
                    await httpPost(apiUrl('/residency/startup'), {}, 120000)
                    if (isExternalProvider()) {
                        const status = await httpPost(apiUrl('/settings/provider/test'), {}, 10000)
                        if (!status?.ok) console.log('[app] provider check failed:', status?.error || 'unknown error')
                    }
                    console.log('[app] residency startup warm done')
                } catch (e) {
                    console.log('[app] residency warm skipped:', e.message)
                }
                if (mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send('api-ready')
                }
            })
            .catch(() => {
                console.error('[app] API server failed to start after preflight — back to setup')
                redirectToSetup('packages')
            })
    } else {
        setupStartFrom = 'python'
        createSetupWindow()
    }

    app.on('activate', () => {
        if (mainWindow) showMainWindow()
        else if (setupWindow) {
            setupWindow.show()
            setupWindow.focus()
        }
    })
})

app.on('window-all-closed', () => {

})

app.on('before-quit', () => {
    isQuitting = true
    globalShortcut.unregister('CommandOrControl+Shift+Space')
    stopCapture()
    if (apiProcess) {
        if (process.platform === 'win32') {
            spawnHidden('taskkill', ['/pid', String(apiProcess.pid), '/T', '/F'])
        } else {
            apiProcess.kill('SIGTERM')
        }
        apiProcess = null
    }
    // Only a server Clippy started is stopped here. A server owned by the
    // Ollama tray app must survive, or it will relaunch and fight for the port.
    if (ollamaProcess) {
        try { ollamaProcess.kill('SIGTERM') } catch (_) { }
        ollamaProcess = null
    }
    clearApiState()
    // Windows keeps painting a tray icon whose owner has exited until the user
    // hovers over it, so release it explicitly on the way out.
    if (tray && !tray.isDestroyed()) {
        tray.destroy()
        tray = null
    }
})
