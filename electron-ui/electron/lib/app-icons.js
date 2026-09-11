const fs = require('fs')
const os = require('os')
const path = require('path')
const { execFileSync } = require('child_process')

/**
 * Resolve a process name to a filesystem path, then use Electron's
 * app.getFileIcon (Windows + macOS) to return a PNG data URL.
 * Linux returns null and the UI keeps the letter fallback.
 */
function createAppIcons({ app }) {
    const pathCache = new Map()
    const iconCache = new Map()
    const negativeCache = new Set()

    function normalizeKey(processName) {
        return String(processName || '').trim().toLowerCase()
    }

    function safeExec(command, args, opts = {}) {
        try {
            return execFileSync(command, args, {
                encoding: 'utf8',
                timeout: 2500,
                windowsHide: true,
                ...opts,
            }).trim()
        } catch (_) {
            return ''
        }
    }

    function resolveWindowsPath(processName) {
        const raw = String(processName || '').trim()
        if (!raw) return null
        if (path.isAbsolute(raw) && fs.existsSync(raw)) return raw

        const exeName = /\.exe$/i.test(raw) ? raw : `${raw}.exe`
        const procName = exeName.replace(/\.exe$/i, '')

        // Prefer a live process so we get the real install path.
        const live = safeExec('powershell.exe', [
            '-NoProfile',
            '-Command',
            `(Get-Process -Name '${procName.replace(/'/g, '')}' -ErrorAction SilentlyContinue | Select-Object -First 1).Path`,
        ])
        if (live && fs.existsSync(live)) return live

        const whereHit = safeExec('where.exe', [exeName]).split(/\r?\n/).map((line) => line.trim()).find(Boolean)
        if (whereHit && fs.existsSync(whereHit)) return whereHit

        // App Paths registry — works even when the process is not running.
        const reg = safeExec('reg.exe', [
            'query',
            `HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\${exeName}`,
            '/ve',
        ])
        const regMatch = reg.match(/REG_SZ\s+(.+)$/im)
        if (regMatch) {
            const candidate = regMatch[1].trim().replace(/^"|"$/g, '')
            if (candidate && fs.existsSync(candidate)) return candidate
        }

        return null
    }

    function resolveMacPath(processName) {
        const raw = String(processName || '').trim()
        if (!raw) return null
        if (raw.endsWith('.app') && fs.existsSync(raw)) return raw
        if (path.isAbsolute(raw) && fs.existsSync(raw)) {
            const appMatch = raw.match(/^(.*?\.app)(?:\/|$)/i)
            if (appMatch && fs.existsSync(appMatch[1])) return appMatch[1]
            return raw
        }

        // AppleScript "path to application" resolves many display / process names.
        const candidates = [raw, raw.replace(/\.app$/i, '')]
        for (const name of candidates) {
            const out = safeExec('osascript', [
                '-e',
                `try\nPOSIX path of (path to application "${name.replace(/"/g, '')}")\nend try`,
            ])
            if (out) {
                const cleaned = out.replace(/\/$/, '')
                if (fs.existsSync(cleaned)) return cleaned
            }
        }

        // Live process executable → climb to enclosing .app
        const live = safeExec('/bin/ps', ['-axo', 'comm=']).split(/\n/)
        const needle = raw.toLowerCase()
        for (const line of live) {
            const comm = line.trim()
            if (!comm) continue
            if (!comm.toLowerCase().includes(needle) && !path.basename(comm).toLowerCase().includes(needle)) continue
            const appMatch = comm.match(/^(.*?\.app)(?:\/|$)/i)
            if (appMatch && fs.existsSync(appMatch[1])) return appMatch[1]
            if (fs.existsSync(comm)) return comm
        }

        // Scan /Applications and ~/Applications for a matching bundle.
        const dirs = ['/Applications', path.join(os.homedir(), 'Applications')]
        for (const dir of dirs) {
            let entries = []
            try { entries = fs.readdirSync(dir) } catch (_) { continue }
            for (const entry of entries) {
                if (!entry.endsWith('.app')) continue
                const base = entry.replace(/\.app$/i, '').toLowerCase()
                if (base === needle || base.includes(needle) || needle.includes(base)) {
                    const full = path.join(dir, entry)
                    if (fs.existsSync(full)) return full
                }
            }
        }
        return null
    }

    function resolveAppPath(processName) {
        const key = normalizeKey(processName)
        if (!key) return null
        if (pathCache.has(key)) return pathCache.get(key)

        let resolved = null
        if (process.platform === 'win32') resolved = resolveWindowsPath(processName)
        else if (process.platform === 'darwin') resolved = resolveMacPath(processName)

        pathCache.set(key, resolved)
        return resolved
    }

    async function getIconDataUrl(processName) {
        const key = normalizeKey(processName)
        if (!key) return null
        if (iconCache.has(key)) return iconCache.get(key)
        if (negativeCache.has(key)) return null
        if (process.platform !== 'win32' && process.platform !== 'darwin') {
            negativeCache.add(key)
            return null
        }

        const filePath = resolveAppPath(processName)
        if (!filePath) {
            negativeCache.add(key)
            return null
        }

        try {
            const image = await app.getFileIcon(filePath, { size: 'normal' })
            if (!image || image.isEmpty()) {
                negativeCache.add(key)
                return null
            }
            const dataUrl = image.toDataURL()
            iconCache.set(key, dataUrl)
            return dataUrl
        } catch (_) {
            negativeCache.add(key)
            return null
        }
    }

    async function getIconsForProcesses(processNames = []) {
        const unique = []
        const seen = new Set()
        for (const name of processNames) {
            const key = normalizeKey(name)
            if (!key || seen.has(key)) continue
            seen.add(key)
            unique.push(String(name).trim())
        }

        const icons = {}
        await Promise.all(unique.map(async (name) => {
            const dataUrl = await getIconDataUrl(name)
            if (dataUrl) icons[normalizeKey(name)] = dataUrl
        }))
        return icons
    }

    return { getIconDataUrl, getIconsForProcesses }
}

module.exports = { createAppIcons }
