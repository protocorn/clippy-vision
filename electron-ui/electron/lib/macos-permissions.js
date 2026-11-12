/**
 * macOS Screen Recording and Accessibility prompts.
 *
 * Capture (mss / CGWindowList) needs Screen Recording. Window titles,
 * password-field painting, and pynput need Accessibility. Electron asks
 * here on launch so a fresh Mac install is not a Settings scavenger hunt.
 *
 * Unsigned builds still show the system sheets because Info.plist carries
 * NSScreenCaptureUsageDescription and NSAccessibilityUsageDescription
 * (electron-ui/package.json build.mac.extendInfo). After the user grants
 * Screen Recording, macOS often requires an app restart before frames
 * actually appear.
 *
 * CI must not hang on those sheets. GitHub-hosted macOS runners have no
 * operator to click Allow.
 */
function envFlagTrue(value) {
    const text = String(value == null ? '' : value).trim().toLowerCase()
    return text === '1' || text === 'true' || text === 'yes'
}

function shouldSkipMacPermissionPrompts({
    platform = process.platform,
    env = process.env,
} = {}) {
    if (platform !== 'darwin') return true
    if (envFlagTrue(env.CLIPPY_SKIP_MAC_PERMISSIONS)) return true
    if (envFlagTrue(env.CI)) return true
    return false
}

async function requestMacPermissions({
    platform = process.platform,
    env = process.env,
    systemPreferences,
    desktopCapturer,
    log = console.log,
} = {}) {
    if (shouldSkipMacPermissionPrompts({ platform, env })) {
        const reason = platform !== 'darwin' ? 'not-macos' : 'ci-or-skip-flag'
        log(`[permissions] skipped (${reason})`)
        return {
            skipped: true,
            reason,
            accessibility: 'skipped',
            screenRecording: 'skipped',
        }
    }

    const result = {
        skipped: false,
        reason: null,
        accessibility: 'unknown',
        screenRecording: 'unknown',
    }

    try {
        if (systemPreferences && typeof systemPreferences.isTrustedAccessibilityClient === 'function') {
            const trusted = systemPreferences.isTrustedAccessibilityClient(false)
            if (trusted) {
                result.accessibility = 'granted'
            } else {
                systemPreferences.isTrustedAccessibilityClient(true)
                result.accessibility = 'prompted'
                log('[permissions] macOS Accessibility prompt requested')
            }
        }
    } catch (error) {
        result.accessibility = 'error'
        log('[permissions] Accessibility check failed:', error.message)
    }

    try {
        const status = systemPreferences && typeof systemPreferences.getMediaAccessStatus === 'function'
            ? systemPreferences.getMediaAccessStatus('screen')
            : 'unknown'
        result.screenRecording = status || 'unknown'
        if (status === 'granted') return result
        if (desktopCapturer && typeof desktopCapturer.getSources === 'function') {
            await desktopCapturer.getSources({ types: ['screen'] })
            result.screenRecording = status === 'denied' ? 'denied' : 'prompted'
            log('[permissions] macOS Screen Recording prompt requested')
        }
    } catch (error) {
        result.screenRecording = 'error'
        log('[permissions] Screen Recording check failed:', error.message)
    }

    return result
}

module.exports = {
    envFlagTrue,
    shouldSkipMacPermissionPrompts,
    requestMacPermissions,
}
