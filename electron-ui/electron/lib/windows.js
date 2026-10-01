const path = require('path')
const { BrowserWindow, Tray, Menu, screen, nativeImage, Notification } = require('electron')

function createWindows({ paths, state, api, updates, shell, app }) {
    const { ELECTRON_DIR, ICON_ACTIVE, ICON_INACTIVE } = paths
    const { isCapturing, captureStatus, toggleCapture, pauseCapture, setCaptureCallbacks } = api
    const { checkForLatestRelease } = updates

    function getTrayIcon(active) {
        const file = active ? ICON_ACTIVE : ICON_INACTIVE
        return nativeImage.createFromPath(file).resize({ width: 16, height: 16 })
    }

    function updateTrayIcon() {
        if (!state.tray) return
        state.tray.setImage(getTrayIcon(isCapturing()))
        const status = captureStatus()
        const tip = status.pending === 'starting'
            ? 'Clippy Vision — Starting capture'
            : status.pending === 'stopping'
                ? 'Clippy Vision — Stopping capture'
                : status.active
                    ? 'Clippy Vision — Capturing'
                    : (status.pausedUntil ? 'Clippy Vision — Paused' : 'Clippy Vision — Idle')
        state.tray.setToolTip(tip)
    }

    function broadcastCaptureStatus(reason, notify) {
        const status = captureStatus()
        if (state.mainWindow && !state.mainWindow.isDestroyed()) {
            state.mainWindow.webContents.send('capture-status-changed', status)
        }
        if (!notify || !Notification.isSupported()) return
        let body = status.active ? 'Screen capture started' : 'Screen capture stopped'
        if (reason === 'failed') body = 'Screen capture did not start'
        else if (!status.active && status.pausedUntil) {
            const when = new Date(status.pausedUntil).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
            body = `Capture paused until ${when}`
        } else if (reason === 'pause') {
            body = 'Capture paused'
        }
        new Notification({ title: 'Clippy Vision', body, silent: false }).show()
    }

    function rebuildTrayMenu() {
        // Rebuild after every capture transition so the menu label mirrors state.
        if (!state.tray) return
        const status = captureStatus()
        const paused = status.pausedUntil > Date.now()
        const pending = status.pending === 'starting'
            ? 'Starting Capture'
            : status.pending === 'stopping'
                ? 'Stopping Capture'
                : ''
        state.tray.setContextMenu(Menu.buildFromTemplate([
            {
                label: pending || (isCapturing() ? 'Stop Capture' : 'Start Capture'),
                enabled: !pending,
                click: toggleCapture,
            },
            { label: 'Pause for 15 minutes', enabled: isCapturing() && !paused, click: () => pauseCapture(15) },
            { label: 'Pause for 30 minutes', enabled: isCapturing() && !paused, click: () => pauseCapture(30) },
            { label: 'Pause for 1 hour', enabled: isCapturing() && !paused, click: () => pauseCapture(60) },
            { type: 'separator' },
            { label: 'Open Clippy Vision', click: showMainWindow },
            { type: 'separator' },
            { label: 'Quit', click: () => { state.isQuitting = true; api.stopCapture(); app.quit() } },
        ]))
    }

    setCaptureCallbacks({
        onCapturePending() {
            updateTrayIcon()
            rebuildTrayMenu()
            broadcastCaptureStatus(null, false)
        },
        onCaptureStarted() {
            updateTrayIcon()
            rebuildTrayMenu()
            broadcastCaptureStatus(null, true)
        },
        onCaptureStopped(reason) {
            updateTrayIcon()
            rebuildTrayMenu()
            broadcastCaptureStatus(reason, true)
        },
    })

    function createSetupWindow() {
        // Setup is a separate, isolated window so install logs and the hardware
        // gate never compete with the main chat renderer.
        if (state.setupWindow && !state.setupWindow.isDestroyed()) {
            state.setupWindow.show()
            state.setupWindow.focus()
            return
        }
        state.setupInstallStarted = false
        const work = screen.getPrimaryDisplay().workAreaSize
        const minWidth = Math.min(480, work.width)
        const minHeight = Math.min(520, work.height)
        const width = Math.min(720, Math.max(minWidth, work.width - 48))
        const height = Math.min(840, Math.max(minHeight, work.height - 48))
        state.setupWindow = new BrowserWindow({
            width,
            height,
            minWidth,
            minHeight,
            resizable: true,
            maximizable: true,
            icon: ICON_ACTIVE,
            webPreferences: {
                preload: path.join(ELECTRON_DIR, 'setup-preload.js'),
                contextIsolation: true,
                nodeIntegration: false,
            },
        })

        state.setupWindow.loadFile(path.join(ELECTRON_DIR, '../src/setup.html'))
        state.setupWindow.setMenu(null)

        state.setupWindow.on('closed', () => { state.setupWindow = null })

    }

    function createMainWindow() {
        // Closing the chat hides it to the tray; the process must stay alive for
        // capture and background processing until the user explicitly quits.
        const work = screen.getPrimaryDisplay().workAreaSize
        // Keep room for brand + New Chat / Timeline / Settings / Capture.
        const minWidth = Math.min(720, work.width)
        const minHeight = Math.min(520, work.height)
        state.mainWindow = new BrowserWindow({
            minWidth,
            minHeight,
            width: Math.min(900, Math.max(minWidth, Math.min(work.width, 900))),
            height: Math.min(700, Math.max(minHeight, Math.min(work.height, 700))),
            icon: ICON_ACTIVE,
            webPreferences: {
                preload: path.join(ELECTRON_DIR, 'preload.js'),
                contextIsolation: true,
                nodeIntegration: false,
            },
        })

        state.mainWindow.loadFile(path.join(ELECTRON_DIR, '../src/index.html'))
        state.mainWindow.setMenu(null)
        state.mainWindow.webContents.setWindowOpenHandler(({ url }) => {
            try {
                const parsed = new URL(url)
                if (parsed.protocol === 'https:' || parsed.protocol === 'http:') {
                    shell.openExternal(url)
                }
            } catch (_) { /* ignore invalid URLs */ }
            return { action: 'deny' }
        })
        state.mainWindow.webContents.on('will-navigate', (event, url) => {
            if (url !== state.mainWindow.webContents.getURL()) event.preventDefault()
        })
        state.mainWindow.webContents.once('did-finish-load', () => checkForLatestRelease())


        state.mainWindow.on('close', (event) => {
            if (!state.isQuitting && !state.allowMainClose) {
                event.preventDefault()
                state.mainWindow.hide()
            }
        })

        state.mainWindow.on('closed', () => { state.mainWindow = null })
    }

    function showMainWindow() {
        if (!state.mainWindow) {
            createMainWindow()
            return
        }
        state.mainWindow.show()
        state.mainWindow.focus()
    }

    function createTray() {
        // The tray is the persistent control surface while the main window is
        // hidden, which keeps screen capture available without a large window.
        // Both the normal launch path and the setup "Launch" handler reach here, so
        // reuse the existing tray instead of leaving a second icon behind.
        if (state.tray && !state.tray.isDestroyed()) return
        state.tray = new Tray(getTrayIcon(false))
        state.tray.setToolTip('Clippy Vision — Idle')
        rebuildTrayMenu()
        state.tray.on('click', toggleCapture)
        state.tray.on('double-click', showMainWindow)
    }

    function sendLoadingStatus(title, sub) {
        if (state.mainWindow && !state.mainWindow.isDestroyed()) {
            const payload = {}
            if (title) payload.title = title
            if (sub) payload.sub = sub
            state.mainWindow.webContents.send('loading-status', payload)
        }
    }

    function waitForMainWindowLoad() {
        return new Promise((resolve) => {
            const win = state.mainWindow
            if (!win || win.isDestroyed()) return resolve()
            const contents = win.webContents
            // loadFile() has not started navigation yet when isLoading() is
            // still false. Treating that as "already loaded" drops every
            // loading-status message and the splash never changes.
            const url = contents.getURL() || ''
            if (!contents.isLoading() && url.startsWith('file:')) return resolve()
            contents.once('did-finish-load', () => resolve())
        })
    }

    function closeMainAndTray() {
        state.allowMainClose = true
        if (state.mainWindow && !state.mainWindow.isDestroyed()) state.mainWindow.close()
        state.allowMainClose = false
        state.mainWindow = null
        if (state.tray) { state.tray.destroy(); state.tray = null }
    }

    function launchFromSetup() {
        if (state.setupWindow && !state.setupWindow.isDestroyed()) state.setupWindow.close()
        createTray()
        createMainWindow()
        state.apiReady = true
        for (const resolve of state.apiReadyWaiters.splice(0)) resolve(true)
        if (state.mainWindow && !state.mainWindow.isDestroyed()) {
            state.mainWindow.webContents.send('api-ready')
        }
    }

    const windowBridge = {
        createSetupWindow,
        closeMainAndTray,
        launchFromSetup,
    }

    return {
        createSetupWindow,
        createMainWindow,
        showMainWindow,
        createTray,
        sendLoadingStatus,
        waitForMainWindowLoad,
        closeMainAndTray,
        launchFromSetup,
        windowBridge,
        getMainWindow: () => state.mainWindow,
    }
}

module.exports = { createWindows }
