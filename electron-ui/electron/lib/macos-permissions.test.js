/**
 * macOS permission prompts must not block CI and must no-op off Darwin.
 * Run with: node --test electron/lib/macos-permissions.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert/strict')

const {
    envFlagTrue,
    shouldSkipMacPermissionPrompts,
    requestMacPermissions,
} = require('./macos-permissions')

test('CI=true skips macOS permission sheets', () => {
    assert.equal(shouldSkipMacPermissionPrompts({ platform: 'darwin', env: { CI: 'true' } }), true)
    assert.equal(shouldSkipMacPermissionPrompts({ platform: 'darwin', env: { CI: '1' } }), true)
})

test('CLIPPY_SKIP_MAC_PERMISSIONS skips even when CI is unset', () => {
    assert.equal(
        shouldSkipMacPermissionPrompts({
            platform: 'darwin',
            env: { CLIPPY_SKIP_MAC_PERMISSIONS: '1' },
        }),
        true,
    )
})

test('real macOS launches still prompt when CI flags are absent', () => {
    assert.equal(shouldSkipMacPermissionPrompts({ platform: 'darwin', env: {} }), false)
})

test('Windows and Linux never request macOS TCC prompts', () => {
    assert.equal(shouldSkipMacPermissionPrompts({ platform: 'win32', env: {} }), true)
    assert.equal(shouldSkipMacPermissionPrompts({ platform: 'linux', env: {} }), true)
})

test('envFlagTrue accepts 1/true/yes only', () => {
    assert.equal(envFlagTrue('true'), true)
    assert.equal(envFlagTrue('YES'), true)
    assert.equal(envFlagTrue('1'), true)
    assert.equal(envFlagTrue('0'), false)
    assert.equal(envFlagTrue(''), false)
})

test('requestMacPermissions no-ops on Windows without touching Electron APIs', async () => {
    let called = false
    const result = await requestMacPermissions({
        platform: 'win32',
        env: {},
        systemPreferences: {
            isTrustedAccessibilityClient() { called = true; return false },
            getMediaAccessStatus() { called = true; return 'not-determined' },
        },
        desktopCapturer: {
            getSources() { called = true; return Promise.resolve([]) },
        },
        log() {},
    })
    assert.equal(result.skipped, true)
    assert.equal(result.reason, 'not-macos')
    assert.equal(called, false)
})

test('requestMacPermissions skips in GitHub Actions so unsigned launch tests cannot hang', async () => {
    let called = false
    const result = await requestMacPermissions({
        platform: 'darwin',
        env: { CI: 'true' },
        systemPreferences: {
            isTrustedAccessibilityClient() { called = true; return false },
        },
        desktopCapturer: {
            getSources() { called = true; return Promise.resolve([]) },
        },
        log() {},
    })
    assert.equal(result.skipped, true)
    assert.equal(result.reason, 'ci-or-skip-flag')
    assert.equal(called, false)
})

test('requestMacPermissions prompts Accessibility and Screen Recording on a fresh Mac', async () => {
    const calls = []
    const result = await requestMacPermissions({
        platform: 'darwin',
        env: {},
        systemPreferences: {
            isTrustedAccessibilityClient(prompt) {
                calls.push(['accessibility', prompt])
                return false
            },
            getMediaAccessStatus(kind) {
                calls.push(['media', kind])
                return 'not-determined'
            },
        },
        desktopCapturer: {
            getSources(opts) {
                calls.push(['sources', opts])
                return Promise.resolve([{ id: 'screen:0' }])
            },
        },
        log() {},
    })
    assert.equal(result.skipped, false)
    assert.equal(result.accessibility, 'prompted')
    assert.equal(result.screenRecording, 'prompted')
    assert.deepEqual(calls, [
        ['accessibility', false],
        ['accessibility', true],
        ['media', 'screen'],
        ['sources', { types: ['screen'] }],
    ])
})

test('requestMacPermissions does not re-prompt Screen Recording when already granted', async () => {
    let sources = 0
    const result = await requestMacPermissions({
        platform: 'darwin',
        env: {},
        systemPreferences: {
            isTrustedAccessibilityClient() { return true },
            getMediaAccessStatus() { return 'granted' },
        },
        desktopCapturer: {
            getSources() {
                sources += 1
                return Promise.resolve([])
            },
        },
        log() {},
    })
    assert.equal(result.accessibility, 'granted')
    assert.equal(result.screenRecording, 'granted')
    assert.equal(sources, 0)
})
