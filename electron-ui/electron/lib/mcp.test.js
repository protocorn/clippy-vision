/**
 * VS Code / Claude Desktop MCP config paths must be resolved per platform.
 * Run with: node --test electron/lib/mcp.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const { createMcp, mcpClientDefinitions, isAppImageMountPath, looksLikeAppImageRuntime, entryHasStaleAppImageMount } = require('./mcp')

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

        const mcp = createMcp({
            paths: {
                ROOT: mountRoot,
                DATA_DIR: path.join(userData, 'data'),
                USER_DATA: userData,
                PYTHON_COMMAND: python,
            },
            runCommand: async () => ({ code: 1, stdout: '' }),
            shell: { openExternal: async () => {} },
            runtime: {
                platform: 'linux',
                home,
                env: { APPIMAGE: appImage, APPDIR: path.join(home, 'tmp', '.mount_ClippyABC123') },
            },
        })

        const launch = await mcp.getMcpLaunchConfig()
        assert.equal(launch.ready, true, launch.issues && launch.issues.join(' '))
        assert.equal(launch.command, python)
        assert.equal(launch.args[0], path.join(userData, 'mcp-runtime', 'mcp_server.py'))
        assert.equal(launch.env.PYTHONPATH, path.join(userData, 'mcp-runtime'))
        assert.equal(launch.env.CLIPPY_DATA_DIR, path.join(userData, 'data'))
        assert.ok(!String(launch.args[0]).includes('.mount_'))
        assert.ok(!String(launch.env.PYTHONPATH).includes('.mount_'))
        assert.ok(!String(launch.launcher).includes('.mount_'))
        assert.equal(launch.launcher, path.join(userData, 'clippy-mcp'))
        assert.equal(fs.existsSync(launch.args[0]), true)
        assert.equal(fs.existsSync(launch.launcher), true)
        const launcherText = fs.readFileSync(launch.launcher, 'utf8')
        assert.match(launcherText, /mcp-runtime/)
        assert.match(launcherText, /^exec /m)
        const execLine = launcherText.split('\n').find((line) => line.startsWith('exec '))
        assert.ok(execLine)
        assert.ok(!execLine.includes('.mount_'))
        assert.ok(!launcherText.includes(mountRoot))
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

        const cursorDoc = JSON.parse(fs.readFileSync(cursorPath, 'utf8'))
        assert.equal(cursorDoc.mcpServers['clippy-vision'].args[0], path.join(userData, 'mcp-runtime', 'mcp_server.py'))
        assert.equal(cursorDoc.mcpServers['clippy-vision'].env.PYTHONPATH, path.join(userData, 'mcp-runtime'))
        assert.equal(cursorDoc.mcpServers['other-app'].command, 'npx')

        const vscodeDoc = JSON.parse(fs.readFileSync(vscodePath, 'utf8'))
        assert.deepEqual(vscodeDoc.servers, { unrelated: { command: 'node', args: ['server.js'] } })
        assert.equal(entryHasStaleAppImageMount(cursorDoc.mcpServers['clippy-vision']), false)
    } finally {
        fs.rmSync(home, { recursive: true, force: true })
    }
})
