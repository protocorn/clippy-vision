const {
    app, ipcMain, shell, dialog, globalShortcut, clipboard,
} = require('electron')
const path = require('path')
const fs   = require('fs')
const { execFileSync } = require('child_process')

const { createPaths }     = require('./lib/paths')
const { createLlmConfig } = require('./lib/llm-config')
const { createApiSpawn }  = require('./lib/api-spawn')
const { createMcp }       = require('./lib/mcp')
const { createHardware }  = require('./lib/hardware')
const { createUpdates }   = require('./lib/updates')
const { createWindows }   = require('./lib/windows')
const { createSetup }     = require('./lib/setup')
const { createAppIcons }  = require('./lib/app-icons')

const IS_PACKAGED = app.isPackaged
if (!IS_PACKAGED) {
    // Installed Clippy and `npm start` share appId; without a separate userData
    // the installer instance steals the lock and `npm start` exits instantly.
    app.setPath('userData', path.join(__dirname, '..', '.dev-electron'))
}

// Keep one desktop process alive so a second launch focuses the existing tray app.
if (!app.requestSingleInstanceLock()) {
    app.quit()
    process.exit(0)
}

const paths = createPaths(app, __dirname)
const llmConfig = createLlmConfig(paths)

const state = {
    mainWindow: null,
    setupWindow: null,
    tray: null,
    captureProcess: null,
    apiProcess: null,
    ollamaProcess: null,
    ollamaStartPromise: null,
    isQuitting: false,
    allowMainClose: false,
    apiPort: Number(process.env.CLIPPY_API_PORT) || 0,
    apiReady: false,
    apiReadyWaiters: [],
    doneSoFar: 0,
    setupStepTotal: 6,
    setupStartFrom: 'python',
    setupInstallStarted: false,
    setupRecovery: null,
}

const api = createApiSpawn({ paths, llmConfig, state })
const mcp = createMcp({ paths, runCommand: api.runCommand, shell })
const hardware = createHardware({ paths, llmConfig, runCommand: api.runCommand })
const updates = createUpdates({ paths, app, getMainWindow: () => state.mainWindow })
const windows = createWindows({ paths, state, api, updates, shell, app })
const setup = createSetup({
    paths,
    llmConfig,
    api,
    state,
    app,
    windowBridge: windows.windowBridge,
})
app.on('second-instance', () => {
    if (state.setupWindow && !state.setupWindow.isDestroyed()) {
        state.setupWindow.show()
        state.setupWindow.focus()
    } else {
        windows.showMainWindow()
    }
})

// Expose only narrow, validated desktop actions to the isolated renderer.
ipcMain.handle('toggle-capture',     () => { api.toggleCapture();  return api.isCapturing() })
ipcMain.handle('get-capture-status', () => api.isCapturing())
ipcMain.handle('get-login-item', () => app.getLoginItemSettings().openAtLogin)
ipcMain.handle('set-login-item', (_event, enabled) => {
    const openAtLogin = Boolean(enabled)
    app.setLoginItemSettings({ openAtLogin })
    return app.getLoginItemSettings().openAtLogin
})
ipcMain.handle('save-text-file', async (_event, payload = {}) => {
    const content = String(payload.content || '')
    if (content.length > 20_000_000) throw new Error('Export is too large to save from the desktop UI.')
    const requestedName = path.basename(String(payload.filename || 'clippy-export.json'))
    const safeName = requestedName && requestedName !== '.' && requestedName !== '..'
        ? requestedName
        : 'clippy-export.json'
    const filename = safeName.toLowerCase().endsWith('.json') ? safeName : `${safeName}.json`
    const result = await dialog.showSaveDialog(state.mainWindow, {
        title: 'Export Clippy data',
        defaultPath: path.join(paths.USER_DATA, filename || 'clippy-export.json'),
        filters: [{ name: 'JSON', extensions: ['json'] }],
    })
    if (result.canceled || !result.filePath) return { canceled: true }
    fs.writeFileSync(result.filePath, content, 'utf8')
    return { canceled: false, path: result.filePath }
})
ipcMain.handle('open-screenshot', async (_event, value) => {
    const filename = path.basename(String(value || '').replace(/^\/screenshots\//, ''))
    const target = path.join(paths.DATA_DIR, 'screenshots', filename)
    if (!filename || !fs.existsSync(target)) return false
    return (await shell.openPath(target)) === ''
})
ipcMain.handle('open-external', (_event, url) => {
    const value = String(url || '')
    let parsed
    try { parsed = new URL(value) } catch (_) { return false }
    const expectedPath = `/${paths.RELEASE_REPOSITORY}`
    if (parsed.protocol !== 'https:' || parsed.hostname.toLowerCase() !== 'github.com' ||
        !(parsed.pathname === expectedPath || parsed.pathname.startsWith(`${expectedPath}/`))) return false
    return shell.openExternal(value)
})

ipcMain.handle('get-api-base', async () => api.apiUrl())
ipcMain.handle('is-api-ready', () => Boolean(state.apiReady))
ipcMain.handle('wait-for-api-ready', () => {
    if (state.apiReady) return true
    return new Promise((resolve) => {
        state.apiReadyWaiters.push(resolve)
    })
})
ipcMain.handle('get-update-check', () => updates.readDesktopSettings().updateCheckEnabled)
ipcMain.handle('get-app-version', () => app.getVersion())
ipcMain.handle('set-update-check', (_event, enabled) => updates.setUpdateCheckEnabled(enabled))

function pidWorkingSetBytes(pid) {
    if (!pid) return 0
    try {
        if (process.platform === 'win32') {
            const out = execFileSync(
                'tasklist',
                ['/FI', `PID eq ${pid}`, '/FO', 'CSV', '/NH'],
                { encoding: 'utf8', windowsHide: true, timeout: 1500 },
            ).trim()
            const fields = out.match(/"(?:[^"]|"")*"/g)
            if (!fields || fields.length < 5) return 0
            const kb = parseInt(fields[4].replace(/[^\d]/g, ''), 10)
            return Number.isFinite(kb) ? kb * 1024 : 0
        }
        const out = execFileSync('ps', ['-o', 'rss=', '-p', String(pid)], {
            encoding: 'utf8',
            timeout: 1500,
        }).trim()
        const kb = parseInt(out, 10)
        return Number.isFinite(kb) ? kb * 1024 : 0
    } catch (_) {
        return 0
    }
}

function formatMemoryLabel(bytes) {
    if (!bytes || bytes < 0) return 'Unavailable'
    const mb = bytes / (1024 * 1024)
    if (mb >= 100) return `${Math.round(mb)} MB`
    return `${mb.toFixed(1)} MB`
}

ipcMain.handle('get-about-info', () => {
    // Electron UI processes (main + renderers) plus local Python API/capture kids.
    // Ollama model RAM is separate and intentionally excluded.
    let bytes = 0
    try {
        for (const metric of app.getAppMetrics()) {
            bytes += (metric.memory?.workingSetSize || 0) * 1024
        }
    } catch (_) { /* ignore */ }
    for (const proc of [state.apiProcess, state.captureProcess]) {
        bytes += pidWorkingSetBytes(proc && proc.pid)
    }
    const config = llmConfig.publicLLMConfig()
    const model = String(config.chat_model || '').trim()
    return {
        version: app.getVersion(),
        memoryBytes: bytes,
        memoryLabel: formatMemoryLabel(bytes),
        chatModel: model ? `${model} (local)` : 'Local',
    }
})

const appIcons = createAppIcons({ app })
ipcMain.handle('get-app-icons', async (_event, processNames = []) => {
    return appIcons.getIconsForProcesses(Array.isArray(processNames) ? processNames : [])
})

mcp.registerMcpIpc(ipcMain, clipboard)

ipcMain.handle('get-hardware-check', async () => hardware.getHardwareCheck())
ipcMain.handle('get-llm-config', () => llmConfig.publicLLMConfig())
ipcMain.handle('save-llm-config', (_event, values = {}) => llmConfig.saveLLMConfig(values))
ipcMain.handle('open-provider-auth', (_event, provider) => llmConfig.openProviderAuth(String(provider || '')))

setup.registerSetupIpc(ipcMain, hardware.getHardwareCheck)

app.whenReady().then(async () => {
    // Windows groups the taskbar entry, tray icon, and notifications by this id.
    if (process.platform === 'win32') app.setAppUserModelId('com.clippyvision.app')

    // Register the global shortcut once Electron owns the application session.
    globalShortcut.register('CommandOrControl+Shift+Space', () => api.toggleCapture())

    for (const d of [paths.DATA_DIR, path.join(paths.DATA_DIR, 'screenshots'), path.join(paths.USER_DATA, 'logs')]) {
        if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true })
    }

    await api.clearStaleApiProcess()
    await api.ensureApiPort()
    console.log(`[api] port=${state.apiPort}`)
    console.log(`[paths] packaged=${paths.IS_PACKAGED}`)
    console.log(`[paths] ROOT=${paths.ROOT}`)
    console.log(`[paths] USER_DATA=${paths.USER_DATA}`)
    console.log(`[paths] DATA_DIR=${paths.DATA_DIR}`)

    setup.syncSetupFlagVersion()
    const setupDone = fs.existsSync(paths.SETUP_FLAG)

    if (setupDone) {

        windows.createTray()
        await api.ensureApiPort()
        windows.createMainWindow()
        await windows.waitForMainWindowLoad()
        windows.sendLoadingStatus(
            null,
            'Checking dependencies…'
        )

        console.log('[preflight] Checking all dependencies...')
        const check = await setup.runPreflightChecks()

        if (!check.ok) {
            console.error(`[preflight] Failed at step "${check.step}": ${check.reason}`)
            setup.redirectToSetup(check.step, check)
            return
        }

        console.log('[preflight] All checks passed — starting API')
        windows.sendLoadingStatus(null, 'Starting AI server and loading models…')
        await api.startServer()
        api.pollUntilAlive(api.apiUrl('/health'), 1000, 90)
            .then(async () => {
                console.log('[app] API server healthy — warming text model...')
                windows.sendLoadingStatus(null, 'Loading text model…')
                try {
                    const warm = await api.httpPost(api.apiUrl('/residency/startup'), {}, 120000)
                    const warmError = String((warm && warm.error) || '')
                    const classified = setup.classifyChatError(warmError)
                    if (classified) {
                        console.error('[app] residency warm failed:', warmError)
                        setup.redirectToSetup(classified.step, classified)
                        return
                    }
                    console.log('[app] residency startup warm done')
                } catch (e) {
                    const classified = setup.classifyChatError(e.message)
                    if (classified) {
                        console.error('[app] residency warm failed:', e.message)
                        setup.redirectToSetup(classified.step, classified)
                        return
                    }
                    console.log('[app] residency warm skipped:', e.message)
                }
                // Always flip the ready flag so renderer waiters unblock even if
                // the BrowserWindow was hidden/recreated during warmup.
                state.apiReady = true
                for (const resolve of state.apiReadyWaiters.splice(0)) resolve(true)
                if (state.mainWindow && !state.mainWindow.isDestroyed()) {
                    state.mainWindow.webContents.send('api-ready')
                }
            })
            .catch(() => {
                console.error('[app] API server failed to start after preflight — back to setup')
                setup.redirectToSetup('packages')
            })
    } else {
        state.setupStartFrom = 'python'
        windows.createSetupWindow()
    }

    app.on('activate', () => {
        if (state.mainWindow) windows.showMainWindow()
        else if (state.setupWindow) {
            state.setupWindow.show()
            state.setupWindow.focus()
        }
    })
})

app.on('window-all-closed', () => {

})

app.on('before-quit', () => {
    state.isQuitting = true
    globalShortcut.unregister('CommandOrControl+Shift+Space')
    api.stopCapture()
    if (state.apiProcess) {
        if (process.platform === 'win32') {
            api.spawnHidden('taskkill', ['/pid', String(state.apiProcess.pid), '/T', '/F'])
        } else {
            state.apiProcess.kill('SIGTERM')
        }
        state.apiProcess = null
    }
    // Only a server Clippy started is stopped here. A server owned by the
    // Ollama tray app must survive, or it will relaunch and fight for the port.
    if (state.ollamaProcess) {
        try { state.ollamaProcess.kill('SIGTERM') } catch (_) { }
        state.ollamaProcess = null
    }
    api.clearApiState()
    // Windows keeps painting a tray icon whose owner has exited until the user
    // hovers over it, so release it explicitly on the way out.
    if (state.tray && !state.tray.isDestroyed()) {
        state.tray.destroy()
        state.tray = null
    }
})
