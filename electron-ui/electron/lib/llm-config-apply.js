function createLlmConfigApply({ llmConfig, api, refreshMcpClientConfigs } = {}) {
    let applyPromise = null

    async function saveAndApplyLLMConfig(values = {}) {
        const previousUrl = String(llmConfig.configuredOllamaBaseURL() || '').trim()
        const saved = llmConfig.saveLLMConfig(values)
        const nextUrl = String(llmConfig.configuredOllamaBaseURL() || '').trim()
        if (previousUrl === nextUrl) return saved

        const run = async () => {
            // Rewrite Cursor/Claude/VS Code configs and the AppImage launcher.
            // Running MCP clients keep their old env until they reload the
            // server — Clippy does not kill other apps' mcp_server processes.
            if (typeof refreshMcpClientConfigs === 'function') {
                await refreshMcpClientConfigs()
            }
            if (typeof api?.restartServer === 'function') {
                await api.restartServer()
            }
        }

        const pending = applyPromise ? applyPromise.then(run, run) : run()
        applyPromise = pending
        try {
            await pending
        } finally {
            if (applyPromise === pending) applyPromise = null
        }
        return saved
    }

    return { saveAndApplyLLMConfig }
}

module.exports = { createLlmConfigApply }
