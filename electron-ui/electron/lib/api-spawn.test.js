/**
 * API child processes must share the stable MiniLM folder and the Settings Ollama URL.
 * Run with: node --test electron/lib/api-spawn.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const { createApiSpawn } = require('./api-spawn')
const { embeddingModelDir } = require('./paths')

function spawnFor(userData, llmConfig = {}, extras = {}) {
    const dataDir = path.join(userData, 'data')
    const embeddingDir = embeddingModelDir(userData)
    return createApiSpawn({
        paths: {
            ROOT: '/opt/clippy',
            DATA_DIR: dataDir,
            USER_DATA: userData,
            EMBEDDING_MODEL_DIR: embeddingDir,
            API_SCRIPT: '/opt/clippy/api_server.py',
            CAPTURE_SCRIPT: '/opt/clippy/core/screen_capture.py',
            OLLAMA_COMMAND: 'ollama',
            API_STATE_FILE: path.join(userData, 'api_process.json'),
            CAPTURE_STATE_FILE: path.join(dataDir, 'capture_status.json'),
            PATH_HINTS: [],
            OLLAMA_MAX_LOADED_MODELS: '1',
            OLLAMA_NUM_PARALLEL: '1',
            DEFAULT_API_PORT: 8000,
            PYTHON_COMMAND: '/opt/clippy/python-venv/bin/python',
            ...(extras.paths || {}),
        },
        llmConfig: {
            configuredOllamaBaseURL: () => 'http://127.0.0.1:11434',
            usesManagedOllama: () => false,
            readLLMConfig: () => ({}),
            ...llmConfig,
        },
        state: extras.state || {},
        spawnFn: extras.spawnFn,
    })
}

function fakeProcess(pid) {
    const exitHandlers = []
    return {
        pid,
        stdout: { on() {} },
        stderr: { on() {} },
        on(event, cb) {
            if (event === 'exit') exitHandlers.push(cb)
        },
        once(event, cb) {
            if (event === 'exit') exitHandlers.push(cb)
        },
        kill() {
            for (const cb of exitHandlers.splice(0)) cb(0)
        },
    }
}

test('buildPythonEnv sets CLIPPY_EMBEDDING_MODEL_DIR under userData', () => {
    const userData = path.join('/home/ada', '.config', 'Clippy Vision')
    const api = spawnFor(userData)

    const env = api.buildPythonEnv()
    assert.equal(env.CLIPPY_DATA_DIR, path.join(userData, 'data'))
    assert.equal(env.CLIPPY_EMBEDDING_MODEL_DIR, embeddingModelDir(userData))
    assert.equal(env.CLIPPY_EMBEDDING_MODEL_DIR, path.join(userData, 'models', 'embeddings', 'all-MiniLM-L6-v2'))
})

test('buildPythonEnv sets CLIPPY_OLLAMA_URL from Settings when unset', () => {
    const previous = process.env.CLIPPY_OLLAMA_URL
    delete process.env.CLIPPY_OLLAMA_URL
    try {
        const api = spawnFor(path.join('/home/ada', '.config', 'Clippy Vision'), {
            configuredOllamaBaseURL: () => 'http://192.168.1.5:11434',
        })
        const env = api.buildPythonEnv()
        assert.equal(env.CLIPPY_OLLAMA_URL, 'http://192.168.1.5:11434')
    } finally {
        if (previous === undefined) delete process.env.CLIPPY_OLLAMA_URL
        else process.env.CLIPPY_OLLAMA_URL = previous
    }
})

test('buildPythonEnv does not override OLLAMA_HOST when Settings URL is default', () => {
    const previousUrl = process.env.CLIPPY_OLLAMA_URL
    const previousHost = process.env.OLLAMA_HOST
    delete process.env.CLIPPY_OLLAMA_URL
    process.env.OLLAMA_HOST = 'http://10.0.0.8:11434'
    try {
        for (const url of ['http://127.0.0.1:11434', 'http://localhost:11434', 'http://[::1]:11434', '']) {
            const api = spawnFor(path.join('/home', 'ada', '.config', 'Clippy Vision'), {
                configuredOllamaBaseURL: () => url,
            })
            const env = api.buildPythonEnv()
            assert.ok(
                !String(env.CLIPPY_OLLAMA_URL || '').trim(),
                `CLIPPY_OLLAMA_URL should be unset for default Settings URL ${url || '(empty)'}`,
            )
            assert.equal(env.OLLAMA_HOST, 'http://10.0.0.8:11434')
        }
    } finally {
        if (previousUrl === undefined) delete process.env.CLIPPY_OLLAMA_URL
        else process.env.CLIPPY_OLLAMA_URL = previousUrl
        if (previousHost === undefined) delete process.env.OLLAMA_HOST
        else process.env.OLLAMA_HOST = previousHost
    }
})

test('buildPythonEnv keeps an existing CLIPPY_OLLAMA_URL', () => {
    const previous = process.env.CLIPPY_OLLAMA_URL
    process.env.CLIPPY_OLLAMA_URL = 'http://10.0.0.8:11434'
    try {
        const api = spawnFor(path.join('/home/ada', '.config', 'Clippy Vision'), {
            configuredOllamaBaseURL: () => 'http://192.168.1.5:11434',
        })
        const env = api.buildPythonEnv()
        assert.equal(env.CLIPPY_OLLAMA_URL, 'http://10.0.0.8:11434')
        assert.equal(api.buildPythonEnv({ CLIPPY_OLLAMA_URL: 'http://10.0.0.9:11434' }).CLIPPY_OLLAMA_URL, 'http://10.0.0.9:11434')
    } finally {
        if (previous === undefined) delete process.env.CLIPPY_OLLAMA_URL
        else process.env.CLIPPY_OLLAMA_URL = previous
    }
})

test('restartServer respawns the API once with the new CLIPPY_OLLAMA_URL', async () => {
    const previous = process.env.CLIPPY_OLLAMA_URL
    delete process.env.CLIPPY_OLLAMA_URL
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-api-restart-'))
    try {
        let url = 'http://10.0.0.1:11434'
        const spawned = []
        let nextPid = 1
        const api = spawnFor(userData, {
            configuredOllamaBaseURL: () => url,
            readLLMConfig: () => ({ chat_model: 'qwen3:8b' }),
        }, {
            state: {},
            spawnFn: (_cmd, args, opts) => {
                spawned.push({ args, env: opts.env })
                return fakeProcess(nextPid++)
            },
        })

        await api.startServer()
        url = 'http://192.168.1.5:11434'
        await api.restartServer()
        const apiSpawns = spawned.filter((row) => row.args && row.args.some((arg) => String(arg).endsWith('api_server.py')))
        assert.equal(apiSpawns.length, 2)
        assert.equal(apiSpawns[0].env.CLIPPY_OLLAMA_URL, 'http://10.0.0.1:11434')
        assert.equal(apiSpawns[1].env.CLIPPY_OLLAMA_URL, 'http://192.168.1.5:11434')
    } finally {
        fs.rmSync(userData, { recursive: true, force: true })
        if (previous === undefined) delete process.env.CLIPPY_OLLAMA_URL
        else process.env.CLIPPY_OLLAMA_URL = previous
    }
})

test('restartServer SIGKILLs a SIGTERM-stuck API before spawning a replacement', async () => {
    const previous = process.env.CLIPPY_OLLAMA_URL
    delete process.env.CLIPPY_OLLAMA_URL
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-api-kill-'))
    try {
        const live = new Map()
        const events = []
        let nextPid = 100
        let apiSpawnedAt = []

        function makeStuckApi() {
            const pid = nextPid++
            const exitHandlers = []
            let dead = false
            const proc = {
                pid,
                stdout: { on() {} },
                stderr: { on() {} },
                on(event, cb) {
                    if (event === 'exit') exitHandlers.push(cb)
                },
                once(event, cb) {
                    if (event === 'exit') exitHandlers.push(cb)
                },
                kill(signal) {
                    if (dead) return
                    events.push({ pid, signal: signal || 'SIGTERM', at: Date.now() })
                    if (signal === 'SIGKILL') {
                        dead = true
                        live.delete(pid)
                        for (const cb of exitHandlers.splice(0)) cb(1)
                    }
                },
            }
            live.set(pid, proc)
            apiSpawnedAt.push({ pid, at: Date.now() })
            return proc
        }

        const state = {}
        const api = spawnFor(userData, {
            configuredOllamaBaseURL: () => 'http://192.168.1.5:11434',
            readLLMConfig: () => ({ chat_model: 'qwen3:8b' }),
        }, {
            state,
            spawnFn: (cmd, args) => {
                const isTaskkill = String(cmd).toLowerCase() === 'taskkill' ||
                    (Array.isArray(args) && args.includes('/F'))
                if (isTaskkill) {
                    const idx = (args || []).findIndex((arg) => String(arg).toLowerCase() === '/pid')
                    const pid = Number((args || [])[idx + 1])
                    const target = live.get(pid)
                    if (target) target.kill('SIGKILL')
                    return {
                        pid: 0,
                        stdout: { on() {} },
                        stderr: { on() {} },
                        on() {},
                        once() {},
                        kill() {},
                    }
                }
                return makeStuckApi()
            },
        })

        await api.startServer()
        const firstPid = state.apiProcess.pid
        assert.equal(live.size, 1)
        assert.ok(live.has(firstPid))

        await api.restartServer({ timeoutMs: 80 })
        const secondPid = state.apiProcess.pid
        assert.ok(secondPid)
        assert.notEqual(secondPid, firstPid)
        assert.equal(live.size, 1, 'exactly one API process should remain')
        assert.ok(live.has(secondPid))
        assert.ok(!live.has(firstPid))

        const firstSignals = events.filter((row) => row.pid === firstPid).map((row) => row.signal)
        assert.deepEqual(firstSignals, ['SIGTERM', 'SIGKILL'])
        const killedAt = events.find((row) => row.pid === firstPid && row.signal === 'SIGKILL').at
        const replacement = apiSpawnedAt.find((row) => row.pid === secondPid)
        assert.ok(replacement.at >= killedAt, 'replacement must start after the old process exits')
    } finally {
        fs.rmSync(userData, { recursive: true, force: true })
        if (previous === undefined) delete process.env.CLIPPY_OLLAMA_URL
        else process.env.CLIPPY_OLLAMA_URL = previous
    }
})

test('restartServer waits for a stuck request process to exit before starting a new API', async () => {
    const previous = process.env.CLIPPY_OLLAMA_URL
    delete process.env.CLIPPY_OLLAMA_URL
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-api-stuck-'))
    const stuckScript = path.join(userData, 'stuck-api.js')
    fs.writeFileSync(stuckScript, [
        "'use strict'",
        'try { process.on("SIGTERM", () => {}) } catch (_) {}',
        'try { process.on("SIGINT", () => {}) } catch (_) {}',
        'const http = require("http")',
        'const port = Number(process.env.CLIPPY_API_PORT || 0)',
        'http.createServer((_req, _res) => {',
        '  // Hold the request open the way a slow Ollama /api/generate does.',
        '}).listen(port, "127.0.0.1")',
        'setInterval(() => {}, 30000)',
        '',
    ].join('\n'))

    const http = require('node:http')
    const state = {}
    const api = spawnFor(userData, {
        configuredOllamaBaseURL: () => 'http://192.168.1.5:11434',
        readLLMConfig: () => ({ chat_model: 'qwen3:8b' }),
    }, {
        state,
        paths: {
            ROOT: userData,
            PYTHON_COMMAND: process.execPath,
            API_SCRIPT: stuckScript,
        },
    })

    function pidAlive(pid) {
        if (!pid) return false
        try {
            process.kill(pid, 0)
            return true
        } catch (_) {
            return false
        }
    }

    let stuckReq = null
    try {
        await api.startServer()
        const first = state.apiProcess
        const firstPid = first && first.pid
        assert.ok(firstPid)
        assert.equal(pidAlive(firstPid), true)

        stuckReq = http.get(`http://127.0.0.1:${state.apiPort}/stuck`, () => {})
        stuckReq.on('error', () => {})
        await new Promise((resolve) => setTimeout(resolve, 80))

        await api.restartServer({ timeoutMs: 250 })
        const second = state.apiProcess
        const secondPid = second && second.pid
        assert.ok(secondPid)
        assert.notEqual(secondPid, firstPid)
        assert.equal(pidAlive(firstPid), false, 'old API must be gone after restart')
        assert.equal(pidAlive(secondPid), true, 'new API must be the only live process')
    } finally {
        if (stuckReq) {
            try { stuckReq.destroy() } catch (_) { /* ignore */ }
        }
        try { await api.stopServer({ timeoutMs: 250 }) } catch (_) { /* ignore */ }
        if (state.apiProcess && state.apiProcess.pid) {
            try { process.kill(state.apiProcess.pid, 'SIGKILL') } catch (_) { /* ignore */ }
        }
        fs.rmSync(userData, { recursive: true, force: true })
        if (previous === undefined) delete process.env.CLIPPY_OLLAMA_URL
        else process.env.CLIPPY_OLLAMA_URL = previous
    }
})

test('restartServer is a no-op when the API is not running', async () => {
    const spawned = []
    const api = spawnFor(path.join('/home/ada', '.config', 'Clippy Vision'), {}, {
        spawnFn: () => {
            spawned.push('spawned')
            return fakeProcess(1)
        },
    })
    await api.restartServer()
    assert.deepEqual(spawned, [])
})
