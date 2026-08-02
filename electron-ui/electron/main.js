const {
    app, BrowserWindow, Tray, Menu,
    nativeImage, ipcMain, Notification
} = require('electron')
const { spawn }  = require('child_process')
const path       = require('path')
const fs         = require('fs')
const http       = require('http')
const os         = require('os')
const updater    = require('./updater')

// ─── paths ────────────────────────────────────────────────────────────────────
// Packaged: Python lives in resources/clippy; writable data in %APPDATA%/Clippy Vision
// Dev:      repo root (electron-ui/../..)
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

// Keep a second slot free so text stays loaded when capture pins vision.
// Vision itself is only warmed while screen capture is on (see model_residency.py).
const OLLAMA_MAX_LOADED_MODELS = '2'
const OLLAMA_NUM_PARALLEL = '1'

function buildPythonEnv(extra = {}) {
    const parts = [ROOT]
    if (process.env.PYTHONPATH) parts.push(process.env.PYTHONPATH)
    return {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
        CLIPPY_DATA_DIR: DATA_DIR,
        PYTHONPATH: parts.join(path.delimiter),
        OLLAMA_MAX_LOADED_MODELS,
        OLLAMA_NUM_PARALLEL,
        ...extra,
    }
}

/** Apply Ollama parallel/loaded-model settings for this process and persist for future logins. */
async function ensureOllamaParallelConfig({ persist = true, restart = false } = {}) {
    process.env.OLLAMA_MAX_LOADED_MODELS = OLLAMA_MAX_LOADED_MODELS
    process.env.OLLAMA_NUM_PARALLEL = OLLAMA_NUM_PARALLEL

    if (persist) {
        // setx writes HKCU env so future shells / Ollama launches inherit it
        await runCommand('setx', ['OLLAMA_MAX_LOADED_MODELS', OLLAMA_MAX_LOADED_MODELS])
        await runCommand('setx', ['OLLAMA_NUM_PARALLEL', OLLAMA_NUM_PARALLEL])
    }

    if (!restart) return

    // Restart so a previously running Ollama process picks up the new limits
    await runCommand('taskkill', ['/IM', 'ollama.exe', '/F'])
    await new Promise((r) => setTimeout(r, 1500))
    spawnHidden('ollama', ['serve'], { cwd: ROOT, detached: false })
}

// ─── state ────────────────────────────────────────────────────────────────────
let mainWindow    = null
let setupWindow   = null
let tray          = null
let captureProcess = null
let apiProcess    = null
let isQuitting    = false

// ─────────────────────────────────────────────────────────────────────────────
// helpers
// ─────────────────────────────────────────────────────────────────────────────

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

// count how many steps are done and update the bottom pill
let doneSoFar = 0
const STEP_TOTAL = 6
function markDone(key, sub) {
    doneSoFar++
    stepUpdate(key, 'done', sub)
    sendSetup('setup-overall', { done: doneSoFar, text: `${doneSoFar} / ${STEP_TOTAL} steps` })
}

// poll a URL until it responds 200, with timeout
function pollUntilAlive(url, intervalMs, maxTries) {
    return new Promise((resolve, reject) => {
        let tries = 0
        const check = () => {
            http.get(url, (res) => {
                if (res.statusCode < 500) resolve()
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

// run a command, collect stdout, return { code, stdout, stderr }
function runCommand(cmd, args, opts = {}) {
    return new Promise((resolve) => {
        const proc = spawnHidden(cmd, args, { cwd: ROOT, ...opts })
        let out = '', err = ''
        proc.stdout.on('data', d => { out += d.toString() })
        proc.stderr.on('data', d => { err += d.toString() })
        proc.on('exit', code => resolve({ code: code || 0, stdout: out.trim(), stderr: err.trim() }))
        proc.on('error', e => resolve({ code: 1, stdout: '', stderr: e.message }))
    })
}

// ─────────────────────────────────────────────────────────────────────────────
// setup steps
// ─────────────────────────────────────────────────────────────────────────────

async function stepCheckPython() {
    stepUpdate('python', 'running', 'Checking for Python 3.9+...')
    log('> python --version', 'dim')

    const { code, stdout } = await runCommand('python', ['--version'])

    if (code === 0 && stdout) {
        log(stdout, 'ok')
        markDone('python', stdout)
        return
    }

    // not found — try installing via winget
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

    const verify = await runCommand('python', ['--version'])
    if (verify.code !== 0) {
        stepUpdate('python', 'error', 'Python installed but not on PATH. Restart required.')
        throw new Error('python-path')
    }

    log(verify.stdout, 'ok')
    markDone('python', verify.stdout)
}

async function stepCheckOllama() {
    stepUpdate('ollama', 'running', 'Checking for Ollama...')
    log('> ollama --version', 'dim')

    const { code, stdout } = await runCommand('ollama', ['--version'])

    if (code === 0) {
        log(stdout, 'ok')
        markDone('ollama', stdout.split('\n')[0])
        return
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

    const verify = await runCommand('ollama', ['--version'])
    if (verify.code !== 0) {
        stepUpdate('ollama', 'error', 'Ollama installed but not on PATH. Restart may be required.')
        throw new Error('ollama-path')
    }

    log(verify.stdout.split('\n')[0], 'ok')
    markDone('ollama', verify.stdout.split('\n')[0])
}

async function stepStartOllamaService() {
    stepUpdate('ollama-service', 'running', 'Configuring & starting Ollama...')
    log('> ollama serve', 'dim')

    // Slot for text + vision when capture pins VL; vision is not warmed at launch.
    log('Setting OLLAMA_MAX_LOADED_MODELS=2 (vision only while capturing)...', 'info')
    await ensureOllamaParallelConfig({ persist: true, restart: true })

    log('Waiting for Ollama service...', 'dim')
    stepProgress('ollama-service', -1)

    try {
        await pollUntilAlive('http://localhost:11434', 1000, 30)
        log('Ollama ready.', 'ok')
        markDone('ollama-service', 'Ollama service running')
    } catch (e) {
        stepUpdate('ollama-service', 'error', 'Ollama service did not start in time.')
        log(e.message, 'err')
        throw new Error('ollama-service-timeout')
    }
}

async function stepInstallPackages() {
    stepUpdate('packages', 'running', 'Installing Python packages...')
    log('> pip install -r requirements.txt', 'dim')
    stepProgress('packages', -1)

    return new Promise((resolve, reject) => {
        const proc = spawnHidden('python', ['-m', 'pip', 'install', '-r', REQUIREMENTS], { cwd: ROOT })

        const PKG_TOTAL = 20  // approximate — progress feels real
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
                // pip writes normal progress to stderr, not just errors
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
    const models = [
        { name: 'nomic-embed-text', label: 'nomic-embed-text (~274 MB)' },
        { name: 'qwen3:8b',         label: 'qwen3:8b (~4.7 GB)' },
        { name: 'qwen3-vl:4b',      label: 'qwen3-vl:4b (~2.9 GB)' },
    ]

    stepUpdate('models', 'running', 'Checking existing models...')
    log('> ollama list', 'dim')

    const { stdout: listOut } = await runCommand('ollama', ['list'])
    log(listOut || '(no models yet)', 'dim')

    const needed = models.filter(m => !listOut.includes(m.name.split(':')[0]))
    const alreadyHave = models.filter(m => listOut.includes(m.name.split(':')[0]))

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
            const proc = spawnHidden('ollama', ['pull', model.name], { cwd: ROOT })

            proc.stdout.on('data', (chunk) => {
                const lines = chunk.toString().split('\n').filter(l => l.trim())
                for (const line of lines) {
                    log(line, 'dim')
                    // ollama pull prints: "pulling sha256:abc... 1.2 GB / 4.7 GB"
                    const match = line.match(/(\d+(?:\.\d+)?)\s*GB\s*\/\s*(\d+(?:\.\d+)?)\s*GB/)
                    if (match) {
                        const pct = Math.round((parseFloat(match[1]) / parseFloat(match[2])) * 100)
                        stepProgress('models', pct)
                        stepUpdate('models', 'running', `${model.label}: ${pct}%`)
                    }
                    // also handle MB progress
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
    stepUpdate('warmup', 'running', 'Loading models into memory...')
    stepProgress('warmup', -1)

    log('Starting API server for warmup...', 'dim')
    startServer()

    try {
        await pollUntilAlive('http://localhost:8000/health', 1000, 90)
        log('API server ready.', 'ok')
    } catch (_) {
        log('API server slow to start — continuing anyway.', 'info')
    }

    // Ensure Ollama is reachable before asking it to load weights
    try {
        await pollUntilAlive('http://localhost:11434', 1000, 30)
    } catch (_) {
        log('Ollama not responding — skipping model warm.', 'info')
    }

    // Explicit warm call (text + embed only). Do not block setup forever if Ollama is slow.
    log('Warming text + embed (vision loads when capture starts)...', 'info')
    stepUpdate('warmup', 'running', 'Loading qwen3:8b + embed...')
    try {
        await httpPost('http://localhost:8000/residency/startup', {}, 120000)
        log('Text model ready — vision idle until screen capture.', 'ok')
    } catch (e) {
        log(`Model warm skipped or timed out (${e.message}) — continuing.`, 'info')
        // Guarantee setup can finish even if warm hung mid-flight
        try {
            fs.writeFileSync(RESIDENCY_FILE, JSON.stringify({
                vision: 'idle',
                reason: 'warmup_timeout',
                updated_at: new Date().toISOString(),
            }, null, 2))
        } catch (_) { /* ignore */ }
    }

    const dirs = [
        DATA_DIR,
        path.join(DATA_DIR, 'screenshots'),
        path.join(USER_DATA, 'logs'),
    ]
    for (const d of dirs) {
        if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true })
    }

    fs.writeFileSync(SETUP_FLAG, JSON.stringify({
        version: '1.0.1',
        completedAt: new Date().toISOString(),
    }, null, 2))

    log('Setup complete!', 'ok')
    markDone('warmup', 'Ready!')
}

// tiny http POST helper (no external deps)
function httpPost(url, body, timeoutMs = 30000) {
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
            res.resume()
            res.on('end', resolve)
        })
        req.on('error', reject)
        req.setTimeout(timeoutMs, () => { req.destroy(); reject(new Error('timeout')) })
        req.write(data)
        req.end()
    })
}

// ─────────────────────────────────────────────────────────────────────────────
// setup orchestrator
// ─────────────────────────────────────────────────────────────────────────────

const stepFns = {
    'python':         stepCheckPython,
    'ollama':         stepCheckOllama,
    'ollama-service': stepStartOllamaService,
    'packages':       stepInstallPackages,
    'models':         stepPullModels,
    'warmup':         stepWarmup,
}

async function runSetup(startFrom = 'python') {
    const order = ['python', 'ollama', 'ollama-service', 'packages', 'models', 'warmup']
    const startIdx = order.indexOf(startFrom)

    for (let i = startIdx; i < order.length; i++) {
        const key = order[i]
        try {
            await stepFns[key]()
        } catch (err) {
            console.error(`[setup] step "${key}" failed:`, err.message)
            stepUpdate(key, 'error', err.message)
            // stop here — user must click Retry in the UI
            return
        }
    }

    // small delay so the renderer finishes processing the final step-update
    // messages before we send setup-complete (prevents race on fast machines)
    setTimeout(() => sendSetup('setup-complete'), 800)
}

// ─────────────────────────────────────────────────────────────────────────────
// pre-flight checks (run on every normal launch to detect broken installs)
// ─────────────────────────────────────────────────────────────────────────────

const REQUIRED_MODELS = ['qwen3:8b', 'qwen3-vl:4b', 'nomic-embed-text']

async function runPreflightChecks() {
    // Ensure text can stay loaded when capture later pins vision
    const alreadyConfigured =
        process.env.OLLAMA_MAX_LOADED_MODELS === OLLAMA_MAX_LOADED_MODELS
    await ensureOllamaParallelConfig({
        persist: !alreadyConfigured,
        restart: false,
    })

    // 1. Python
    const py = await runCommand('python', ['--version'])
    if (py.code !== 0) {
        return { ok: false, step: 'python', reason: 'Python not found or not on PATH.' }
    }

    // 2. Ollama binary
    const ol = await runCommand('ollama', ['--version'])
    if (ol.code !== 0) {
        return { ok: false, step: 'ollama', reason: 'Ollama not found or not on PATH.' }
    }

    // 3. Ollama service — if we just wrote the env vars for the first time,
    //    restart so the running process picks up MAX_LOADED_MODELS=2
    const serviceAlive = await pollUntilAlive('http://localhost:11434', 500, 3).then(() => true).catch(() => false)
    if (!alreadyConfigured) {
        await ensureOllamaParallelConfig({ persist: false, restart: true })
        const started = await pollUntilAlive('http://localhost:11434', 1000, 15).then(() => true).catch(() => false)
        if (!started) {
            return { ok: false, step: 'ollama-service', reason: 'Ollama service could not be started.' }
        }
    } else if (!serviceAlive) {
        spawnHidden('ollama', ['serve'], { cwd: ROOT, detached: false })
        const started = await pollUntilAlive('http://localhost:11434', 1000, 10).then(() => true).catch(() => false)
        if (!started) {
            return { ok: false, step: 'ollama-service', reason: 'Ollama service could not be started.' }
        }
    }

    // 4. Required models
    const list = await runCommand('ollama', ['list'])
    const missing = REQUIRED_MODELS.filter(m => !list.stdout.includes(m.split(':')[0]))
    if (missing.length > 0) {
        return { ok: false, step: 'models', reason: `Missing models: ${missing.join(', ')}` }
    }

    // 5. Python packages (proxy: can api_server.py import cleanly?)
    const pkgCheck = await runCommand('python', [
        '-c',
        'import fastapi, uvicorn, pynput, mss, PIL, psutil, imagehash, transformers, torch, sklearn',
    ])
    if (pkgCheck.code !== 0) {
        return { ok: false, step: 'packages', reason: 'One or more Python packages are missing.' }
    }

    return { ok: true, step: null, reason: null }
}

// ─────────────────────────────────────────────────────────────────────────────
// hardware gate (shown before install steps)
// ─────────────────────────────────────────────────────────────────────────────

const HW_MIN = { ramGb: 16, vramGb: 6, diskGb: 12 }
const HW_REC = { ramGb: 32, vramGb: 8, diskGb: 15 }

function gradeResource(value, min, rec) {
    if (value < min) return 'fail'
    if (value < rec) return 'warn'
    return 'ok'
}

function detectWindowsLabel() {
    if (process.platform !== 'win32') return process.platform
    const build = parseInt(os.release().split('.')[2] || '0', 10)
    return build >= 22000 ? 'windows11' : 'windows10'
}

async function getFreeDiskGb(dirPath) {
    try {
        if (typeof fs.promises.statfs === 'function') {
            const s = await fs.promises.statfs(dirPath)
            return (Number(s.bavail) * Number(s.bsize)) / (1024 ** 3)
        }
    } catch (_) { /* fall through */ }

    try {
        const root = path.parse(path.resolve(dirPath)).root
        const letter = root.replace(/:\\?$/, '').replace('\\', '')
        const r = await runCommand('powershell', [
            '-NoProfile', '-Command',
            `(Get-PSDrive -Name '${letter}').Free`,
        ])
        const bytes = parseFloat(String(r.stdout || '').trim())
        if (!Number.isNaN(bytes) && bytes > 0) return bytes / (1024 ** 3)
    } catch (_) { /* ignore */ }
    return 0
}

async function getVramGb() {
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
    // Round RAM to nearest GB so marketed 16GB machines (~15.7 usable) aren't blocked
    const ramGb = Math.round(os.totalmem() / (1024 ** 3))
    const diskRaw = await getFreeDiskGb(USER_DATA)
    const vramRaw = await getVramGb()
    const diskGb = Math.round(diskRaw * 10) / 10
    const vramGb = Math.round(vramRaw * 10) / 10
    const osId = detectWindowsLabel()
    const osOk = osId === 'windows10' || osId === 'windows11'

    const grades = {
        ram:  gradeResource(ramGb, HW_MIN.ramGb, HW_REC.ramGb),
        vram: gradeResource(vramGb, HW_MIN.vramGb, HW_REC.vramGb),
        disk: gradeResource(diskGb, HW_MIN.diskGb, HW_REC.diskGb),
        os:   osOk ? 'ok' : 'fail',
    }

    let level = 'ready'
    if (Object.values(grades).includes('fail')) level = 'block'
    else if (Object.values(grades).includes('warn')) level = 'warn'

    return {
        level,
        grades,
        min: HW_MIN,
        recommended: HW_REC,
        yours: {
            ramGb,
            vramGb,
            diskGb,
            os: osId,
            osLabel: osId === 'windows11' ? 'Windows 11'
                : osId === 'windows10' ? 'Windows 10'
                : osId,
        },
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// windows
// ─────────────────────────────────────────────────────────────────────────────

let setupStartFrom = 'python'  // which step setup should resume from
let setupInstallStarted = false

function createSetupWindow() {
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
    // Install steps start only after the user passes the hardware gate (IPC).
}

function createMainWindow() {
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

    // X → hide to tray instead of quit
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

// ─────────────────────────────────────────────────────────────────────────────
// API server
// ─────────────────────────────────────────────────────────────────────────────

function startServer() {
    if (apiProcess) return
    apiProcess = spawnHidden('python', [API_SCRIPT], { cwd: ROOT })
    apiProcess.stdout.on('data', d => console.log('[API]', d.toString().trim()))
    apiProcess.stderr.on('data', d => console.error('[API ERR]', d.toString().trim()))
    apiProcess.on('exit', (code) => {
        console.log('[API] exited', code)
        apiProcess = null
    })
}

// ─────────────────────────────────────────────────────────────────────────────
// capture process
// ─────────────────────────────────────────────────────────────────────────────

function isCapturing() {
    return captureProcess != null && !captureProcess.killed
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

function notifyVisionUnload() {
    // Capture process is force-killed; ask the API to unload VL.
    httpPost('http://localhost:8000/residency/capture-stop', {}, 10000).catch((e) => {
        console.log('[Capture] vision unload notify failed:', e.message)
    })
}

function startCapture() {
    if (isCapturing()) return
    captureProcess = spawnHidden('python', [CAPTURE_SCRIPT], { cwd: ROOT })
    captureProcess.stdout.on('data', d => console.log('[Capture]', d.toString().trim()))
    captureProcess.stderr.on('data', d => console.error('[Capture ERR]', d.toString().trim()))
    captureProcess.on('exit', (code) => {
        console.log('[Capture] exited', code)
        captureProcess = null
        notifyVisionUnload()
        updateTrayIcon()
        rebuildTrayMenu()
        broadcastCaptureStatus()
    })
    updateTrayIcon()
    rebuildTrayMenu()
    broadcastCaptureStatus()
    console.log('[Capture] started pid=', captureProcess.pid)
}

function stopCapture() {
    if (!captureProcess) return
    const proc = captureProcess
    captureProcess = null
    if (process.platform === 'win32' && proc.pid) {
        spawnHidden('taskkill', ['/pid', String(proc.pid), '/T', '/F'])
    } else {
        proc.kill('SIGTERM')
    }
    // exit handler also notifies; call here so unload starts immediately
    notifyVisionUnload()
    updateTrayIcon()
    rebuildTrayMenu()
    broadcastCaptureStatus()
    console.log('[Capture] stopped')
}

function toggleCapture() {
    if (isCapturing()) stopCapture()
    else startCapture()
}

// ─────────────────────────────────────────────────────────────────────────────
// tray
// ─────────────────────────────────────────────────────────────────────────────

function rebuildTrayMenu() {
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
    tray = new Tray(getTrayIcon(false))
    tray.setToolTip('Clippy Vision — Idle')
    rebuildTrayMenu()
    tray.on('click', toggleCapture)
    tray.on('double-click', showMainWindow)
}

// ─────────────────────────────────────────────────────────────────────────────
// IPC handlers
// ─────────────────────────────────────────────────────────────────────────────

ipcMain.handle('toggle-capture',     () => { toggleCapture();  return isCapturing() })
ipcMain.handle('get-capture-status', () => isCapturing())

function broadcastUpdateStatus(status) {
    if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('update-status-changed', status)
    }
}

updater.onUpdateStateChanged(broadcastUpdateStatus)

ipcMain.handle('get-update-status', () => updater.getState())
ipcMain.handle('check-for-update', () => updater.checkForUpdate({ force: true }))
ipcMain.handle('start-update', () => updater.startUpdate({
    quitApp: () => {
        isQuitting = true
        stopCapture()
        app.quit()
    },
}))

ipcMain.handle('get-hardware-check', async () => getHardwareCheck())

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
    // Kick install after the renderer has switched views
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
    updater.startPeriodicChecks()
    // API server was already started during warmup step
})

// ─────────────────────────────────────────────────────────────────────────────
// app lifecycle
// ─────────────────────────────────────────────────────────────────────────────

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

// ─────────────────────────────────────────────────────────────────────────────
// app lifecycle
// ─────────────────────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
    // Ensure writable dirs exist (packaged → AppData; dev → repo)
    for (const d of [DATA_DIR, path.join(DATA_DIR, 'screenshots'), path.join(USER_DATA, 'logs')]) {
        if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true })
    }
    console.log(`[paths] packaged=${IS_PACKAGED}`)
    console.log(`[paths] ROOT=${ROOT}`)
    console.log(`[paths] USER_DATA=${USER_DATA}`)
    console.log(`[paths] DATA_DIR=${DATA_DIR}`)

    const setupDone = fs.existsSync(SETUP_FLAG)

    if (setupDone) {
        // Show loading UI immediately while preflight runs (no blank wait)
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
        sendLoadingStatus(
            null,
            'Starting AI server and loading models…'
        )
        startServer()
        pollUntilAlive('http://localhost:8000/health', 1000, 90)
            .then(async () => {
                console.log('[app] API server healthy — warming text model...')
                sendLoadingStatus(null, 'Loading text model…')
                try {
                    await httpPost('http://localhost:8000/residency/startup', {}, 120000)
                    console.log('[app] residency startup warm done')
                } catch (e) {
                    console.log('[app] residency warm skipped:', e.message)
                }
                if (mainWindow && !mainWindow.isDestroyed()) {
                    mainWindow.webContents.send('api-ready')
                }
                updater.startPeriodicChecks()
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
    })
})

app.on('window-all-closed', () => {
    // intentionally empty — tray keeps the app alive
})

app.on('before-quit', () => {
    isQuitting = true
    updater.stopPeriodicChecks()
    stopCapture()
    if (apiProcess) {
        spawnHidden('taskkill', ['/pid', String(apiProcess.pid), '/T', '/F'])
    }
})