
const { test } = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const { createApiSpawn } = require('./api-spawn')

function makeFakeProcess() {
    const child = new EventEmitter()
    child.stdout = new EventEmitter()
    child.stderr = new EventEmitter()
    return child
}

function makeApi(child) {
    return createApiSpawn({
        paths: {
            ROOT: process.cwd(),
            DATA_DIR: process.cwd(),
            PATH_HINTS: [],
        },
        llmConfig: {},
        state: {},
        spawnProcess: () => child,
    })
}

test('runCommand waits for close after exit and late stdout', async () => {
    const child = makeFakeProcess()
    const api = makeApi(child)

    let finished = false
    const resultPromise = api.runCommand('python3', ['--version'])
    resultPromise.then(() => { finished = true })

    // The process exits before all stdout data is delivered.
    child.emit('exit', 0)
    await Promise.resolve()

    assert.equal(finished, false,
        'runCommand resolved on exit before stdout was collected')

    child.stdout.emit('data', Buffer.from('Python 3.13.5\n'))
    child.emit('close', 0)

    const result = await resultPromise
    assert.deepEqual(result, {
        code: 0,
        stdout: 'Python 3.13.5',
        stderr: '',
    })
})

test('runCommand preserves exit code and stderr', async () => {
    const child = makeFakeProcess()
    const api = makeApi(child)

    const resultPromise = api.runCommand('python3', ['--version'])

    child.emit('exit', 2)
    child.stderr.emit('data', Buffer.from('version check failed\n'))
    child.emit('close', 2)

    assert.deepEqual(await resultPromise, {
        code: 2,
        stdout: '',
        stderr: 'version check failed',
    })
})

test('runCommand handles a spawn error', async () => {
    const child = makeFakeProcess()
    const api = makeApi(child)

    const resultPromise = api.runCommand('python3', ['--version'])

    child.emit('error', new Error('ENOENT'))
    child.emit('close', -2)

    const result = await resultPromise
    assert.equal(result.code, 1)
    assert.match(result.stderr, /ENOENT/)
})
