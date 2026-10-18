const path = require('path')
const fs   = require('fs')
const os   = require('os')

function resolveUserConfigDir({ platform = process.platform, env = process.env, home = os.homedir() } = {}) {
    if (platform === 'darwin') {
        return path.join(home, 'Library', 'Application Support')
    }
    if (platform === 'win32') {
        return env.APPDATA || path.join(home, 'AppData', 'Roaming')
    }
    return env.XDG_CONFIG_HOME || path.join(home, '.config')
}

const MCP_RUNTIME_DIRNAME = 'mcp-runtime'

function isAppImageMountPath(filePath) {
    const normalized = String(filePath || '').replace(/\\/g, '/')
    return /(^|\/)tmp\/\.mount_[^/]+/i.test(normalized)
}

function looksLikeAppImageRuntime({ env = {}, root = '', platform = process.platform } = {}) {
    // Windows/macOS installers keep resources at a stable path. Only Linux
    // AppImage mounts (and APPIMAGE= extract-and-run) change every launch.
    if (platform === 'win32' || platform === 'darwin') return false
    if (env.APPIMAGE) return true
    return isAppImageMountPath(root) || isAppImageMountPath(env.APPDIR)
}

function entryHasStaleAppImageMount(entry, env = {}) {
    if (!entry || typeof entry !== 'object') return false
    const values = [entry.command, ...(Array.isArray(entry.args) ? entry.args : [])]
    if (entry.env && typeof entry.env === 'object') {
        values.push(entry.env.PYTHONPATH, entry.env.CLIPPY_DATA_DIR)
    }
    const appdir = String(env.APPDIR || '').replace(/\\/g, '/').replace(/\/+$/, '')
    return values.some((value) => {
        const normalized = String(value || '').replace(/\\/g, '/')
        if (isAppImageMountPath(normalized)) return true
        return Boolean(appdir) && normalized.startsWith(appdir)
    })
}

function shSingleQuote(value) {
    return `'${String(value).replace(/'/g, `'\\''`)}'`
}

function mcpClientDefinitions({ platform = process.platform, env = process.env, home = os.homedir() } = {}) {
    const userConfigDir = resolveUserConfigDir({ platform, env, home })
    return {
        cursor: {
            id: 'cursor',
            label: 'Cursor',
            method: 'deeplink+file',
            configPath: path.join(home, '.cursor', 'mcp.json'),
            hint: 'One-click install and writes ~/.cursor/mcp.json',
        },
        vscode: {
            id: 'vscode',
            label: 'VS Code',
            method: 'deeplink+file',
            configPath: path.join(userConfigDir, 'Code', 'User', 'mcp.json'),
            hint: 'Opens VS Code install prompt and writes User/mcp.json when possible',
        },
        'claude-desktop': {
            id: 'claude-desktop',
            label: 'Claude Desktop',
            method: 'file',
            configPath: path.join(userConfigDir, 'Claude', 'claude_desktop_config.json'),
            hint: 'Writes Claude\u2019s MCP config — fully quit and reopen Claude after connecting',
        },
    }
}

function createMcp({ paths, runCommand, shell, runtime } = {}) {
    const { ROOT, DATA_DIR } = paths
    const USER_DATA = paths.USER_DATA || DATA_DIR
    const runtimePlatform = runtime?.platform || process.platform
    const runtimeEnv = runtime ? (runtime.env || {}) : process.env

    const MCP_SERVER_KEY = 'clippy-vision'

    function usesAppImageRuntime() {
        return looksLikeAppImageRuntime({ env: runtimeEnv, root: ROOT, platform: runtimePlatform })
    }

    function bundledLauncherPath() {
        const name = runtimePlatform === 'win32' ? 'clippy-mcp.cmd' : 'clippy-mcp'
        return path.join(ROOT, 'scripts', name)
    }

    function stableRuntimeDir() {
        return path.join(USER_DATA, MCP_RUNTIME_DIRNAME)
    }

    function stableLauncherPath() {
        return path.join(USER_DATA, runtimePlatform === 'win32' ? 'clippy-mcp.cmd' : 'clippy-mcp')
    }

    function mcpPythonRoot() {
        return usesAppImageRuntime() ? stableRuntimeDir() : ROOT
    }

    function mcpLauncherPath() {
        return usesAppImageRuntime() ? stableLauncherPath() : bundledLauncherPath()
    }

    function mcpServerPath() {
        return path.join(mcpPythonRoot(), 'mcp_server.py')
    }

    function appImageRuntimeStamp() {
        const appImage = String(runtimeEnv.APPIMAGE || '')
        let mtime = ''
        try {
            if (appImage && fs.existsSync(appImage)) mtime = String(fs.statSync(appImage).mtimeMs)
        } catch (_) { /* ignore */ }
        return `${appImage}\n${mtime}\n${ROOT}\n`
    }

    function writeStableLinuxLauncher({ launcherPath, python, serverEntry, dataDir, pythonPath }) {
        const script = [
            '#!/usr/bin/env bash',
            '# Stable stdio entrypoint for MCP clients. Clippy rewrites this when the',
            '# AppImage remounts under a new /tmp/.mount_* path.',
            'set -euo pipefail',
            `export CLIPPY_DATA_DIR=${shSingleQuote(dataDir)}`,
            `export PYTHONPATH=${shSingleQuote(pythonPath)}`,
            'export PYTHONIOENCODING=utf-8',
            'export PYTHONUTF8=1',
            `exec ${shSingleQuote(python)} ${shSingleQuote(serverEntry)} "$@"`,
            '',
        ].join('\n')
        fs.mkdirSync(path.dirname(launcherPath), { recursive: true })
        fs.writeFileSync(launcherPath, script, { encoding: 'utf8', mode: 0o755 })
    }

    function syncAppImageMcpRuntime() {
        if (!usesAppImageRuntime()) return { ok: true, root: ROOT }
        const dest = stableRuntimeDir()
        const stampPath = `${dest}.stamp`
        const stamp = appImageRuntimeStamp()
        try {
            if (fs.existsSync(stampPath) && fs.existsSync(path.join(dest, 'mcp_server.py'))) {
                if (fs.readFileSync(stampPath, 'utf8') === stamp) {
                    return { ok: true, root: dest }
                }
            }
            fs.mkdirSync(USER_DATA, { recursive: true })
            const tmpDest = `${dest}.tmp-${process.pid}`
            try { fs.rmSync(tmpDest, { recursive: true, force: true }) } catch (_) { /* ignore */ }
            fs.cpSync(ROOT, tmpDest, { recursive: true, dereference: true })
            try { fs.rmSync(dest, { recursive: true, force: true }) } catch (_) { /* ignore */ }
            fs.renameSync(tmpDest, dest)
            fs.writeFileSync(stampPath, stamp, 'utf8')
            const appImage = String(runtimeEnv.APPIMAGE || '').trim()
            if (appImage) {
                fs.writeFileSync(path.join(USER_DATA, 'appimage.path'), `${appImage}\n`, 'utf8')
            }
            return { ok: true, root: dest }
        } catch (error) {
            try { fs.rmSync(`${dest}.tmp-${process.pid}`, { recursive: true, force: true }) } catch (_) { /* ignore */ }
            return { ok: false, root: dest, error: error.message || String(error) }
        }
    }

    async function resolvePythonExecutable() {
        // Prefer an absolute path so MCP client configs do not depend on PATH.
        // Prefer the app venv (paths.PYTHON_COMMAND) once setup has created it.
        const candidates = [
            String(runtimeEnv.CLIPPY_PYTHON || '').trim(),
            paths.PYTHON_COMMAND,
        ].filter(Boolean)

        for (const candidate of candidates) {
            if (path.isAbsolute(candidate) && fs.existsSync(candidate)) return candidate
        }

        const lookup = process.platform === 'win32' ? 'where' : 'which'
        for (const name of ['python', 'python3']) {
            const { code, stdout } = await runCommand(lookup, [name])
            if (code !== 0 || !stdout) continue
            const hit = stdout.split(/\r?\n/).map((line) => line.trim()).find((line) => line && fs.existsSync(line))
            if (hit) return hit
        }

        return paths.PYTHON_COMMAND
    }

    async function getMcpLaunchConfig() {
        const sync = syncAppImageMcpRuntime()
        const pythonRoot = mcpPythonRoot()
        const launcher = mcpLauncherPath()
        const serverEntry = mcpServerPath()
        const python = await resolvePythonExecutable()
        const issues = []
        if (!sync.ok) {
            issues.push(`Could not copy MCP runtime out of the AppImage mount: ${sync.error}`)
        }
        if (!fs.existsSync(serverEntry)) issues.push(`MCP server missing: ${serverEntry}`)
        if (!path.isAbsolute(python) || !fs.existsSync(python)) {
            issues.push(`Python not resolved to an absolute path (got: ${python})`)
        }
        // Launcher remains for manual/dev use; clients spawn python directly (more
        // reliable on Windows than stdio-spawning a .cmd file).
        if (usesAppImageRuntime() && sync.ok && runtimePlatform !== 'win32') {
            try {
                writeStableLinuxLauncher({
                    launcherPath: launcher,
                    python,
                    serverEntry,
                    dataDir: DATA_DIR,
                    pythonPath: pythonRoot,
                })
            } catch (error) {
                issues.push(`Could not write stable MCP launcher: ${error.message || error}`)
            }
        }
        if (!fs.existsSync(launcher)) issues.push(`Launcher missing: ${launcher}`)

        const env = {
            CLIPPY_DATA_DIR: DATA_DIR,
            PYTHONPATH: pythonRoot,
            PYTHONIOENCODING: 'utf-8',
            PYTHONUTF8: '1',
        }

        return {
            name: 'clippy-vision',
            // What MCP clients should run (stdio-safe on Windows).
            command: python,
            args: [serverEntry],
            env,
            launcher,
            serverEntry,
            dataDir: DATA_DIR,
            ready: issues.length === 0,
            issues,
            clientConfig: {
                command: python,
                args: [serverEntry],
                env,
            },
        }
    }

    function clientDefinitions() {
        return mcpClientDefinitions(runtime)
    }

    function readJsonObject(filePath) {
        if (!fs.existsSync(filePath)) return {}
        try {
            const parsed = JSON.parse(fs.readFileSync(filePath, 'utf8'))
            return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
        } catch (_) {
            return {}
        }
    }

    function writeJsonObject(filePath, value) {
        fs.mkdirSync(path.dirname(filePath), { recursive: true })
        const text = `${JSON.stringify(value, null, 2)}\n`
        const tmpPath = `${filePath}.clippy-tmp`
        try {
            fs.writeFileSync(filePath, text, 'utf8')
            return
        } catch (directError) {
            try {
                fs.writeFileSync(tmpPath, text, 'utf8')
                try { fs.unlinkSync(filePath) } catch (_) { /* replace via rename */ }
                fs.renameSync(tmpPath, filePath)
                return
            } catch (fallbackError) {
                try { fs.unlinkSync(tmpPath) } catch (_) { /* ignore */ }
                const err = new Error(
                    `Could not write ${filePath}: ${fallbackError.message || directError.message}`
                )
                err.code = fallbackError.code || directError.code
                throw err
            }
        }
    }

    function mcpBucketKeysForClient(clientId) {
        // VS Code user mcp.json uses "servers"; Cursor/Claude use "mcpServers".
        if (clientId === 'vscode') return ['servers', 'mcpServers']
        return ['mcpServers', 'servers']
    }

    function findClippyEntry(doc, launch) {
        if (!doc || typeof doc !== 'object') return null
        for (const key of ['mcpServers', 'servers']) {
            const bucket = doc[key]
            if (bucket && typeof bucket === 'object' && entryLooksLikeClippy(bucket[MCP_SERVER_KEY], launch)) {
                return { bucketKey: key, entry: bucket[MCP_SERVER_KEY] }
            }
        }
        if (entryLooksLikeClippy(doc[MCP_SERVER_KEY], launch)) {
            return { bucketKey: null, entry: doc[MCP_SERVER_KEY] }
        }
        return null
    }

    function entryLooksLikeClippy(entry, launch) {
        if (!entry || typeof entry !== 'object') return false
        const command = String(entry.command || '')
        const args = Array.isArray(entry.args) ? entry.args.map(String) : []
        if (args.some((arg) => /mcp_server\.py$/i.test(arg))) return true
        if (/clippy-mcp(\.cmd)?$/i.test(command) || /mcp_server\.py$/i.test(command)) return true
        if (launch?.command && path.normalize(command) === path.normalize(launch.command)) {
            if (!launch.args?.length) return true
            const wanted = path.normalize(String(launch.args[0] || ''))
            if (wanted && args.some((arg) => path.normalize(arg) === wanted)) return true
        }
        return false
    }

    function isMcpClientConnected(clientId, launch) {
        const def = clientDefinitions()[clientId]
        if (!def?.configPath) return false
        const doc = readJsonObject(def.configPath)
        return Boolean(findClippyEntry(doc, launch))
    }

    function clientConfigForFile(clientId, launch) {
        if (clientId === 'vscode') {
            return {
                type: 'stdio',
                command: launch.command,
                args: launch.args || [],
                env: { ...launch.env },
            }
        }
        return { ...launch.clientConfig }
    }

    function mergeMcpServerIntoFile(configPath, launch, clientId) {
        const doc = readJsonObject(configPath)
        const primary = mcpBucketKeysForClient(clientId)[0]
        if (!doc[primary] || typeof doc[primary] !== 'object') doc[primary] = {}
        doc[primary][MCP_SERVER_KEY] = clientConfigForFile(clientId, launch)
        // Avoid leaving a stale duplicate under the other key.
        for (const key of mcpBucketKeysForClient(clientId).slice(1)) {
            if (doc[key] && typeof doc[key] === 'object' && MCP_SERVER_KEY in doc[key]) {
                delete doc[key][MCP_SERVER_KEY]
            }
        }
        if (MCP_SERVER_KEY in doc && primary !== null) delete doc[MCP_SERVER_KEY]
        writeJsonObject(configPath, doc)
        return configPath
    }

    function removeMcpServerFromFile(configPath) {
        if (!fs.existsSync(configPath)) return { removed: false, path: configPath, writeFailed: false }
        const doc = readJsonObject(configPath)
        let removed = false
        for (const key of ['mcpServers', 'servers']) {
            if (doc[key] && typeof doc[key] === 'object' && MCP_SERVER_KEY in doc[key]) {
                delete doc[key][MCP_SERVER_KEY]
                removed = true
            }
        }
        if (MCP_SERVER_KEY in doc) {
            delete doc[MCP_SERVER_KEY]
            removed = true
        }
        if (!removed) return { removed: false, path: configPath, writeFailed: false }
        try {
            writeJsonObject(configPath, doc)
            // Verify the edit stuck (Cursor may lock mcp.json with EPERM).
            const verify = readJsonObject(configPath)
            if (findClippyEntry(verify, null)) {
                return { removed: false, path: configPath, writeFailed: true, error: 'Config still contains Clippy Vision after write.' }
            }
            return { removed: true, path: configPath, writeFailed: false }
        } catch (error) {
            return {
                removed: false,
                path: configPath,
                writeFailed: true,
                error: error.message || String(error),
            }
        }
    }

    function buildCursorInstallUrl(launch) {
        const config = Buffer.from(JSON.stringify(launch.clientConfig), 'utf8').toString('base64')
        return `cursor://anysphere.cursor-deeplink/mcp/install?name=${encodeURIComponent(launch.name)}&config=${config}`
    }

    function buildVscodeInstallUrl(launch) {
        const payload = {
            name: launch.name,
            type: 'stdio',
            command: launch.command,
            args: launch.args,
            env: launch.env,
        }
        return `vscode:mcp/install?${encodeURIComponent(JSON.stringify(payload))}`
    }

    async function listMcpClientStatus() {
        const launch = await getMcpLaunchConfig()
        const defs = clientDefinitions()
        const clients = Object.values(defs).map((def) => ({
            id: def.id,
            label: def.label,
            method: def.method,
            hint: def.hint,
            configPath: def.configPath || null,
            connected: isMcpClientConnected(def.id, launch),
        }))
        return { ready: launch.ready, issues: launch.issues, launch, clients }
    }

    async function connectMcpClient(clientId) {
        const launch = await getMcpLaunchConfig()
        if (!launch.ready) {
            return { ok: false, error: (launch.issues || []).join(' ') || 'MCP launcher is not ready.' }
        }
        const def = clientDefinitions()[clientId]
        if (!def) return { ok: false, error: `Unknown client: ${clientId}` }

        const result = {
            ok: true,
            clientId,
            wroteConfig: false,
            configPath: def.configPath || null,
            openedInstall: false,
            message: '',
        }

        if (def.configPath) {
            try {
                mergeMcpServerIntoFile(def.configPath, launch, clientId)
                result.wroteConfig = true
            } catch (error) {
                return {
                    ok: false,
                    error: `Could not write ${def.label} config (close ${def.label} and try again): ${error.message}`,
                }
            }
        }

        if (clientId === 'cursor') {
            try {
                await shell.openExternal(buildCursorInstallUrl(launch))
                result.openedInstall = true
                result.message = 'Opened Cursor install prompt and wrote ~/.cursor/mcp.json. Approve the prompt if Cursor asks.'
            } catch (error) {
                result.message = `Wrote ~/.cursor/mcp.json. Could not open Cursor automatically (${error.message}).`
            }
        } else if (clientId === 'vscode') {
            try {
                await shell.openExternal(buildVscodeInstallUrl(launch))
                result.openedInstall = true
                result.message = 'Opened VS Code install prompt and wrote User/mcp.json when possible.'
            } catch (error) {
                result.message = `Wrote VS Code mcp.json when possible. Could not open VS Code automatically (${error.message}).`
            }
        } else if (clientId === 'claude-desktop') {
            result.message = 'Wrote Claude Desktop config. Fully quit Claude and reopen it for the server to load.'
        }

        result.connected = isMcpClientConnected(clientId, launch)
        return result
    }

    async function refreshStaleMcpClientConfigs() {
        const launch = await getMcpLaunchConfig()
        const serverEntry = launch.args && launch.args[0]
        // Rewrite even when the launcher issue list is non-empty, as long as the
        // python entry we would write is a real, non-mount path.
        if (!serverEntry || !fs.existsSync(serverEntry)) {
            return { refreshed: [], issues: launch.issues }
        }
        const defs = clientDefinitions()
        const refreshed = []
        for (const def of Object.values(defs)) {
            if (!def.configPath || !fs.existsSync(def.configPath)) continue
            const doc = readJsonObject(def.configPath)
            const found = findClippyEntry(doc, launch)
            if (!found) continue
            if (!entryHasStaleAppImageMount(found.entry, runtimeEnv)) continue
            mergeMcpServerIntoFile(def.configPath, launch, def.id)
            refreshed.push({ clientId: def.id, configPath: def.configPath })
        }
        return { refreshed, issues: [] }
    }

    async function disconnectMcpClient(clientId) {
        const def = clientDefinitions()[clientId]
        if (!def) return { ok: false, error: `Unknown client: ${clientId}` }
        if (!def.configPath) {
            return { ok: false, error: 'This client has no config file Clippy can edit.' }
        }

        // Per-app only: remove Clippy from this client's config. Do NOT kill mcp_server
        // processes — other apps may still be connected to the same server.
        const removal = removeMcpServerFromFile(def.configPath)
        const launch = await getMcpLaunchConfig()
        const stillListed = isMcpClientConnected(clientId, launch)

        if (stillListed || removal.writeFailed) {
            return {
                ok: false,
                clientId,
                removed: false,
                configPath: def.configPath,
                connected: true,
                error: [
                    `Could not remove Clippy Vision from ${def.label}'s config`,
                    removal.error ? `(${removal.error})` : '',
                    `— fully quit ${def.label}, then click Disconnect again.`,
                ].filter(Boolean).join(' '),
            }
        }

        return {
            ok: true,
            clientId,
            removed: removal.removed,
            configPath: def.configPath,
            connected: false,
            message: [
                removal.removed
                    ? `Removed Clippy Vision from ${def.label} only.`
                    : `Clippy Vision was not listed in ${def.label}'s config.`,
                'Other apps stay connected. Reload MCP in this app if tools still appear.',
            ].join(' '),
        }
    }

    function registerMcpIpc(ipcMain, clipboard) {
        ipcMain.handle('mcp-get-launch-config', async () => getMcpLaunchConfig())
        ipcMain.handle('mcp-copy-config', async () => {
            const config = await getMcpLaunchConfig()
            const payload = {
                mcpServers: {
                    [config.name]: config.clientConfig,
                },
            }
            clipboard.writeText(JSON.stringify(payload, null, 2))
            return { ok: true, ready: config.ready, issues: config.issues }
        })
        ipcMain.handle('mcp-list-clients', async () => listMcpClientStatus())
        ipcMain.handle('mcp-connect-client', async (_event, clientId) => connectMcpClient(String(clientId || '')))
        ipcMain.handle('mcp-disconnect-client', async (_event, clientId) => disconnectMcpClient(String(clientId || '')))
    }

    return {
        getMcpLaunchConfig,
        listMcpClientStatus,
        connectMcpClient,
        disconnectMcpClient,
        refreshStaleMcpClientConfigs,
        registerMcpIpc,
    }
}

module.exports = {
    createMcp,
    mcpClientDefinitions,
    resolveUserConfigDir,
    isAppImageMountPath,
    looksLikeAppImageRuntime,
    entryHasStaleAppImageMount,
}
