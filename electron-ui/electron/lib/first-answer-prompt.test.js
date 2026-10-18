/**
 * First-answer prompt: show once, hide while searching, Connect link if needed.
 * Run with: node --test electron/lib/first-answer-prompt.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert/strict')

const {
    DISMISSED_KEY,
    isDismissed,
    shouldShowFirstAnswer,
    anyMcpClientConnected,
    connectLinkVisible,
} = require('./first-answer-prompt')

test('dismissed key is stable so the prompt cannot return after Dismiss', () => {
    assert.equal(DISMISSED_KEY, 'clippy.firstAnswerDismissed')
    assert.equal(isDismissed('1'), true)
    assert.equal(isDismissed(true), true)
    assert.equal(isDismissed(''), false)
    assert.equal(isDismissed(null), false)
})

test('prompt stays hidden until capture is eligible', () => {
    assert.equal(shouldShowFirstAnswer({ eligible: false, dismissed: false }), false)
    assert.equal(shouldShowFirstAnswer({ eligible: true, dismissed: false }), true)
})

test('prompt never returns after dismiss', () => {
    assert.equal(shouldShowFirstAnswer({ eligible: true, dismissed: '1' }), false)
    assert.equal(shouldShowFirstAnswer({ eligible: true, dismissed: true }), false)
})

test('prompt stays off the session list while the user is searching', () => {
    assert.equal(shouldShowFirstAnswer({ eligible: true, searching: true }), false)
})

test('Connect apps link only appears when no MCP client is connected', () => {
    assert.equal(connectLinkVisible([]), true)
    assert.equal(connectLinkVisible([{ id: 'cursor', connected: false }]), true)
    assert.equal(
        connectLinkVisible([
            { id: 'cursor', connected: false },
            { id: 'vscode', connected: true },
        ]),
        false,
    )
    assert.equal(anyMcpClientConnected([{ connected: true }]), true)
})
