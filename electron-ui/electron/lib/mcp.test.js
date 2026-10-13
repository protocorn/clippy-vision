/**
 * VS Code / Claude Desktop MCP config paths must be resolved per platform.
 * Run with: node --test electron/lib/mcp.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const { createMcp, mcpClientDefinitions } = require('./mcp')

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
