const path = require('path')
const fs   = require('fs')
const os   = require('os')

function createMcp({ paths, runCommand, shell }) {
    const { ROOT, DATA_DIR, PYTHON_COMMAND } = paths

    const MCP_SERVER_KEY = 'clippy-vision'

    function mcpLauncherPath() {
        const name = process.platform === 'win32' ? 'clippy-mcp.cmd' : 'clippy-mcp'
        return path.join(ROOT, 'scripts', name)
    }

    function mcpServerPath() {
        return path.join(ROOT, 'mcp_server.py')
    }

    async function resolvePythonExecutable() {
        // Prefer an absolute path so MCP client configs do not depend on PATH.
        const candidates = [
            (process.env.CLIPPY_PYTHON || '').trim(),
            PYTHON_COMMAND,
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

        return PYTHON_COMMAND
    }

    async function getMcpLaunchConfig() {
        const launcher = mcpLauncherPath()
        const serverEntry = mcpServerPath()
        const python = await resolvePythonExecutable()
        const issues = []
        if (!fs.existsSync(serverEntry)) issues.push(`MCP server missing: ${serverEntry}`)
        if (!path.isAbsolute(python) || !fs.existsSync(python)) {
            issues.push(`Python not resolved to an absolute path (got: ${python})`)
        }
        // Launcher remains for manual/dev use; clients spawn python directly (more
        // reliable on Windows than stdio-spawning a .cmd file).
        if (!fs.existsSync(launcher)) issues.push(`Launcher missing: ${launcher}`)

        const env = {
            CLIPPY_DATA_DIR: DATA_DIR,
            PYTHONPATH: ROOT,
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

    function mcpClientDefinitions() {
        const home = os.homedir()
        const appData = process.env.APPDATA || path.join(home, 'AppData', 'Roaming')
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
                configPath: path.join(appData, 'Code', 'User', 'mcp.json'),
                hint: 'Opens VS Code install prompt and writes User/mcp.json when possible',
            },
            'claude-desktop': {
                id: 'claude-desktop',
                label: 'Claude Desktop',
                method: 'file',
                configPath: process.platform === 'darwin'
                    ? path.join(home, 'Library', 'Application Support', 'Claude', 'claude_desktop_config.json')
                    : path.join(appData, 'Claude', 'claude_desktop_config.json'),
                hint: 'Writes Claude\u2019s MCP config — fully quit and reopen Claude after connecting',
            },
        }
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
        const def = mcpClientDefinitions()[clientId]
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
        const defs = mcpClientDefinitions()
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
        const def = mcpClientDefinitions()[clientId]
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

    async function disconnectMcpClient(clientId) {
        const def = mcpClientDefinitions()[clientId]
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
        registerMcpIpc,
    }
}

module.exports = { createMcp }
