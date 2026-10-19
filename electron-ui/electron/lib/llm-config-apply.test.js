/**
 * Saving a new Ollama address must restart the API once with that URL.
 * Run with: node --test electron/lib/llm-config-apply.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert/strict')

const { createLlmConfigApply } = require('./llm-config-apply')

function createHarness(initialUrl) {
    let url = String(initialUrl || 'http://127.0.0.1:11434').replace(/\/+$/, '')
    const restarts = []
    const mcpRefreshes = []
    const llmConfig = {
        configuredOllamaBaseURL: () => url,
        saveLLMConfig: (values = {}) => {
            if (Object.prototype.hasOwnProperty.call(values, 'base_url')) {
                url = String(values.base_url || '').trim().replace(/\/+$/, '')
            }
            return { base_url: url }
        },
    }
    const api = {
        restartServer: async () => {
            restarts.push(llmConfig.configuredOllamaBaseURL())
        },
    }
    const apply = createLlmConfigApply({
        llmConfig,
        api,
        refreshMcpClientConfigs: async () => {
            mcpRefreshes.push(llmConfig.configuredOllamaBaseURL())
        },
    })
    return { apply, restarts, mcpRefreshes }
}

test('URL change triggers one restart with the new env', async () => {
    const { apply, restarts, mcpRefreshes } = createHarness('http://127.0.0.1:11434')
    await apply.saveAndApplyLLMConfig({ base_url: 'http://192.168.1.5:11434' })
    await apply.saveAndApplyLLMConfig({ base_url: 'http://192.168.1.5:11434' })
    assert.deepEqual(restarts, ['http://192.168.1.5:11434'])
    assert.deepEqual(mcpRefreshes, ['http://192.168.1.5:11434'])
})

test('saving the same URL does not restart', async () => {
    const { apply, restarts, mcpRefreshes } = createHarness('http://127.0.0.1:11434')
    await apply.saveAndApplyLLMConfig({ chat_model: 'qwen3:8b' })
    await apply.saveAndApplyLLMConfig({ base_url: 'http://127.0.0.1:11434' })
    assert.deepEqual(restarts, [])
    assert.deepEqual(mcpRefreshes, [])
})
