/**
 * Home-screen "try this question" prompt policy.
 * Shown once after the first session summary (or ~30 minutes of capture).
 */
const DISMISSED_KEY = 'clippy.firstAnswerDismissed'

function isDismissed(value) {
    return value === true || value === '1' || value === 1
}

function shouldShowFirstAnswer({ dismissed = false, eligible = false, searching = false } = {}) {
    return Boolean(eligible) && !isDismissed(dismissed) && !searching
}

function anyMcpClientConnected(clients) {
    return (Array.isArray(clients) ? clients : []).some((client) => client && client.connected)
}

function connectLinkVisible(clients) {
    return !anyMcpClientConnected(clients)
}

module.exports = {
    DISMISSED_KEY,
    isDismissed,
    shouldShowFirstAnswer,
    anyMcpClientConnected,
    connectLinkVisible,
}
