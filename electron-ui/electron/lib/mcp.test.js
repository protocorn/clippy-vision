/**
 * VS Code / Claude Desktop MCP config paths must be resolved per platform.
 * Run with: node --test electron/lib/mcp.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const { createMcp, mcpClientDefinitions, isAppImageMountPath, looksLikeAppImageRuntime, entryHasStaleAppImageMount, hashMcpRuntimeContents } = require('./mcp')

function seedClippyRoot(root) {
    fs.mkdirSync(path.join(root, 'scripts'), { recursive: true })
    fs.writeFileSync(path.join(root, 'mcp_server.py'), '# fake mcp server\n')
    fs.writeFileSync(path.join(root, 'scripts', 'clippy-mcp'), '#!/bin/sh\n')
    fs.writeFileSync(path.join(root, 'scripts', 'clippy-mcp.cmd'), '@echo off\n')
}

function seedPython(pythonPath) {
    fs.mkdirSync(path.dirname(pythonPath), { recursive: true })
    fs.writeFileSync(pythonPath, '')
}

function createAppImageMcp({ home, mountRoot, userData, python, appImage, appDir, version = '2.1.1', llmConfig } = {}) {
    return createMcp({
        paths: {
            ROOT: mountRoot,
            DATA_DIR: path.join(userData, 'data'),
            USER_DATA: userData,
            PYTHON_COMMAND: python,
        },
        runCommand: async () => ({ code: 1, stdout: '' }),
        shell: { openExternal: async () => {} },
        version,
        llmConfig,
        runtime: {
            platform: 'linux',
            home,
            env: { APPIMAGE: appImage, APPDIR: appDir },
        },
    })
}

function mcpRuntimeDirs(userData) {
    if (!fs.existsSync(userData)) return []
    return fs.readdirSync(userData).filter((name) => {
        if (!name.startsWith('mcp-runtime-')) return false
        if (name.includes('.tmp-')) return false
        return fs.statSync(path.join(userData, name)).isDirectory()
    })
}

function vscodePath(runtime) {
    return mcpClientDefinitions(runtime).vscode.configPath
}

function claudePath(runtime) {
    return mcpClientDefinitions(runtime)['claude-desktop'].configPath
}

test('VS Code mcp.json path on Windows uses APPDATA', () => {
    const runtime = {
        platform: 'win32',
        home: 'C:\\Users\\ada',
        env: { APPDATA: 'C:\\Users\\ada\\AppData\\Roaming' },
    }
    assert.equal(
        vscodePath(runtime),
        path.join('C:\\Users\\ada\\AppData\\Roaming', 'Code', 'User', 'mcp.json'),
    )
})

test('VS Code mcp.json path on Windows falls back to ~/AppData/Roaming', () => {
    const runtime = {
        platform: 'win32',
        home: 'C:\\Users\\ada',
        env: {},
    }
    assert.equal(
        vscodePath(runtime),
        path.join('C:\\Users\\ada', 'AppData', 'Roaming', 'Code', 'User', 'mcp.json'),
    )
})

test('VS Code mcp.json path on macOS uses Application Support and ignores APPDATA', () => {
    const runtime = {
        platform: 'darwin',
        home: '/Users/ada',
        env: { APPDATA: 'C:\\WindowsStyle\\Roaming', XDG_CONFIG_HOME: '/should-not-use' },
    }
    assert.equal(
        vscodePath(runtime),
        path.join('/Users/ada', 'Library', 'Application Support', 'Code', 'User', 'mcp.json'),
    )
})

test('VS Code mcp.json path on Linux uses ~/.config and ignores APPDATA', () => {
    const runtime = {
        platform: 'linux',
        home: '/home/ada',
        env: { APPDATA: 'C:\\WindowsStyle\\Roaming' },
    }
    assert.equal(
        vscodePath(runtime),
        path.join('/home/ada', '.config', 'Code', 'User', 'mcp.json'),
    )
})

test('VS Code mcp.json path on Linux prefers XDG_CONFIG_HOME', () => {
    const runtime = {
        platform: 'linux',
        home: '/home/ada',
        env: { XDG_CONFIG_HOME: '/home/ada/.xdg-config', APPDATA: 'C:\\WindowsStyle\\Roaming' },
    }
    assert.equal(
        vscodePath(runtime),
        path.join('/home/ada/.xdg-config', 'Code', 'User', 'mcp.json'),
    )
})

test('Claude Desktop config path on Windows uses APPDATA', () => {
    const runtime = {
        platform: 'win32',
        home: 'C:\\Users\\ada',
        env: { APPDATA: 'C:\\Users\\ada\\AppData\\Roaming' },
    }
    assert.equal(
        claudePath(runtime),
        path.join('C:\\Users\\ada\\AppData\\Roaming', 'Claude', 'claude_desktop_config.json'),
    )
})

test('Claude Desktop config path on macOS uses Application Support', () => {
    const runtime = {
        platform: 'darwin',
        home: '/Users/ada',
        env: {},
    }
    assert.equal(
        claudePath(runtime),
        path.join('/Users/ada', 'Library', 'Application Support', 'Claude', 'claude_desktop_config.json'),
    )
})

test('Claude Desktop config path on Linux uses ~/.config', () => {
    const runtime = {
        platform: 'linux',
        home: '/home/ada',
        env: {},
    }
    assert.equal(
        claudePath(runtime),
        path.join('/home/ada', '.config', 'Claude', 'claude_desktop_config.json'),
    )
})

test('connected-status check reads the same VS Code path used for writes', async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-mcp-'))
    try {
        const runtime = { platform: 'linux', home, env: {} }
        const configPath = vscodePath(runtime)
        assert.equal(configPath, path.join(home, '.config', 'Code', 'User', 'mcp.json'))

        const mcp = createMcp({
            paths: { ROOT: home, DATA_DIR: path.join(home, 'data'), PYTHON_COMMAND: process.execPath },
            runCommand: async () => ({ code: 1, stdout: '' }),
            shell: { openExternal: async () => {} },
            runtime,
        })

        const before = await mcp.listMcpClientStatus()
        const vscodeBefore = before.clients.find((client) => client.id === 'vscode')
        assert.equal(vscodeBefore.configPath, configPath)
        assert.equal(vscodeBefore.connected, false)

        fs.mkdirSync(path.dirname(configPath), { recursive: true })
        fs.writeFileSync(configPath, JSON.stringify({
            servers: {
                'clippy-vision': {
                    command: process.execPath,
                    args: [path.join(home, 'mcp_server.py')],
                },
            },
        }))

        const wrongWindowsPath = path.join(home, 'AppData', 'Roaming', 'Code', 'User', 'mcp.json')
        assert.notEqual(configPath, wrongWindowsPath)

        const after = await mcp.listMcpClientStatus()
        const vscodeAfter = after.clients.find((client) => client.id === 'vscode')
        assert.equal(vscodeAfter.configPath, configPath)
        assert.equal(vscodeAfter.connected, true)
    } finally {
        fs.rmSync(home, { recursive: true, force: true })
    }
})

test('isAppImageMountPath detects FUSE mounts and ignores Windows install paths', () => {
    assert.equal(isAppImageMountPath('/tmp/.mount_ClippyXYZ/usr/lib/clippy-vision/resources/clippy'), true)
    assert.equal(isAppImageMountPath('/tmp/.mount_ClippyXYZ/usr/bin/clippy-vision'), true)
    assert.equal(isAppImageMountPath('C:\\Users\\ada\\AppData\\Local\\Programs\\Clippy Vision\\resources\\clippy'), false)
    assert.equal(isAppImageMountPath('/home/ada/src/clippy-vision'), false)
    assert.equal(isAppImageMountPath('/tmp/clippy-dev'), false)
})

test('looksLikeAppImageRuntime is Linux-only', () => {
    const mount = '/tmp/.mount_ClippyABC/usr/lib/clippy-vision/resources/clippy'
    assert.equal(looksLikeAppImageRuntime({ platform: 'linux', env: { APPIMAGE: '/home/ada/Clippy.AppImage' }, root: mount }), true)
    assert.equal(looksLikeAppImageRuntime({ platform: 'linux', env: {}, root: mount }), true)
    assert.equal(looksLikeAppImageRuntime({ platform: 'win32', env: { APPIMAGE: '/home/ada/Clippy.AppImage' }, root: mount }), false)
    assert.equal(looksLikeAppImageRuntime({ platform: 'darwin', env: { APPIMAGE: '/home/ada/Clippy.AppImage' }, root: mount }), false)
    assert.equal(looksLikeAppImageRuntime({
        platform: 'win32',
        env: {},
        root: 'C:\\Users\\ada\\AppData\\Local\\Programs\\Clippy Vision\\resources\\clippy',
    }), false)
    assert.equal(looksLikeAppImageRuntime({ platform: 'linux', env: {}, root: '/home/ada/src/clippy-vision' }), false)
})

test('AppImage launch config copies python runtime into userData instead of /tmp/.mount_*', async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-mcp-appimage-'))
    try {
        const mountRoot = path.join(home, 'tmp', '.mount_ClippyABC123', 'usr', 'lib', 'clippy-vision', 'resources', 'clippy')
        seedClippyRoot(mountRoot)
        const userData = path.join(home, 'user-data')
        const python = path.join(userData, 'python-venv', 'bin', 'python')
        seedPython(python)
        const appImage = path.join(home, 'ClippyVision-Linux-x86_64-2.1.1.AppImage')
        fs.writeFileSync(appImage, '')

        fs.writeFileSync(path.join(mountRoot, 'api_server.py'), '# not needed by MCP clients\n')

        const mcp = createAppImageMcp({
            home,
            mountRoot,
            userData,
            python,
            appImage,
            appDir: path.join(home, 'tmp', '.mount_ClippyABC123'),
        })

        const launch = await mcp.getMcpLaunchConfig()
        assert.equal(launch.ready, true, launch.issues && launch.issues.join(' '))
        assert.equal(launch.runtimeCopied, true)
        assert.equal(launch.command, python)
        assert.ok(path.basename(launch.env.PYTHONPATH).startsWith('mcp-runtime-'))
        assert.equal(launch.args[0], path.join(launch.env.PYTHONPATH, 'mcp_server.py'))
        assert.equal(launch.env.CLIPPY_DATA_DIR, path.join(userData, 'data'))
        assert.equal(
            launch.env.CLIPPY_EMBEDDING_MODEL_DIR,
            path.join(userData, 'models', 'embeddings', 'all-MiniLM-L6-v2'),
        )
        assert.equal(launch.env.CLIPPY_OLLAMA_URL, 'http://127.0.0.1:11434')
        assert.ok(!String(launch.args[0]).includes('.mount_'))
        assert.ok(!String(launch.env.PYTHONPATH).includes('.mount_'))
        assert.ok(!String(launch.launcher).includes('.mount_'))
        assert.equal(launch.launcher, path.join(userData, 'clippy-mcp'))
        assert.equal(fs.existsSync(launch.args[0]), true)
        assert.equal(fs.existsSync(launch.launcher), true)
        assert.equal(fs.existsSync(path.join(launch.env.PYTHONPATH, 'api_server.py')), false)
        const launcherText = fs.readFileSync(launch.launcher, 'utf8')
        assert.match(launcherText, /mcp-runtime/)
        assert.match(launcherText, /CLIPPY_EMBEDDING_MODEL_DIR=/)
        assert.ok(launcherText.includes(launch.env.CLIPPY_EMBEDDING_MODEL_DIR))
        assert.match(launcherText, /CLIPPY_OLLAMA_URL=/)
        assert.ok(launcherText.includes(launch.env.CLIPPY_OLLAMA_URL))
        assert.match(launcherText, /^exec /m)
        const execLine = launcherText.split('\n').find((line) => line.startsWith('exec '))
        assert.ok(execLine)
        assert.ok(!execLine.includes('.mount_'))
        assert.ok(!launcherText.includes(mountRoot))
        const stamp = fs.readFileSync(path.join(userData, 'mcp-runtime.stamp'), 'utf8')
        assert.ok(!stamp.includes('.mount_'))
        assert.ok(!stamp.includes(mountRoot))
        assert.match(stamp, /^v1\n2\.1\.1\n[a-f0-9]{64}\n$/)
    } finally {
        fs.rmSync(home, { recursive: true, force: true })
    }
})

test('Windows packaged ROOT is left unchanged (not copied into userData)', async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-mcp-win-'))
    try {
        const root = path.join(home, 'AppData', 'Local', 'Programs', 'Clippy Vision', 'resources', 'clippy')
        seedClippyRoot(root)
        const userData = path.join(home, 'AppData', 'Roaming', 'Clippy Vision')
        const python = path.join(userData, 'python-venv', 'Scripts', 'python.exe')
        seedPython(python)

        const mcp = createMcp({
            paths: {
                ROOT: root,
                DATA_DIR: path.join(userData, 'data'),
                USER_DATA: userData,
                PYTHON_COMMAND: python,
            },
            runCommand: async () => ({ code: 1, stdout: '' }),
            shell: { openExternal: async () => {} },
            runtime: {
                platform: 'win32',
                home,
                env: { APPDATA: path.join(home, 'AppData', 'Roaming') },
            },
        })

        const launch = await mcp.getMcpLaunchConfig()
        assert.equal(launch.args[0], path.join(root, 'mcp_server.py'))
        assert.equal(launch.env.PYTHONPATH, root)
        assert.equal(
            launch.env.CLIPPY_EMBEDDING_MODEL_DIR,
            path.join(userData, 'models', 'embeddings', 'all-MiniLM-L6-v2'),
        )
        assert.equal(launch.env.CLIPPY_OLLAMA_URL, 'http://127.0.0.1:11434')
        assert.equal(launch.launcher, path.join(root, 'scripts', 'clippy-mcp.cmd'))
        assert.equal(fs.existsSync(path.join(userData, 'mcp-runtime')), false)
    } finally {
        fs.rmSync(home, { recursive: true, force: true })
    }
})

test('non-AppImage Linux ROOT is left unchanged', async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-mcp-dev-'))
    try {
        seedClippyRoot(home)
        const python = path.join(home, 'venv', 'bin', 'python')
        seedPython(python)

        const mcp = createMcp({
            paths: { ROOT: home, DATA_DIR: path.join(home, 'data'), USER_DATA: home, PYTHON_COMMAND: python },
            runCommand: async () => ({ code: 1, stdout: '' }),
            shell: { openExternal: async () => {} },
            runtime: { platform: 'linux', home, env: {} },
        })

        const launch = await mcp.getMcpLaunchConfig()
        assert.equal(launch.args[0], path.join(home, 'mcp_server.py'))
        assert.equal(launch.env.PYTHONPATH, home)
        assert.equal(
            launch.env.CLIPPY_EMBEDDING_MODEL_DIR,
            path.join(home, 'models', 'embeddings', 'all-MiniLM-L6-v2'),
        )
        assert.equal(launch.env.CLIPPY_OLLAMA_URL, 'http://127.0.0.1:11434')
        assert.equal(fs.existsSync(path.join(home, 'mcp-runtime')), false)
    } finally {
        fs.rmSync(home, { recursive: true, force: true })
    }
})

test('startup refresh rewrites stale /tmp/.mount_* MCP configs and leaves others alone', async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-mcp-refresh-'))
    try {
        const mountRoot = path.join(home, 'tmp', '.mount_ClippyOLD', 'usr', 'lib', 'clippy-vision', 'resources', 'clippy')
        seedClippyRoot(mountRoot)
        const userData = path.join(home, 'user-data')
        const python = path.join(userData, 'python-venv', 'bin', 'python')
        seedPython(python)
        const appImage = path.join(home, 'ClippyVision.AppImage')
        fs.writeFileSync(appImage, '')

        const runtime = {
            platform: 'linux',
            home,
            env: { APPIMAGE: appImage, APPDIR: path.join(home, 'tmp', '.mount_ClippyOLD') },
        }
        const staleServer = path.join(mountRoot, 'mcp_server.py')
        const cursorPath = mcpClientDefinitions(runtime).cursor.configPath
        const vscodePath = mcpClientDefinitions(runtime).vscode.configPath
        fs.mkdirSync(path.dirname(cursorPath), { recursive: true })
        fs.mkdirSync(path.dirname(vscodePath), { recursive: true })
        fs.writeFileSync(cursorPath, JSON.stringify({
            mcpServers: {
                'clippy-vision': {
                    command: python,
                    args: [staleServer],
                    env: { PYTHONPATH: mountRoot, CLIPPY_DATA_DIR: path.join(userData, 'data') },
                },
                'other-app': { command: 'npx', args: ['something'] },
            },
        }))
        fs.writeFileSync(vscodePath, JSON.stringify({
            servers: {
                'unrelated': { command: 'node', args: ['server.js'] },
            },
        }))

        const mcp = createMcp({
            paths: {
                ROOT: mountRoot,
                DATA_DIR: path.join(userData, 'data'),
                USER_DATA: userData,
                PYTHON_COMMAND: python,
            },
            runCommand: async () => ({ code: 1, stdout: '' }),
            shell: { openExternal: async () => {} },
            runtime,
        })

        const result = await mcp.refreshStaleMcpClientConfigs()
        assert.deepEqual(result.refreshed.map((row) => row.clientId), ['cursor'])

        const launch = await mcp.getMcpLaunchConfig()
        const cursorDoc = JSON.parse(fs.readFileSync(cursorPath, 'utf8'))
        assert.equal(cursorDoc.mcpServers['clippy-vision'].args[0], launch.args[0])
        assert.equal(cursorDoc.mcpServers['clippy-vision'].env.PYTHONPATH, launch.env.PYTHONPATH)
        assert.equal(cursorDoc.mcpServers['clippy-vision'].env.CLIPPY_EMBEDDING_MODEL_DIR, launch.env.CLIPPY_EMBEDDING_MODEL_DIR)
        assert.ok(path.basename(launch.env.PYTHONPATH).startsWith('mcp-runtime-'))
        assert.equal(cursorDoc.mcpServers['other-app'].command, 'npx')

        const vscodeDoc = JSON.parse(fs.readFileSync(vscodePath, 'utf8'))
        assert.deepEqual(vscodeDoc.servers, { unrelated: { command: 'node', args: ['server.js'] } })
        assert.equal(entryHasStaleAppImageMount(cursorDoc.mcpServers['clippy-vision']), false)
    } finally {
        fs.rmSync(home, { recursive: true, force: true })
    }
})

test('startup refresh writes CLIPPY_EMBEDDING_MODEL_DIR onto current runtime configs', async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-mcp-embed-refresh-'))
    try {
        const mountRoot = path.join(home, 'tmp', '.mount_ClippyCUR', 'usr', 'lib', 'clippy-vision', 'resources', 'clippy')
        seedClippyRoot(mountRoot)
        const userData = path.join(home, 'user-data')
        const python = path.join(userData, 'python-venv', 'bin', 'python')
        seedPython(python)
        const appImage = path.join(home, 'ClippyVision.AppImage')
        fs.writeFileSync(appImage, '')

        const mcp = createAppImageMcp({
            home,
            mountRoot,
            userData,
            python,
            appImage,
            appDir: path.join(home, 'tmp', '.mount_ClippyCUR'),
        })
        const launch = await mcp.getMcpLaunchConfig()
        const runtime = { platform: 'linux', home, env: { APPIMAGE: appImage, APPDIR: path.join(home, 'tmp', '.mount_ClippyCUR') } }
        const cursorPath = mcpClientDefinitions(runtime).cursor.configPath
        fs.mkdirSync(path.dirname(cursorPath), { recursive: true })
        fs.writeFileSync(cursorPath, JSON.stringify({
            mcpServers: {
                'clippy-vision': {
                    command: python,
                    args: [launch.args[0]],
                    env: { PYTHONPATH: launch.env.PYTHONPATH, CLIPPY_DATA_DIR: launch.env.CLIPPY_DATA_DIR },
                },
            },
        }))

        const result = await mcp.refreshStaleMcpClientConfigs()
        assert.deepEqual(result.refreshed.map((row) => row.clientId), ['cursor'])
        const cursorDoc = JSON.parse(fs.readFileSync(cursorPath, 'utf8'))
        assert.equal(
            cursorDoc.mcpServers['clippy-vision'].env.CLIPPY_EMBEDDING_MODEL_DIR,
            path.join(userData, 'models', 'embeddings', 'all-MiniLM-L6-v2'),
        )
    } finally {
        fs.rmSync(home, { recursive: true, force: true })
    }
})

test('MCP launch env includes CLIPPY_OLLAMA_URL from Settings', async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-mcp-ollama-'))
    try {
        seedClippyRoot(home)
        const python = path.join(home, 'venv', 'bin', 'python')
        seedPython(python)
        const ollamaUrl = 'http://192.168.1.5:11434'
        const mcp = createMcp({
            paths: { ROOT: home, DATA_DIR: path.join(home, 'data'), USER_DATA: home, PYTHON_COMMAND: python },
            runCommand: async () => ({ code: 1, stdout: '' }),
            shell: { openExternal: async () => {} },
            llmConfig: { configuredOllamaBaseURL: () => ollamaUrl },
            runtime: { platform: 'linux', home, env: {} },
        })

        const launch = await mcp.getMcpLaunchConfig()
        assert.equal(launch.env.CLIPPY_OLLAMA_URL, ollamaUrl)

        const cursorPath = mcpClientDefinitions({ platform: 'linux', home, env: {} }).cursor.configPath
        await mcp.connectMcpClient('cursor')
        const cursorDoc = JSON.parse(fs.readFileSync(cursorPath, 'utf8'))
        assert.equal(cursorDoc.mcpServers['clippy-vision'].env.CLIPPY_OLLAMA_URL, ollamaUrl)
    } finally {
        fs.rmSync(home, { recursive: true, force: true })
    }
})

test('startup refresh writes CLIPPY_OLLAMA_URL onto current runtime configs', async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-mcp-ollama-refresh-'))
    try {
        const mountRoot = path.join(home, 'tmp', '.mount_ClippyCUR', 'usr', 'lib', 'clippy-vision', 'resources', 'clippy')
        seedClippyRoot(mountRoot)
        const userData = path.join(home, 'user-data')
        const python = path.join(userData, 'python-venv', 'bin', 'python')
        seedPython(python)
        const appImage = path.join(home, 'ClippyVision.AppImage')
        fs.writeFileSync(appImage, '')
        const ollamaUrl = 'http://10.0.0.8:11434'

        const mcp = createAppImageMcp({
            home,
            mountRoot,
            userData,
            python,
            appImage,
            appDir: path.join(home, 'tmp', '.mount_ClippyCUR'),
            llmConfig: { configuredOllamaBaseURL: () => ollamaUrl },
        })
        const launch = await mcp.getMcpLaunchConfig()
        const runtime = { platform: 'linux', home, env: { APPIMAGE: appImage, APPDIR: path.join(home, 'tmp', '.mount_ClippyCUR') } }
        const cursorPath = mcpClientDefinitions(runtime).cursor.configPath
        fs.mkdirSync(path.dirname(cursorPath), { recursive: true })
        fs.writeFileSync(cursorPath, JSON.stringify({
            mcpServers: {
                'clippy-vision': {
                    command: python,
                    args: [launch.args[0]],
                    env: {
                        PYTHONPATH: launch.env.PYTHONPATH,
                        CLIPPY_DATA_DIR: launch.env.CLIPPY_DATA_DIR,
                        CLIPPY_EMBEDDING_MODEL_DIR: launch.env.CLIPPY_EMBEDDING_MODEL_DIR,
                    },
                },
            },
        }))

        const result = await mcp.refreshStaleMcpClientConfigs()
        assert.deepEqual(result.refreshed.map((row) => row.clientId), ['cursor'])
        const cursorDoc = JSON.parse(fs.readFileSync(cursorPath, 'utf8'))
        assert.equal(cursorDoc.mcpServers['clippy-vision'].env.CLIPPY_OLLAMA_URL, ollamaUrl)
    } finally {
        fs.rmSync(home, { recursive: true, force: true })
    }
})

test('startup refresh updates CLIPPY_OLLAMA_URL when Settings address changes', async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-mcp-ollama-change-'))
    try {
        seedClippyRoot(home)
        const python = path.join(home, 'venv', 'bin', 'python')
        seedPython(python)
        let ollamaUrl = 'http://127.0.0.1:11434'
        const mcp = createMcp({
            paths: { ROOT: home, DATA_DIR: path.join(home, 'data'), USER_DATA: home, PYTHON_COMMAND: python },
            runCommand: async () => ({ code: 1, stdout: '' }),
            shell: { openExternal: async () => {} },
            llmConfig: { configuredOllamaBaseURL: () => ollamaUrl },
            runtime: { platform: 'linux', home, env: {} },
        })

        await mcp.connectMcpClient('cursor')
        const cursorPath = mcpClientDefinitions({ platform: 'linux', home, env: {} }).cursor.configPath
        const before = JSON.parse(fs.readFileSync(cursorPath, 'utf8'))
        assert.equal(before.mcpServers['clippy-vision'].env.CLIPPY_OLLAMA_URL, 'http://127.0.0.1:11434')

        ollamaUrl = 'http://192.168.1.9:11434'
        const result = await mcp.refreshStaleMcpClientConfigs()
        assert.deepEqual(result.refreshed.map((row) => row.clientId), ['cursor'])
        const after = JSON.parse(fs.readFileSync(cursorPath, 'utf8'))
        assert.equal(after.mcpServers['clippy-vision'].env.CLIPPY_OLLAMA_URL, ollamaUrl)
    } finally {
        fs.rmSync(home, { recursive: true, force: true })
    }
})

test('AppImage runtime is copied once across two launches with different mount paths', async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-mcp-stamp-'))
    try {
        const mountA = path.join(home, 'tmp', '.mount_ClippyAAA', 'usr', 'lib', 'clippy-vision', 'resources', 'clippy')
        const mountB = path.join(home, 'tmp', '.mount_ClippyBBB', 'usr', 'lib', 'clippy-vision', 'resources', 'clippy')
        seedClippyRoot(mountA)
        seedClippyRoot(mountB)
        const userData = path.join(home, 'user-data')
        const python = path.join(userData, 'python-venv', 'bin', 'python')
        seedPython(python)
        const appImage = path.join(home, 'ClippyVision.AppImage')
        fs.writeFileSync(appImage, '')

        assert.equal(hashMcpRuntimeContents(mountA), hashMcpRuntimeContents(mountB))

        const first = createAppImageMcp({
            home,
            mountRoot: mountA,
            userData,
            python,
            appImage,
            appDir: path.join(home, 'tmp', '.mount_ClippyAAA'),
            version: '2.1.1',
        })
        const firstLaunch = await first.getMcpLaunchConfig()
        assert.equal(firstLaunch.ready, true, firstLaunch.issues && firstLaunch.issues.join(' '))
        assert.equal(firstLaunch.runtimeCopied, true)
        assert.deepEqual(mcpRuntimeDirs(userData), [path.basename(firstLaunch.env.PYTHONPATH)])

        const sentinel = path.join(firstLaunch.env.PYTHONPATH, 'copied-once.marker')
        fs.writeFileSync(sentinel, 'keep\n')
        const copiedMtime = fs.statSync(firstLaunch.args[0]).mtimeMs
        const stampAfterFirst = fs.readFileSync(path.join(userData, 'mcp-runtime.stamp'), 'utf8')
        assert.ok(!stampAfterFirst.includes('.mount_'))
        assert.ok(!stampAfterFirst.includes(mountA))
        assert.ok(!stampAfterFirst.includes(mountB))

        const second = createAppImageMcp({
            home,
            mountRoot: mountB,
            userData,
            python,
            appImage,
            appDir: path.join(home, 'tmp', '.mount_ClippyBBB'),
            version: '2.1.1',
        })
        const secondLaunch = await second.getMcpLaunchConfig()
        assert.equal(secondLaunch.ready, true, secondLaunch.issues && secondLaunch.issues.join(' '))
        assert.equal(secondLaunch.runtimeCopied, false)
        assert.equal(secondLaunch.env.PYTHONPATH, firstLaunch.env.PYTHONPATH)
        assert.equal(secondLaunch.args[0], firstLaunch.args[0])
        assert.equal(fs.existsSync(sentinel), true)
        assert.equal(fs.readFileSync(sentinel, 'utf8'), 'keep\n')
        assert.equal(fs.statSync(secondLaunch.args[0]).mtimeMs, copiedMtime)
        assert.deepEqual(mcpRuntimeDirs(userData), [path.basename(firstLaunch.env.PYTHONPATH)])
        assert.equal(
            fs.readFileSync(path.join(userData, 'mcp-runtime.stamp'), 'utf8'),
            stampAfterFirst,
        )
    } finally {
        fs.rmSync(home, { recursive: true, force: true })
    }
})

test('AppImage runtime swap keeps the previous folder when contents change', async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-mcp-swap-'))
    try {
        const mountA = path.join(home, 'tmp', '.mount_ClippyOLD', 'usr', 'lib', 'clippy-vision', 'resources', 'clippy')
        const mountB = path.join(home, 'tmp', '.mount_ClippyNEW', 'usr', 'lib', 'clippy-vision', 'resources', 'clippy')
        seedClippyRoot(mountA)
        seedClippyRoot(mountB)
        fs.writeFileSync(path.join(mountB, 'mcp_server.py'), '# upgraded mcp server\n')
        const userData = path.join(home, 'user-data')
        const python = path.join(userData, 'python-venv', 'bin', 'python')
        seedPython(python)
        const appImage = path.join(home, 'ClippyVision.AppImage')
        fs.writeFileSync(appImage, '')

        const first = createAppImageMcp({
            home, mountRoot: mountA, userData, python, appImage,
            appDir: path.join(home, 'tmp', '.mount_ClippyOLD'),
        })
        const firstLaunch = await first.getMcpLaunchConfig()
        const oldRoot = firstLaunch.env.PYTHONPATH
        const sentinel = path.join(oldRoot, 'still-running.marker')
        fs.writeFileSync(sentinel, 'do-not-delete\n')

        const second = createAppImageMcp({
            home, mountRoot: mountB, userData, python, appImage,
            appDir: path.join(home, 'tmp', '.mount_ClippyNEW'),
        })
        const secondLaunch = await second.getMcpLaunchConfig()
        assert.equal(secondLaunch.runtimeCopied, true)
        assert.notEqual(secondLaunch.env.PYTHONPATH, oldRoot)
        assert.equal(fs.existsSync(sentinel), true)
        assert.equal(fs.readFileSync(path.join(oldRoot, 'mcp_server.py'), 'utf8'), '# fake mcp server\n')
        assert.equal(fs.readFileSync(secondLaunch.args[0], 'utf8'), '# upgraded mcp server\n')
        assert.deepEqual(mcpRuntimeDirs(userData).sort(), [
            path.basename(oldRoot),
            path.basename(secondLaunch.env.PYTHONPATH),
        ].sort())
    } finally {
        fs.rmSync(home, { recursive: true, force: true })
    }
})
