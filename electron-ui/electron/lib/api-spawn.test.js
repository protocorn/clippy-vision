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
        let url = 'http://127.0.0.1:11434'
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
        assert.equal(apiSpawns[0].env.CLIPPY_OLLAMA_URL, 'http://127.0.0.1:11434')
        assert.equal(apiSpawns[1].env.CLIPPY_OLLAMA_URL, 'http://192.168.1.5:11434')
    } finally {
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
