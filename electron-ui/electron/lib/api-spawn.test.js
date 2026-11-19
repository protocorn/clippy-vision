
const { test } = require('node:test')
const assert = require('node:assert/strict')
const { once } = require('node:events')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { createApiSpawn } = require('./api-spawn')

test('API stdout is received before Python exits', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-api-'))
    const script = path.join(dir, 'api_stdout_test.py')

    // Keep Python alive until stdin is closed.
    fs.writeFileSync(script, [
        'import sys',
        'print("API_STDOUT_READY")',
        'sys.stdin.readline()',
    ].join('\n'))

    const state = { apiPort: 19876, apiProcess: null }
    const logs = []
    const originalLog = console.log

    const api = createApiSpawn({
        paths: {
            ROOT: dir,
            DATA_DIR: dir,
            USER_DATA: dir,
            API_SCRIPT: script,
            API_STATE_FILE: path.join(dir, 'api-state.json'),
            PYTHON_COMMAND: process.env.CLIPPY_TEST_PYTHON ||
                (process.platform === 'win32' ? 'python' : 'python3'),
            PATH_HINTS: [],
        },
        llmConfig: {
            readLLMConfig: () => ({}),
        },
        state,
    })

    let proc
    let timeout

    try {
        // Capture logs emitted by the Electron API handler.
        console.log = (...args) => {
            logs.push(args.join(' '))
        }

        await api.startServer()
        proc = state.apiProcess

        assert.ok(proc, 'API process was not started')

        const output = await Promise.race([
            once(proc.stdout, 'data'),
            new Promise((_, reject) => {
                timeout = setTimeout(() => {
                    reject(new Error(
                        'stdout was not received while Python was running'
                    ))
                }, 3000)
            }),
        ])

        assert.match(output[0].toString(), /API_STDOUT_READY/)

        // Python must still be running when the message arrives.
        assert.equal(proc.exitCode, null)

        // Electron must also have captured the message.
        assert.ok(
            logs.some(line =>
                line.includes('[API] API_STDOUT_READY')
            ),
            'Electron did not capture the API stdout message'
        )
    } finally {
        clearTimeout(timeout)
        console.log = originalLog

        if (proc && proc.exitCode === null) {
            const closed = once(proc, 'close')
            proc.stdin.end()
            await closed
        }

        fs.rmSync(dir, { recursive: true, force: true })
    }
})
