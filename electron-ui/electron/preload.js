const { contextBridge, ipcRenderer } = require('electron')

/**
 * Extract a human-readable error message from a failed response.
 * FastAPI returns errors as JSON with a "detail" field (string or array).
 * Falls back to "HTTP {status}" if the body isn't JSON or has no detail.
 */
/**
 * The API binds a free loopback port chosen by the main process at launch, so
 * the base URL is resolved once over IPC and reused for every later request.
 */
let apiBasePromise = null
async function apiUrl(pathname) {
    if (!apiBasePromise) apiBasePromise = ipcRenderer.invoke('get-api-base')
    return `${await apiBasePromise}${pathname}`
}

async function getErrorMessage(response) {
    try {
        const data = await response.json()
        if (data && data.detail !== undefined) {
            if (Array.isArray(data.detail)) {
                // FastAPI validation errors: array of {loc, msg, type}
                return data.detail.map((d) => d.msg || JSON.stringify(d)).join('; ')
            }
            return String(data.detail)
        }
    } catch (_) {
        // Response wasn't JSON — fall through
    }
    return `HTTP ${response.status}`
}

contextBridge.exposeInMainWorld('clippy', {

    chat: async (message, conversationId) => {
        const response = await fetch(await apiUrl('/chat'), {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                message,
                conversation_id: conversationId,
            })
        })
        
        if (!response.ok) throw new Error(await getErrorMessage(response))
        return response.json()
    },

    chatStream: async (message, conversationId, onEvent) => {
        const response = await fetch(await apiUrl('/chat/stream'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message,
                conversation_id: conversationId,
            }),
        })
        if (!response.ok) throw new Error(await getErrorMessage(response))
        if (!response.body) throw new Error('No response body')

        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        while (true) {
            const { done, value } = await reader.read()
            if (done) break
            buffer += decoder.decode(value, { stream: true })
            const parts = buffer.split('\n\n')
            buffer = parts.pop() || ''
            for (const part of parts) {
                const line = part.split('\n').find(l => l.startsWith('data: '))
                if (!line) continue
                try {
                    const event = JSON.parse(line.slice(6))
                    onEvent(event)
                } catch (_) { /* ignore malformed chunk */ }
            }
        }
    },

    getName: async () => {
        const response = await fetch(await apiUrl('/user/name'))
        if (!response.ok) throw new Error(await getErrorMessage(response))
        return response.json()
    },

    setName: async (name) => {
        const response = await fetch(await apiUrl('/user/name'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        })
        if (!response.ok) throw new Error(await getErrorMessage(response))
        return response.json()
    },

    getProfile: async () => {
        const response = await fetch(await apiUrl('/user/profile'))
        if (!response.ok) throw new Error(await getErrorMessage(response))
        return response.json()
    },

    updateProfile: async (payload) => {
        const response = await fetch(await apiUrl('/user/profile'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
        if (!response.ok) throw new Error(await getErrorMessage(response))
        return response.json()
    },

    getPrivacySettings: async () => {
        const response = await fetch(await apiUrl('/settings/privacy'))
        if (!response.ok) throw new Error(await getErrorMessage(response))
        return response.json()
    },

    updatePrivacySettings: async (enabled) => {
        const response = await fetch(await apiUrl('/settings/privacy'), {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled }),
        })
        if (!response.ok) throw new Error(await getErrorMessage(response))
        return response.json()
    },

    listConversations: async () => {
        const response = await fetch(await apiUrl('/conversations'))
        if (!response.ok) throw new Error(await getErrorMessage(response))
        return response.json()
    },

    searchConversations: async (query, limit = 20) => {
        const params = new URLSearchParams({ q: query || '', limit: String(limit) })
        const response = await fetch(await apiUrl(`/conversations/search?${params}`))
        if (!response.ok) throw new Error(await getErrorMessage(response))
        return response.json()
    },

    getConversation: async (conversationId) => {
        const response = await fetch(
            await apiUrl(`/conversations/${encodeURIComponent(conversationId)}`)
        )
        if (!response.ok) throw new Error(await getErrorMessage(response))
        return response.json()
    },

    deleteConversation: async (conversationId) => {
        const response = await fetch(
            await apiUrl(`/conversations/${encodeURIComponent(conversationId)}`),
            { method: 'DELETE' }
        )
        if (!response.ok) throw new Error(await getErrorMessage(response))
        return response.json()
    },

    checkHealth: async () => {
        const response = await fetch(await apiUrl('/health'))
        return response.ok
    },

    getUpdateCheckEnabled: () => ipcRenderer.invoke('get-update-check'),

    setUpdateCheckEnabled: (enabled) => ipcRenderer.invoke('set-update-check', Boolean(enabled)),

    getAppVersion: () => ipcRenderer.invoke('get-app-version'),

    toggleCapture: () => ipcRenderer.invoke('toggle-capture'),

    getCaptureStatus: () => ipcRenderer.invoke('get-capture-status'),

    onCaptureStatusChanged: (callback) => {
        ipcRenderer.on('capture-status-changed', (_event, active) => callback(active))
    },

    onApiReady: (callback) => {
        ipcRenderer.on('api-ready', () => callback())
    },

    onLoadingStatus: (callback) => {
        ipcRenderer.on('loading-status', (_event, data) => callback(data))
    },

    onReleaseAvailable: (callback) => {
        ipcRenderer.on('release-available', (_event, data) => callback(data))
    },

})