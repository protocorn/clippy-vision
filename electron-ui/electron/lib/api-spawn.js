const { spawn } = require('child_process')
const path      = require('path')
const fs        = require('fs')
const http      = require('http')
const net       = require('net')

function createApiSpawn({ paths, llmConfig, state }) {
    const {
        ROOT,
        DATA_DIR,
        USER_DATA,
        API_SCRIPT,
        CAPTURE_SCRIPT,
        PYTHON_COMMAND,
        OLLAMA_COMMAND,
        API_STATE_FILE,
        CAPTURE_STATE_FILE,
        PATH_HINTS,
        OLLAMA_MAX_LOADED_MODELS,
        OLLAMA_NUM_PARALLEL,
        DEFAULT_API_PORT,
    } = paths
    const { configuredOllamaBaseURL, usesManagedOllama, readLLMConfig } = llmConfig

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
            ...extra,
        }
    }

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

    function apiUrl(pathname = '') {
        return `http://127.0.0.1:${state.apiPort || DEFAULT_API_PORT}${pathname}`
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
        if (state.apiPort) return state.apiPort
        try {
            state.apiPort = await findFreePort()
        } catch (error) {
            console.log('[API] free port lookup failed, using default:', error.message)
            state.apiPort = DEFAULT_API_PORT
        }
        return state.apiPort
    }

    function writeApiState(pid) {
        try {
            fs.mkdirSync(USER_DATA, { recursive: true })
            fs.writeFileSync(API_STATE_FILE, JSON.stringify({ pid, port: state.apiPort }, null, 2))
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
        if (state.apiProcess) return
        await ensureApiPort()
        const proc = spawnHidden(PYTHON_COMMAND, [API_SCRIPT], {
            cwd: ROOT,
            env: {
                CLIPPY_API_PORT: String(state.apiPort),
                CLIPPY_CHAT_MODEL: String(readLLMConfig().chat_model || '').trim(),
            },
        })
        state.apiProcess = proc
        writeApiState(proc.pid)
        proc.stdout.on('data', d => console.log('[API]', d.toString().trim()))
        proc.stderr.on('data', d => console.error('[API ERR]', d.toString().trim()))
        proc.on('exit', (code) => {
            console.log('[API] exited', code)
            if (state.apiProcess !== proc) return
            state.apiProcess = null
            clearApiState()
        })
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
        if (state.ollamaStartPromise) return state.ollamaStartPromise

        state.ollamaStartPromise = (async () => {
            if (state.ollamaProcess && !state.ollamaProcess.killed) {
                return ollamaReachable(waitTries, 1000)
            }
            console.log('[ollama] no server on', configuredOllamaBaseURL(), '— starting one')
            const proc = spawnHidden(OLLAMA_COMMAND, ['serve'], { cwd: ROOT, detached: false })
            state.ollamaProcess = proc
            // Ollama reports load failures and bind conflicts on stderr. Surfacing
            // them here is what turns an opaque HTTP 500 into a readable cause.
            proc.stdout.on('data', (d) => console.log('[ollama]', d.toString().trim()))
            proc.stderr.on('data', (d) => console.error('[ollama ERR]', d.toString().trim()))
            proc.on('exit', (code) => {
                console.log('[ollama] serve exited', code)
                if (state.ollamaProcess === proc) state.ollamaProcess = null
            })
            const alive = await ollamaReachable(waitTries, 1000)
            if (!alive) console.error('[ollama] server did not become reachable')
            return alive
        })()

        try {
            return await state.ollamaStartPromise
        } finally {
            state.ollamaStartPromise = null
        }
    }

    function isCapturing() {
        return state.captureProcess != null && !state.captureProcess.killed
    }

    function writeCaptureState(active) {
        try {
            fs.writeFileSync(CAPTURE_STATE_FILE, JSON.stringify({
                active: Boolean(active),
                pid: active && state.captureProcess ? state.captureProcess.pid : null,
                updated_at: Date.now() / 1000,
            }, null, 2))
        } catch (_) {                          }
    }

    let captureCallbacks = null

    function setCaptureCallbacks(callbacks) {
        captureCallbacks = callbacks
    }

    function startCapture() {
        // Capture is a separate Python process because keyboard hooks and image
        // processing must not block Electron's renderer or tray event loop.
        if (isCapturing()) return
        const proc = spawnHidden(PYTHON_COMMAND, [CAPTURE_SCRIPT], { cwd: ROOT })
        state.captureProcess = proc
        proc.stdout.on('data', d => console.log('[Capture]', d.toString().trim()))
        proc.stderr.on('data', d => console.error('[Capture ERR]', d.toString().trim()))
        proc.on('exit', (code) => {
            console.log('[Capture] exited', code)
            if (state.captureProcess !== proc) return
            state.captureProcess = null
            writeCaptureState(false)
            if (captureCallbacks) captureCallbacks.onCaptureStopped()
        })
        if (captureCallbacks) captureCallbacks.onCaptureStarted()
        writeCaptureState(true)
        console.log('[Capture] started pid=', state.captureProcess.pid)
    }

    function stopCapture() {
        // Windows needs a tree kill for child processes; POSIX systems can use the
        // normal termination signal and let the Python shutdown hook clean up.
        if (!state.captureProcess) return
        const proc = state.captureProcess
        state.captureProcess = null
        writeCaptureState(false)
        if (process.platform === 'win32' && proc.pid) {
            spawnHidden('taskkill', ['/pid', String(proc.pid), '/T', '/F'])
        } else {
            try { proc.kill('SIGTERM') } catch (_) { }
        }

        if (captureCallbacks) captureCallbacks.onCaptureStopped()
        console.log('[Capture] stopped')
    }

    function toggleCapture() {
        if (isCapturing()) stopCapture()
        else startCapture()
    }

    return {
        buildPythonEnv,
        spawnHidden,
        runCommand,
        pollUntilAlive,
        httpPost,
        apiUrl,
        findFreePort,
        ensureApiPort,
        writeApiState,
        clearApiState,
        clearStaleApiProcess,
        startServer,
        ensureOllamaParallelConfig,
        ollamaReachable,
        ensureOllamaServing,
        isCapturing,
        writeCaptureState,
        setCaptureCallbacks,
        startCapture,
        stopCapture,
        toggleCapture,
    }
}

module.exports = { createApiSpawn }
