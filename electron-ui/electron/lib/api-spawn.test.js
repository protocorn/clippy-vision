/**
 * API child processes must share the stable MiniLM folder.
 * Run with: node --test electron/lib/api-spawn.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path')

const { createApiSpawn } = require('./api-spawn')
const { embeddingModelDir } = require('./paths')

test('buildPythonEnv sets CLIPPY_EMBEDDING_MODEL_DIR under userData', () => {
    const userData = path.join('/home/ada', '.config', 'Clippy Vision')
    const dataDir = path.join(userData, 'data')
    const embeddingDir = embeddingModelDir(userData)
    const api = createApiSpawn({
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
        },
        state: {},
    })

    const env = api.buildPythonEnv()
    assert.equal(env.CLIPPY_DATA_DIR, dataDir)
    assert.equal(env.CLIPPY_EMBEDDING_MODEL_DIR, embeddingDir)
    assert.equal(env.CLIPPY_EMBEDDING_MODEL_DIR, path.join(userData, 'models', 'embeddings', 'all-MiniLM-L6-v2'))
})
