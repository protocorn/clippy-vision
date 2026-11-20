/**
 * Relocatable CPython download used when no system Python 3.11+ is present.
 * Run with: node --test electron/lib/managed-python.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const http = require('node:http')
const os = require('node:os')
const path = require('node:path')

const {
    PYTHON_RUNTIME_DIRNAME,
    MANAGED_CPYTHON_VERSION,
    PYTHON_BUILD_STANDALONE_TAG,
    archiveSpec,
    pythonRuntimeDir,
    managedPythonExecutable,
    isHealthyManagedPython,
    tarExtractArgs,
    tarCommand,
    isAllowedDownloadUrl,
    sha256Buffer,
    downloadToFile,
    extractTarGz,
    installManagedPython,
} = require('./managed-python')

test('runtime lives under userData/python-runtime', () => {
    assert.equal(PYTHON_RUNTIME_DIRNAME, 'python-runtime')
    assert.equal(
        pythonRuntimeDir('/tmp/clippy-data'),
        path.join('/tmp/clippy-data', 'python-runtime'),
    )
})

test('managed Python executable paths match python-build-standalone layout', () => {
    assert.equal(
        managedPythonExecutable('C:\\Users\\ada\\Clippy\\python-runtime', 'win32'),
        'C:\\Users\\ada\\Clippy\\python-runtime\\python\\python.exe',
    )
    assert.equal(
        managedPythonExecutable('/home/ada/.config/clippy/python-runtime', 'linux'),
        '/home/ada/.config/clippy/python-runtime/python/bin/python3',
    )
    assert.equal(
        managedPythonExecutable('/Users/ada/Library/Application Support/clippy/python-runtime', 'darwin'),
        '/Users/ada/Library/Application Support/clippy/python-runtime/python/bin/python3',
    )
})

test('isHealthyManagedPython requires the platform interpreter', () => {
    const linuxPy = managedPythonExecutable('/tmp/runtime', 'linux')
    const winPy = managedPythonExecutable('C:\\Users\\ada\\runtime', 'win32')
    assert.equal(isHealthyManagedPython('/tmp/runtime', 'linux', (file) => file === linuxPy), true)
    assert.equal(isHealthyManagedPython('/tmp/runtime', 'linux', () => false), false)
    assert.equal(isHealthyManagedPython('C:\\Users\\ada\\runtime', 'win32', (file) => file === winPy), true)
})

test('Windows x64 archive is a hashed python-build-standalone 3.12 tarball', () => {
    const spec = archiveSpec('win32', 'x64')
    assert.ok(spec)
    assert.equal(spec.version, MANAGED_CPYTHON_VERSION)
    assert.equal(spec.tag, PYTHON_BUILD_STANDALONE_TAG)
    assert.match(spec.url, /^https:\/\/github\.com\/astral-sh\/python-build-standalone\/releases\/download\//)
    assert.match(spec.filename, /x86_64-pc-windows-msvc-install_only_stripped\.tar\.gz$/)
    assert.equal(spec.sha256.length, 64)
    assert.equal(spec.sha256, '52124cee54126f3f360eaa378288f6f64c402c983a3c14c95eff67f4af986aaa')
})

test('Linux x64 archive is a hashed python-build-standalone 3.12 tarball', () => {
    const spec = archiveSpec('linux', 'x64')
    assert.ok(spec)
    assert.match(spec.filename, /x86_64-unknown-linux-gnu-install_only_stripped\.tar\.gz$/)
    assert.equal(spec.sha256, '7bb1659e3235077b7f63d5b6eb6ce653c6fcd6c5041e9d5f73b42ce10421464d')
    assert.ok(spec.url.startsWith('https://'))
})

test('macOS arm64 and Intel have standalone archives', () => {
    const arm = archiveSpec('darwin', 'arm64')
    const intel = archiveSpec('darwin', 'x64')
    assert.match(arm.filename, /aarch64-apple-darwin-install_only_stripped\.tar\.gz$/)
    assert.match(intel.filename, /x86_64-apple-darwin-install_only_stripped\.tar\.gz$/)
})

test('unsupported platform/arch has no archive', () => {
    assert.equal(archiveSpec('linux', 'ia32'), null)
    assert.equal(archiveSpec('freebsd', 'x64'), null)
})

test('tar extract args are non-shell and relocatable', () => {
    assert.deepEqual(
        tarExtractArgs('/tmp/python.tgz', '/tmp/dest'),
        ['-xf', '/tmp/python.tgz', '-C', '/tmp/dest'],
    )
})

test('Windows tar uses System32 tar.exe so GNU tar does not treat C:\\ as a remote', () => {
    assert.equal(
        tarCommand('win32', { SystemRoot: 'C:\\Windows' }),
        'C:\\Windows\\System32\\tar.exe',
    )
    assert.equal(tarCommand('linux'), 'tar')
    assert.equal(tarCommand('darwin'), 'tar')
})

test('isAllowedDownloadUrl allows https and localhost http only', () => {
    assert.equal(isAllowedDownloadUrl('https://github.com/astral-sh/python-build-standalone/releases/download/x'), true)
    assert.equal(isAllowedDownloadUrl('http://127.0.0.1:9/file.tar.gz'), true)
    assert.equal(isAllowedDownloadUrl('http://localhost:9/file.tar.gz'), true)
    assert.equal(isAllowedDownloadUrl('http://[::1]:9/file.tar.gz'), true)
    assert.equal(isAllowedDownloadUrl('http://example.com/file.tar.gz'), false)
    assert.equal(isAllowedDownloadUrl('ftp://127.0.0.1/file.tar.gz'), false)
})

test('sha256Buffer matches Node crypto for the same bytes', () => {
    const payload = Buffer.from('clippy-vision')
    assert.equal(
        sha256Buffer(payload),
        require('crypto').createHash('sha256').update(payload).digest('hex'),
    )
})

test('downloadToFile follows a redirect, writes bytes, and checks sha256', async () => {
    const payload = Buffer.from('relocatable-python-fixture')
    const digest = sha256Buffer(payload)
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-py-'))
    const dest = path.join(tmp, 'runtime.tar.gz')

    const server = http.createServer((req, res) => {
        if (req.url === '/start') {
            res.writeHead(302, { Location: '/file.tar.gz' })
            res.end()
            return
        }
        if (req.url === '/file.tar.gz') {
            res.writeHead(200, { 'Content-Length': String(payload.length) })
            res.end(payload)
            return
        }
        res.writeHead(404)
        res.end()
    })

    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
    const { port } = server.address()
    try {
        const progress = []
        const result = await downloadToFile(`http://127.0.0.1:${port}/start`, dest, {
            expectedSha256: digest,
            onProgress: (event) => progress.push(event),
        })
        assert.equal(result.sha256, digest)
        assert.equal(result.bytes, payload.length)
        assert.equal(fs.readFileSync(dest).equals(payload), true)
        assert.ok(progress.length >= 1)
        assert.equal(progress[progress.length - 1].received, payload.length)
    } finally {
        server.close()
        fs.rmSync(tmp, { recursive: true, force: true })
    }
})

test('extractTarGz unpacks a tarball with the native tar command', async () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-tar-'))
    const src = path.join(tmp, 'src')
    const dest = path.join(tmp, 'dest')
    const archive = path.join(tmp, 'runtime.tar.gz')
    fs.mkdirSync(path.join(src, 'python', 'bin'), { recursive: true })
    fs.writeFileSync(path.join(src, 'python', 'bin', 'python3'), 'ok\n')
    const { spawnSync } = require('child_process')
    const packed = spawnSync('tar', ['-czf', archive, '-C', src, 'python'], { encoding: 'utf8' })
    assert.equal(packed.status, 0, packed.stderr)
    try {
        await extractTarGz(archive, dest)
        assert.equal(
            fs.readFileSync(path.join(dest, 'python', 'bin', 'python3'), 'utf8'),
            'ok\n',
        )
    } finally {
        fs.rmSync(tmp, { recursive: true, force: true })
    }
})

test('installManagedPython stages, extracts, and returns the relocatable interpreter', async () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-py-install-'))
    try {
        const exe = await installManagedPython({
            userData: tmp,
            platform: 'linux',
            arch: 'x64',
            downloadFn: async (_url, destPath) => {
                fs.mkdirSync(path.dirname(destPath), { recursive: true })
                fs.writeFileSync(destPath, 'archive')
                return { sha256: 'abc', bytes: 7 }
            },
            extractFn: async (_archivePath, destDir) => {
                const target = managedPythonExecutable(destDir, 'linux')
                fs.mkdirSync(path.dirname(target), { recursive: true })
                fs.writeFileSync(target, '')
            },
        })
        const expected = managedPythonExecutable(pythonRuntimeDir(tmp), 'linux')
        assert.equal(exe, expected)
        assert.equal(fs.existsSync(expected), true)
        assert.equal(fs.existsSync(`${pythonRuntimeDir(tmp)}.partial`), false)
    } finally {
        fs.rmSync(tmp, { recursive: true, force: true })
    }
})

test('extractTarGz on win32 spawns System32 tar.exe', async () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-tar-win-'))
    const archive = path.join(tmp, 'python.tgz')
    const calls = []
    try {
        await extractTarGz(archive, tmp, {
            platform: 'win32',
            env: { SystemRoot: 'C:\\Windows' },
            spawnFn: (cmd, args) => {
                calls.push([cmd, args])
                return {
                    stderr: { on() {} },
                    on(event, cb) {
                        if (event === 'exit') queueMicrotask(() => cb(0))
                    },
                }
            },
        })
        assert.equal(calls.length, 1)
        assert.equal(calls[0][0], 'C:\\Windows\\System32\\tar.exe')
        assert.deepEqual(calls[0][1], tarExtractArgs(archive, tmp))
    } finally {
        fs.rmSync(tmp, { recursive: true, force: true })
    }
})

test('downloadToFile refuses a non-https redirect off localhost', async () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-py-'))
    const dest = path.join(tmp, 'runtime.tar.gz')
    const server = http.createServer((_req, res) => {
        res.writeHead(302, { Location: 'http://example.com/cpython.tar.gz' })
        res.end()
    })
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
    const { port } = server.address()
    try {
        await assert.rejects(
            () => downloadToFile(`http://127.0.0.1:${port}/start`, dest),
            /Refusing Python download redirect/,
        )
        assert.equal(fs.existsSync(dest), false)
    } finally {
        server.close()
        fs.rmSync(tmp, { recursive: true, force: true })
    }
})

test('downloadToFile rejects a checksum mismatch and deletes the file', async () => {
    const payload = Buffer.from('wrong-bytes')
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-py-'))
    const dest = path.join(tmp, 'runtime.tar.gz')

    const server = http.createServer((_req, res) => {
        res.writeHead(200, { 'Content-Length': String(payload.length) })
        res.end(payload)
    })
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
    const { port } = server.address()
    try {
        await assert.rejects(
            () => downloadToFile(`http://127.0.0.1:${port}/runtime.tar.gz`, dest, {
                expectedSha256: '0'.repeat(64),
            }),
            /checksum mismatch/,
        )
        assert.equal(fs.existsSync(dest), false)
    } finally {
        server.close()
        fs.rmSync(tmp, { recursive: true, force: true })
    }
})
