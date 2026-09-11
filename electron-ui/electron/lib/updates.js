const fs    = require('fs')
const https = require('https')

const RELEASE_CHECK_INTERVAL_MS = 12 * 60 * 60 * 1000

function createUpdates({ paths, app, getMainWindow }) {
    const {
        USER_DATA,
        DESKTOP_SETTINGS_FILE,
        RELEASE_CHECK_FILE,
        RELEASE_REPOSITORY,
    } = paths

    function readDesktopSettings() {
        let saved = {}
        try { saved = JSON.parse(fs.readFileSync(DESKTOP_SETTINGS_FILE, 'utf8')) || {} } catch (_) { }
        return {
            ...saved,
            updateCheckEnabled: saved.updateCheckEnabled !== false,
        }
    }

    function setUpdateCheckEnabled(enabled) {
        // The version lookup is the only outbound request Clippy makes, so it gets
        // an explicit switch rather than being buried in the update code path.
        const settings = { ...readDesktopSettings(), updateCheckEnabled: Boolean(enabled) }
        try {
            fs.mkdirSync(USER_DATA, { recursive: true })
            fs.writeFileSync(DESKTOP_SETTINGS_FILE, JSON.stringify(settings, null, 2) + '\n')
        } catch (error) {
            console.log('[updates] could not persist preference:', error.message)
        }
        return settings.updateCheckEnabled
    }

    function releaseVersionParts(value) {
        return String(value || '').replace(/^v/i, '').split(/[+-]/)[0].split('.').map((part) => {
            const number = parseInt(part, 10)
            return Number.isFinite(number) ? number : 0
        })
    }

    function isNewerVersion(candidate, current) {
        const next = releaseVersionParts(candidate)
        const installed = releaseVersionParts(current)
        for (let i = 0; i < Math.max(next.length, installed.length); i++) {
            if ((next[i] || 0) !== (installed[i] || 0)) return (next[i] || 0) > (installed[i] || 0)
        }
        return false
    }

    function fetchLatestRelease() {
        return new Promise((resolve, reject) => {
            const request = https.get(
                `https://api.github.com/repos/${RELEASE_REPOSITORY}/releases/latest`,
                { headers: { 'User-Agent': 'Clippy-Vision', Accept: 'application/vnd.github+json' } },
                (response) => {
                    let body = ''
                    response.setEncoding('utf8')
                    response.on('data', (chunk) => { body += chunk })
                    response.on('end', () => {
                        if (response.statusCode !== 200) return reject(new Error(`GitHub returned ${response.statusCode}`))
                        try { resolve(JSON.parse(body)) } catch (error) { reject(error) }
                    })
                },
            )
            request.setTimeout(5000, () => request.destroy(new Error('release check timeout')))
            request.on('error', reject)
        })
    }

    async function checkForLatestRelease() {
        // Release checks are throttled and best-effort so a network outage never
        // blocks a local app launch.
        const mainWindow = getMainWindow()
        if (!mainWindow || mainWindow.isDestroyed()) return
        if (!readDesktopSettings().updateCheckEnabled) return
        try {
            const previous = JSON.parse(fs.readFileSync(RELEASE_CHECK_FILE, 'utf8'))
            if (Date.now() - Number(previous.checkedAt || 0) < RELEASE_CHECK_INTERVAL_MS) return
        } catch (_) {                   }

        try {
            const release = await fetchLatestRelease()
            fs.mkdirSync(USER_DATA, { recursive: true })
            fs.writeFileSync(RELEASE_CHECK_FILE, JSON.stringify({ checkedAt: Date.now() }))
            const version = String(release.tag_name || release.name || '').trim()
            if (!version || !isNewerVersion(version, app.getVersion())) return
            const url = release.html_url || `https://github.com/${RELEASE_REPOSITORY}/releases/tag/${encodeURIComponent(version)}`
            const win = getMainWindow()
            if (win && !win.isDestroyed()) {
                win.webContents.send('release-available', { version, url, name: release.name || version })
            }
        } catch (error) {
            console.log('[updates] release check skipped:', error.message)
        }
    }

    return {
        readDesktopSettings,
        setUpdateCheckEnabled,
        checkForLatestRelease,
    }
}

module.exports = { createUpdates }
