const path = require('path')
const fs   = require('fs')
const http = require('http')

function createSetup({ paths, llmConfig, api, state, app, windowBridge, onLaunched }) {
    const {
        ROOT,
        DATA_DIR,
        USER_DATA,
        REQUIREMENTS,
        SETUP_FLAG,
        RESIDENCY_FILE,
        PYTHON_COMMAND,
        OLLAMA_COMMAND,
        OLLAMA_MAX_LOADED_MODELS,
    } = paths
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

    function stepUpdate(key, stateVal, sub) {
        sendSetup('step-update', { key, state: stateVal, sub })
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
                detail: 'Clippy needs Python 3.9+ on PATH. Install it, then continue.',
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
        // Download MiniLM + router from Hugging Face, then pull the user's local AI model
        // from Ollama. Capture uses accessibility + OCR (no vision model).
        stepUpdate('models', 'running', 'Downloading local ML models (MiniLM + router)...')
        log('> python -m core.model_download', 'dim')
        try {
            const { code, stdout, stderr } = await runCommand(PYTHON_COMMAND, ['-m', 'core.model_download'], { cwd: ROOT })
            if (stdout) log(stdout, 'dim')
            if (stderr) log(stderr, code === 0 ? 'dim' : 'err')
            if (code !== 0) {
                log('HF model download failed — embeddings/router will retry on first use.', 'err')
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
                stepUpdate(key, 'error', err.message)
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

        const py = await runCommand(PYTHON_COMMAND, ['--version'])
        if (py.code !== 0) {
            return describePreflightFailure('python', 'Python not found or not on PATH.')
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
        const pkgCheck = await runCommand(PYTHON_COMMAND, [
            '-c',
            'import fastapi, uvicorn, pynput, mss, PIL, psutil, imagehash, transformers, torch, sklearn',
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

        ipcMain.handle('retry-step', (_event, key) => {
            state.doneSoFar = Math.max(0, state.doneSoFar - 1)
            runSetup(key)
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

module.exports = { createSetup }
