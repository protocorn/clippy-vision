const { test } = require('node:test')
const assert = require('node:assert/strict')
const { createSetup } = require('./setup')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

// These tests exercise the public setup flow rather than private logging helpers.
// They simulate commands: no Python or package-manager install is performed.
async function scenario({ platform = 'linux', commandResult, existingPaths = [] }) {
    const oldPlatform = process.platform
    const oldPath = process.env.PATH
    const originalExistsSync = fs.existsSync
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-python-diagnostics-'))
    const calls = []
    const messages = []
    let pythonDone = false
    try {
        Object.defineProperty(process, 'platform', { configurable: true, value: platform })
        fs.existsSync = (candidate) => existingPaths.includes(String(candidate)) || originalExistsSync(candidate)
        const state = {
            doneSoFar: 0,
            setupWindow: {
                isDestroyed: () => false,
                webContents: {
                    send: (channel, payload) => {
                        messages.push({ channel, payload })
                        if (channel === 'step-update' && payload.key === 'python' && payload.state === 'done') {
                            pythonDone = true
                            // Stop after Python verification; do not enter package installation.
                            throw new Error('STOP_AFTER_PYTHON_VERIFIED')
                        }
                    },
                },
            },
        }
        const api = {
            runCommand: async (command, args) => {
                calls.push({ command, args })
                return commandResult(command, args, calls.length)
            },
        }
        const setup = createSetup({
            paths: {
                ROOT: dir, DATA_DIR: dir, USER_DATA: dir,
                SYSTEM_PYTHON_COMMAND: 'python3', PYTHON_COMMAND: 'python3',
                OLLAMA_COMMAND: 'ollama',
            },
            llmConfig: { usesManagedOllama: () => false },
            api, state,
        })
        await setup.runSetup('python')
        return {
            calls, messages, pythonDone,
            logs: messages.filter(m => m.channel === 'setup-log').map(m => String(m.payload.line)),
            steps: messages.filter(m => m.channel === 'step-update').map(m => m.payload),
        }
    } finally {
        fs.existsSync = originalExistsSync
        Object.defineProperty(process, 'platform', { configurable: true, value: oldPlatform })
        process.env.PATH = oldPath
        fs.rmSync(dir, { recursive: true, force: true })
    }
}

const result = (code, stdout = '', stderr = '') => ({ code, stdout, stderr })

function assertCommandLogged(run, command) {
    assert.ok(run.logs.some(line => line.includes(command) && line.includes('--version')),
        `Expected executable ${command} in logs; got ${JSON.stringify(run.logs)}`)
}

function assertDiagnostic(run, { code, stderr }) {
    assert.ok(run.logs.some(line => {
        const matchCode = new RegExp(`(?:exit|code)\\s*[:=]\\s*${code}(?!\\d)`, 'i').test(line)
        const hasStderr = /stderr/i.test(line) && (stderr ? line.includes(stderr) : /stderr\s*[:=]\s*["']{2}/i.test(line))
        return matchCode && hasStderr
    }), `Expected diagnostic with code ${code} and stderr ${JSON.stringify(stderr)}; got ${JSON.stringify(run.logs)}`)
}

function assertInstallAttempt(run, packageManager) {
    assert.ok(run.calls.some(c => c.command === packageManager && c.args[0] === 'install'),
        `Expected ${packageManager} install attempt`)
}

test('failed initial probe identifies actual executable, exit code and stderr', async () => {
    const run = await scenario({ commandResult: () => result(127, '', 'ENOENT: python3 missing') })
    assertCommandLogged(run, 'python3')
    assertDiagnostic(run, { code: 127, stderr: 'ENOENT: python3 missing' })
    assert.ok(run.steps.some(s => s.key === 'python' && s.state === 'error'))
})

test('unsupported Python version reports exit status even when command succeeds', async () => {
    const run = await scenario({ commandResult: () => result(0, 'Python 3.10.9', '') })
    assertCommandLogged(run, 'python3')
    assertDiagnostic(run, { code: 0, stderr: '' })
    assert.ok(run.steps.some(s => s.key === 'python' && s.state === 'error'))
})

test('Homebrew post-install probe failure includes diagnostics', async () => {
    let probes = 0
    const run = await scenario({
        platform: 'darwin',
        commandResult: (cmd, args) => {
            if (cmd === 'brew') return result(0, 'brew ready')
            if (cmd === 'python3') return ++probes === 1
                ? result(127, '', 'before install')
                : result(4, '', 'after install failure')
            return result(4, '', 'after install failure')
        },
    })
    assertInstallAttempt(run, 'brew')
    assertDiagnostic(run, { code: 4, stderr: 'after install failure' })
})

test('winget post-install probe failure includes diagnostics', async () => {
    let probes = 0
    const run = await scenario({
        platform: 'win32',
        commandResult: (cmd) => {
            if (cmd === 'winget') return result(0, 'installed')
            if (cmd === 'python3') return ++probes === 1
                ? result(127, '', 'before install')
                : result(4, '', 'after install failure')
            return result(4, '', 'after install failure')
        },
    })
    assertInstallAttempt(run, 'winget')
    assertDiagnostic(run, { code: 4, stderr: 'after install failure' })
})

test('Homebrew post-install rejects unsupported Python and logs status', async () => {
    let installed = false
    const run = await scenario({
        platform: 'darwin',
        commandResult: (cmd, args) => {
            if (cmd === 'brew') {
                if (args[0] === 'install') installed = true
                return result(0, 'ok')
            }
            return installed ? result(0, 'Python 3.10.9') : result(127, '', 'missing')
        },
    })
    assertInstallAttempt(run, 'brew')
    assertDiagnostic(run, { code: 0, stderr: '' })
    assert.ok(run.steps.some(s => s.key === 'python' && s.state === 'error'))
})

test('winget post-install rejects unsupported Python and logs status', async () => {
    let probes = 0
    const run = await scenario({
        platform: 'win32',
        commandResult: (cmd) => cmd === 'winget' ? result(0, 'ok')
            : cmd === 'python3' && ++probes === 1 ? result(127, '', 'missing') : result(0, 'Python 3.10.9'),
    })
    assertInstallAttempt(run, 'winget')
    assertDiagnostic(run, { code: 0, stderr: '' })
    assert.ok(run.steps.some(s => s.key === 'python' && s.state === 'error'))
})

test('fallback logs both attempted executables and accepts valid Python', async () => {
    // A mocked known installation path avoids depending on the host OS.
    const alternate = '/usr/bin/python3'
    const run = await scenario({
        existingPaths: [alternate],
        commandResult: (cmd) => cmd === alternate
            ? result(0, 'Python 3.13.5') : result(127, '', 'python3 command missing'),
    })
    assertCommandLogged(run, 'python3')
    assertCommandLogged(run, alternate)
    assertDiagnostic(run, { code: 127, stderr: 'python3 command missing' })
    assert.ok(run.calls.some(c => c.command === alternate))
    assert.equal(run.pythonDone, true)
    assert.equal(run.calls.some(c => ['winget', 'brew'].includes(c.command)), false)
})

test('valid Python completes the Python step without trying an installer', async () => {
    const run = await scenario({ commandResult: () => result(0, 'Python 3.13.5') })
    assert.equal(run.pythonDone, true)
    assert.ok(run.steps.some(s => s.key === 'python' && s.state === 'done'))
    assert.equal(run.calls.length, 1)
    assert.equal(run.calls[0].command, 'python3')
    assert.equal(run.logs.some(line => /Python detection.*(?:exit|code)/i.test(line)), false)
})
