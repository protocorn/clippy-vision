/**
 * GitHub Releases updater for Clippy Vision.
 *
 * Compares app.getVersion() to the latest release tag and can download + launch
 * ClippyVision-Setup-*.exe. No electron-updater / latest.yml required.
 */

const { app, shell } = require('electron')
const fs = require('fs')
const path = require('path')
const https = require('https')
const http = require('http')
const { spawn } = require('child_process')

const GITHUB_OWNER = 'protocorn'
const GITHUB_REPO = 'clippy-vision'
const RELEASES_API =
    `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/releases/latest`
const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000 // every 6 hours while outdated / idle
const USER_AGENT = 'ClippyVision-Updater'

/** @typedef {'idle'|'available'|'downloading'|'installing'|'error'|'up_to_date'} UpdatePhase */

function currentAppVersion() {
    try {
        return app.getVersion()
    } catch (_) {
        return '0.0.0'
    }
}

/** @type {{
 *   phase: UpdatePhase,
 *   currentVersion: string,
 *   latestVersion: string | null,
 *   releaseName: string | null,
 *   downloadUrl: string | null,
 *   progress: number,
 *   error: string | null,
 * }} */
let state = {
    phase: 'idle',
    currentVersion: '0.0.0',
    latestVersion: null,
    releaseName: null,
    downloadUrl: null,
    progress: 0,
    error: null,
}

/** @type {((s: typeof state) => void) | null} */
let onChange = null
let checkTimer = null
let checking = false
let installing = false

function setState(patch) {
    state = { ...state, ...patch }
    if (onChange) onChange(getState())
}

function getState() {
    return { ...state, currentVersion: state.currentVersion || currentAppVersion() }
}

function onUpdateStateChanged(cb) {
    onChange = cb
}

/** Parse "v1.2.3" / "1.2.3" → [1,2,3] */
function parseVersion(raw) {
    const cleaned = String(raw || '')
        .trim()
        .replace(/^v/i, '')
        .split(/[+-]/)[0] // drop pre-release / build metadata for compare
    const parts = cleaned.split('.').map((p) => parseInt(p, 10))
    if (!parts.length || parts.some((n) => Number.isNaN(n))) return null
    while (parts.length < 3) parts.push(0)
    return parts
}

/** @returns {number} negative if a < b, 0 if equal, positive if a > b */
function compareVersions(a, b) {
    const pa = parseVersion(a)
    const pb = parseVersion(b)
    if (!pa || !pb) return 0
    for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        const da = pa[i] || 0
        const db = pb[i] || 0
        if (da !== db) return da - db
    }
    return 0
}

function httpsGetJson(url) {
    return new Promise((resolve, reject) => {
        const req = https.get(
            url,
            {
                headers: {
                    'User-Agent': USER_AGENT,
                    Accept: 'application/vnd.github+json',
                },
                timeout: 20000,
            },
            (res) => {
                if (res.statusCode && res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
                    res.resume()
                    httpsGetJson(res.headers.location).then(resolve, reject)
                    return
                }
                if (res.statusCode !== 200) {
                    res.resume()
                    reject(new Error(`GitHub API HTTP ${res.statusCode}`))
                    return
                }
                const chunks = []
                res.on('data', (c) => chunks.push(c))
                res.on('end', () => {
                    try {
                        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')))
                    } catch (e) {
                        reject(e)
                    }
                })
            },
        )
        req.on('error', reject)
        req.on('timeout', () => {
            req.destroy()
            reject(new Error('GitHub API timeout'))
        })
    })
}

function pickInstallerAsset(assets) {
    if (!Array.isArray(assets)) return null
    const preferred = assets.find((a) =>
        /^ClippyVision-Setup-.*\.exe$/i.test(a.name || ''),
    )
    if (preferred?.browser_download_url) return preferred
    const anyExe = assets.find((a) => /\.exe$/i.test(a.name || ''))
    return anyExe?.browser_download_url ? anyExe : null
}

async function checkForUpdate({ force = false } = {}) {
    if (checking) return getState()
    if (installing && !force) return getState()
    checking = true
    try {
        const current = currentAppVersion()
        const release = await httpsGetJson(RELEASES_API)
        const latestRaw = release.tag_name || release.name || ''
        const latest = String(latestRaw).replace(/^v/i, '')
        const asset = pickInstallerAsset(release.assets)
        const newer = compareVersions(current, latest) < 0

        if (newer && asset) {
            setState({
                phase: 'available',
                currentVersion: current,
                latestVersion: latest,
                releaseName: release.name || `v${latest}`,
                downloadUrl: asset.browser_download_url,
                progress: 0,
                error: null,
            })
        } else {
            setState({
                phase: 'up_to_date',
                currentVersion: current,
                latestVersion: latest || current,
                releaseName: release.name || null,
                downloadUrl: null,
                progress: 0,
                error: null,
            })
        }
    } catch (e) {
        console.log('[updater] check failed:', e.message)
        // Keep prior "available" state if we already know an update exists
        if (state.phase !== 'available' && state.phase !== 'downloading') {
            setState({
                phase: state.phase === 'idle' ? 'idle' : state.phase,
                error: e.message,
            })
        }
    } finally {
        checking = false
    }
    return getState()
}

function downloadFile(url, dest, onProgress) {
    return new Promise((resolve, reject) => {
        const follow = (currentUrl, redirectsLeft) => {
            const client = currentUrl.startsWith('http://') ? http : https
            const req = client.get(
                currentUrl,
                {
                    headers: { 'User-Agent': USER_AGENT },
                    timeout: 120000,
                },
                (res) => {
                    if (
                        res.statusCode &&
                        res.statusCode >= 300 &&
                        res.statusCode < 400 &&
                        res.headers.location &&
                        redirectsLeft > 0
                    ) {
                        res.resume()
                        follow(res.headers.location, redirectsLeft - 1)
                        return
                    }
                    if (res.statusCode !== 200) {
                        res.resume()
                        reject(new Error(`Download HTTP ${res.statusCode}`))
                        return
                    }
                    const total = parseInt(res.headers['content-length'] || '0', 10)
                    let received = 0
                    const out = fs.createWriteStream(dest)
                    res.on('data', (chunk) => {
                        received += chunk.length
                        if (total > 0 && onProgress) {
                            onProgress(Math.min(99, Math.round((received / total) * 100)))
                        }
                    })
                    res.pipe(out)
                    out.on('finish', () => {
                        out.close(() => resolve(dest))
                    })
                    out.on('error', (err) => {
                        try { fs.unlinkSync(dest) } catch (_) {}
                        reject(err)
                    })
                },
            )
            req.on('error', reject)
            req.on('timeout', () => {
                req.destroy()
                reject(new Error('Download timeout'))
            })
        }
        follow(url, 5)
    })
}

/**
 * Download the setup exe and launch it. Quits the app so the installer can replace files.
 * @param {{ quitApp: () => void }} opts
 */
async function startUpdate({ quitApp }) {
    if (installing) return getState()
    if (state.phase !== 'available' || !state.downloadUrl) {
        await checkForUpdate({ force: true })
        if (state.phase !== 'available' || !state.downloadUrl) {
            setState({ phase: 'error', error: 'No update available to install' })
            return getState()
        }
    }

    installing = true
    const version = state.latestVersion || 'latest'
    const fileName = `ClippyVision-Setup-${version}.exe`
    const dest = path.join(app.getPath('temp'), fileName)

    try {
        setState({ phase: 'downloading', progress: 0, error: null })
        await downloadFile(state.downloadUrl, dest, (pct) => {
            setState({ phase: 'downloading', progress: pct })
        })

        setState({ phase: 'installing', progress: 100 })

        // Detached installer; NSIS will prompt UAC if needed
        const child = spawn(dest, [], {
            detached: true,
            stdio: 'ignore',
            windowsHide: false,
        })
        child.unref()

        // Brief pause so the process starts before we tear down
        setTimeout(() => {
            if (typeof quitApp === 'function') quitApp()
            else app.quit()
        }, 800)
    } catch (e) {
        console.error('[updater] install failed:', e)
        installing = false
        setState({
            phase: 'available',
            progress: 0,
            error: e.message || String(e),
        })
        // Fallback: open the release page if download/launch failed
        try {
            await shell.openExternal(
                `https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/latest`,
            )
        } catch (_) {}
    }
    return getState()
}

function startPeriodicChecks() {
    stopPeriodicChecks()
    // Initial check shortly after UI is up (don’t race startup)
    setTimeout(() => {
        checkForUpdate().catch(() => {})
    }, 4000)
    checkTimer = setInterval(() => {
        if (state.phase === 'downloading' || state.phase === 'installing') return
        checkForUpdate().catch(() => {})
    }, CHECK_INTERVAL_MS)
}

function stopPeriodicChecks() {
    if (checkTimer) {
        clearInterval(checkTimer)
        checkTimer = null
    }
}

module.exports = {
    getState,
    checkForUpdate,
    startUpdate,
    startPeriodicChecks,
    stopPeriodicChecks,
    onUpdateStateChanged,
    compareVersions,
}
