const fs = require('fs')

function createLlmConfig(paths) {
    const {
        DATA_DIR,
        LLM_CONFIG_FILE,
        OLLAMA_BASE_URL,
        LOCAL_EMBEDDING_MODEL,
        DEFAULT_LLM_CONFIG,
    } = paths

    function normalizeLLMConfig(values = {}) {
        const merged = { ...DEFAULT_LLM_CONFIG, ...values }
        const requestedProvider = String(merged.provider || 'ollama').toLowerCase()
        const supportedProvider = requestedProvider === 'ollama'

        // An old hosted-provider setting must never leave its remote URL behind
        // after being normalized to the local Ollama default.
        merged.provider = 'ollama'
        if (!values.base_url || !supportedProvider) {
            merged.base_url = OLLAMA_BASE_URL
        }
        merged.base_url = String(merged.base_url || '').trim().replace(/\/+$/, '')
        // Do not retain hosted credentials in a local-only build. Ollama's local
        // endpoint does not require an API key.
        merged.api_key = ''
        merged.cli_command = ''
        merged.chat_model = String(merged.chat_model || DEFAULT_LLM_CONFIG.chat_model).trim()
        // Drop leftover vision_model keys from older installs — setup never pulls VL.
        delete merged.vision_model
        // Embeddings remain a local Ollama responsibility and cannot be redirected
        // to a hosted service through the desktop settings.
        merged.embedding_model = LOCAL_EMBEDDING_MODEL
        return merged
    }

    function validateLLMConfig(values = {}) {
        for (const field of ['base_url', 'chat_model']) {
            if (Object.prototype.hasOwnProperty.call(values, field) && !String(values[field] || '').trim()) {
                throw new Error(`${field} cannot be empty.`)
            }
        }
        if (values.provider !== 'ollama' || !/^https?:\/\/[^\s]+$/i.test(values.base_url)) {
            throw new Error('Base URL must be a valid HTTP or HTTPS URL.')
        }
        if (values.chat_model.length > 240) throw new Error('chat_model is too long.')
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
        }).filter(([, envName]) => String(process.env[envName] || '').trim()).map(([field]) => field)
        return { ...safe, api_key_set: Boolean(config.api_key), environment_overrides }
    }

    function saveLLMConfig(values = {}) {
        if (Object.prototype.hasOwnProperty.call(values, 'provider')) {
            const provider = String(values.provider || '').trim().toLowerCase()
            const validProviders = new Set(['ollama'])
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
            nextValues.base_url = OLLAMA_BASE_URL
        }
        const next = normalizeLLMConfig(nextValues)

        validateLLMConfig(next)
        fs.mkdirSync(DATA_DIR, { recursive: true })
        fs.writeFileSync(LLM_CONFIG_FILE, JSON.stringify(next, null, 2) + '\n')
        return publicLLMConfig()
    }

    function openProviderAuth() {
        // Hosted and subscription sign-in is intentionally a no-op while Clippy
        // Vision's product boundary is 100% local.
        return {
            ok: false,
            error: 'Hosted and subscription providers are disabled. Clippy Vision uses local Ollama.',
        }
    }

    function configuredOllamaBaseURL() {
        const config = readLLMConfig()
        return config.provider === 'ollama' ? config.base_url : OLLAMA_BASE_URL
    }

    function usesManagedOllama() {
        const baseURL = configuredOllamaBaseURL().toLowerCase().replace(/\/+$/, '')
        return new Set([
            'http://127.0.0.1:11434',
            'http://localhost:11434',
            'http://[::1]:11434',
        ]).has(baseURL)
    }

    return {
        normalizeLLMConfig,
        validateLLMConfig,
        readLLMConfig,
        publicLLMConfig,
        saveLLMConfig,
        openProviderAuth,
        configuredOllamaBaseURL,
        usesManagedOllama,
    }
}

module.exports = { createLlmConfig }
