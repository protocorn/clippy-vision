/**
 * LLM config default is qwen3:8b; explicit chat_model is preserved.
 * Run with: node --test electron/lib/llm-config.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { createLlmConfig } = require('./llm-config')
const { DEFAULT_CHAT_MODEL } = require('./paths')

const OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
const LOCAL_EMBEDDING_MODEL = 'local:sentence-transformers/all-MiniLM-L6-v2'
const DEFAULT_LLM_CONFIG = {
    provider: 'ollama',
    base_url: OLLAMA_BASE_URL,
    api_key: '',
    cli_command: '',
    chat_model: DEFAULT_CHAT_MODEL,
    embedding_model: LOCAL_EMBEDDING_MODEL,
}

function makeConfig(dir) {
    return createLlmConfig({
        DATA_DIR: dir,
        LLM_CONFIG_FILE: path.join(dir, 'llm_config.json'),
        OLLAMA_BASE_URL,
        LOCAL_EMBEDDING_MODEL,
        DEFAULT_LLM_CONFIG,
    })
}

function withClearedChatEnv(fn) {
    const keys = [
        'CLIPPY_CHAT_MODEL',
        'CLIPPY_LLM_PROVIDER',
        'CLIPPY_LLM_BASE_URL',
        'CLIPPY_LLM_API_KEY',
        'CLIPPY_CLI_COMMAND',
    ]
    const previous = Object.fromEntries(keys.map((key) => [key, process.env[key]]))
    for (const key of keys) delete process.env[key]
    try {
        return fn()
    } finally {
        for (const key of keys) {
            if (previous[key] === undefined) delete process.env[key]
            else process.env[key] = previous[key]
        }
    }
}

test('new installs without llm_config.json use qwen3:8b', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-llm-'))
    try {
        withClearedChatEnv(() => {
            const cfg = makeConfig(dir)
            assert.equal(cfg.readLLMConfig().chat_model, DEFAULT_CHAT_MODEL)
            assert.equal(DEFAULT_CHAT_MODEL, 'qwen3:8b')
        })
    } finally {
        fs.rmSync(dir, { recursive: true, force: true })
    }
})

test('existing llm_config.json without chat_model uses qwen3:8b', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-llm-'))
    fs.writeFileSync(path.join(dir, 'llm_config.json'), JSON.stringify({
        provider: 'ollama',
        base_url: 'http://127.0.0.1:11434',
    }))
    try {
        withClearedChatEnv(() => {
            const cfg = makeConfig(dir)
            assert.equal(cfg.readLLMConfig().chat_model, 'qwen3:8b')
        })
    } finally {
        fs.rmSync(dir, { recursive: true, force: true })
    }
})

test('existing llm_config.json with an explicit chat_model is unchanged', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-llm-'))
    fs.writeFileSync(path.join(dir, 'llm_config.json'), JSON.stringify({
        provider: 'ollama',
        base_url: 'http://127.0.0.1:11434',
        chat_model: 'llama3.2:3b',
    }))
    try {
        withClearedChatEnv(() => {
            const cfg = makeConfig(dir)
            assert.equal(cfg.readLLMConfig().chat_model, 'llama3.2:3b')
        })
    } finally {
        fs.rmSync(dir, { recursive: true, force: true })
    }
})
