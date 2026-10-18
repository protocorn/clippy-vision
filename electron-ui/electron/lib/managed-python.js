/**
 * App-managed CPython runtime for packaged Clippy Vision.
 *
 * Packaged Windows, macOS, and Linux apps do not bundle Python (keeps
 * installer size unchanged). First-run setup downloads a relocatable
 * python-build-standalone CPython 3.12 into Electron userData, then the
 * existing app-managed venv is created from that interpreter.
 *
 * CLIPPY_PYTHON still wins. A healthy system Python 3.11+ is used when present
 * so machines that already have Python skip this download.
 */
const crypto = require('crypto')
const fs = require('fs')
const http = require('http')
const https = require('https')
const path = require('path')
const { spawn } = require('child_process')

const PYTHON_RUNTIME_DIRNAME = 'python-runtime'
const MANAGED_CPYTHON_VERSION = '3.12.15'
const PYTHON_BUILD_STANDALONE_TAG = '20261001'
const PYTHON_BUILD_STANDALONE_BASE =
    `https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_BUILD_STANDALONE_TAG}`
const USER_AGENT = 'Clippy-Vision'
const MAX_REDIRECTS = 5
const STALL_TIMEOUT_MS = 60_000

// install_only_stripped: relocatable, includes pip/venv, no debug symbols.
// Hashes from the 20261001 SHA256SUMS file on the GitHub release.
const ARCHIVES = {
    'win32-x64': {
        filename: 'cpython-3.12.15+20261001-x86_64-pc-windows-msvc-install_only_stripped.tar.gz',
        sha256: '52124cee54126f3f360eaa378288f6f64c402c983a3c14c95eff67f4af986aaa',
        approxBytes: 26_000_000,
    },
    'linux-x64': {
        filename: 'cpython-3.12.15+20261001-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz',
        sha256: '7bb1659e3235077b7f63d5b6eb6ce653c6fcd6c5041e9d5f73b42ce10421464d',
        approxBytes: 31_000_000,
    },
    'linux-arm64': {
        filename: 'cpython-3.12.15+20261001-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz',
        sha256: '0b35f4dc08d58534eb82e024989e2db9873885dccd4f2a316ff55c0cec146123',
        approxBytes: 31_000_000,
    },
    'darwin-x64': {
        filename: 'cpython-3.12.15+20261001-x86_64-apple-darwin-install_only_stripped.tar.gz',
        sha256: 'd101ac54bc34afff54741406261325dc896b7b646a36a58fff4845ef0a00b2ce',
        approxBytes: 20_000_000,
    },
    'darwin-arm64': {
        filename: 'cpython-3.12.15+20261001-aarch64-apple-darwin-install_only_stripped.tar.gz',
        sha256: '10cab8f6ed6202fdd81637aa6eda4af8d5b7eaa8fc42f9df3c6bea4923de0d93',
        approxBytes: 20_000_000,
    },
}

function normalizeArch(arch) {
    const value = String(arch || '').toLowerCase()
    if (value === 'x64' || value === 'x86_64' || value === 'amd64') return 'x64'
    if (value === 'arm64' || value === 'aarch64') return 'arm64'
    return value
}

function archiveKey(platform, arch) {
    return `${platform}-${normalizeArch(arch)}`
}

function archiveSpec(platform = process.platform, arch = process.arch) {
    const spec = ARCHIVES[archiveKey(platform, arch)]
    if (!spec) return null
    return {
        ...spec,
        version: MANAGED_CPYTHON_VERSION,
        tag: PYTHON_BUILD_STANDALONE_TAG,
        url: `${PYTHON_BUILD_STANDALONE_BASE}/${spec.filename}`,
    }
}

function pythonRuntimeDir(userData) {
    return path.join(userData, PYTHON_RUNTIME_DIRNAME)
}

function managedPythonExecutable(runtimeDir, platform = process.platform) {
    const p = platform === 'win32' ? path.win32 : path.posix
    if (platform === 'win32') return p.join(runtimeDir, 'python', 'python.exe')
    return p.join(runtimeDir, 'python', 'bin', 'python3')
}

function isHealthyManagedPython(runtimeDir, platform = process.platform, existsFn = fs.existsSync) {
    if (!runtimeDir) return false
    try {
        return Boolean(existsFn(managedPythonExecutable(runtimeDir, platform)))
    } catch (_) {
        return false
    }
}

function tarExtractArgs(archivePath, destDir) {
    return ['-xf', archivePath, '-C', destDir]
}

function tarCommand(platform = process.platform, env = process.env) {
    // Windows PATH can put Git's GNU tar first. GNU tar treats C:\Users\... as
    // a remote host:path unless --force-local is set. bsdtar in System32 does
    // not, so always use that binary on win32.
    if (platform === 'win32') {
        const root = env.SystemRoot || env.SYSTEMROOT || 'C:\\Windows'
        return path.win32.join(root, 'System32', 'tar.exe')
    }
    return 'tar'
}

function isAllowedDownloadUrl(url) {
    let parsed
    try {
        parsed = new URL(url)
    } catch (_) {
        return false
    }
    if (parsed.protocol === 'https:') return true
    if (parsed.protocol === 'http:') {
        const host = String(parsed.hostname || '').toLowerCase()
        return host === '127.0.0.1' || host === 'localhost' || host === '::1' || host === '[::1]'
    }
    return false
}

function sha256FileSync(filePath) {
    const hash = crypto.createHash('sha256')
    hash.update(fs.readFileSync(filePath))
    return hash.digest('hex')
}

function sha256Buffer(buffer) {
    return crypto.createHash('sha256').update(buffer).digest('hex')
}

function followRedirectsGet(url, callback, redirectsLeft = MAX_REDIRECTS) {
    if (!isAllowedDownloadUrl(url)) {
        callback(new Error(`Refusing Python download from ${url}`))
        return
    }
    const parsed = new URL(url)
    const lib = parsed.protocol === 'http:' ? http : https
    const req = lib.get({
        protocol: parsed.protocol,
        hostname: parsed.hostname,
        port: parsed.port || (parsed.protocol === 'https:' ? 443 : 80),
        path: parsed.pathname + parsed.search,
        headers: {
            'User-Agent': USER_AGENT,
            Accept: '*/*',
        },
    }, (res) => {
        const status = res.statusCode || 0
        if ([301, 302, 303, 307, 308].includes(status) && res.headers.location) {
            res.resume()
            if (redirectsLeft <= 0) {
                callback(new Error('Too many redirects while downloading Python'))
                return
            }
            const next = new URL(res.headers.location, url).toString()
            if (!isAllowedDownloadUrl(next)) {
                callback(new Error(`Refusing Python download redirect to ${next}`))
                return
            }
            followRedirectsGet(next, callback, redirectsLeft - 1)
            return
        }
        callback(null, res)
    })
    req.on('error', (err) => callback(err))
    return req
}

function downloadToFile(url, destPath, { onProgress, expectedSha256, getFn = followRedirectsGet } = {}) {
    return new Promise((resolve, reject) => {
        fs.mkdirSync(path.dirname(destPath), { recursive: true })
        const hash = crypto.createHash('sha256')
        let received = 0
        let settled = false
        let stallTimer = null

        let response = null
        const fail = (err) => {
            if (settled) return
            settled = true
            if (stallTimer) clearTimeout(stallTimer)
            try { if (response) response.destroy() } catch (_) { /* ignore */ }
            try { fs.unlinkSync(destPath) } catch (_) { /* ignore */ }
            reject(err)
        }

        const bumpStall = () => {
            if (stallTimer) clearTimeout(stallTimer)
            stallTimer = setTimeout(() => {
                fail(new Error('Python download stalled'))
            }, STALL_TIMEOUT_MS)
        }

        bumpStall()
        getFn(url, (err, res) => {
            if (err) return fail(err)
            response = res
            const status = res.statusCode || 0
            if (status !== 200) {
                res.resume()
                return fail(new Error(`Python download failed with HTTP ${status}`))
            }
            const total = Number(res.headers['content-length'] || 0)
            const stream = fs.createWriteStream(destPath)
            bumpStall()

            res.on('data', (chunk) => {
                bumpStall()
                received += chunk.length
                hash.update(chunk)
                if (typeof onProgress === 'function') {
                    onProgress({ received, total })
                }
            })
            res.pipe(stream)
            stream.on('finish', () => {
                if (stallTimer) clearTimeout(stallTimer)
                const digest = hash.digest('hex')
                if (expectedSha256 && digest !== expectedSha256) {
                    return fail(new Error(
                        `Python runtime checksum mismatch (got ${digest}, expected ${expectedSha256})`,
                    ))
                }
                if (settled) return
                settled = true
                resolve({ sha256: digest, bytes: received })
            })
            stream.on('error', fail)
            res.on('error', fail)
        })
    })
}

function extractTarGz(archivePath, destDir, {
    spawnFn = spawn,
    platform = process.platform,
    env = process.env,
} = {}) {
    return new Promise((resolve, reject) => {
        fs.mkdirSync(destDir, { recursive: true })
        const proc = spawnFn(tarCommand(platform, env), tarExtractArgs(archivePath, destDir), {
            windowsHide: true,
        })
        let stderr = ''
        if (proc.stderr) proc.stderr.on('data', (chunk) => { stderr += chunk.toString() })
        proc.on('error', (err) => {
            reject(new Error(
                `Could not extract Python runtime (tar not found or failed: ${err.message})`,
            ))
        })
        proc.on('exit', (code) => {
            if (code === 0) {
                resolve()
                return
            }
            const detail = String(stderr || '').trim().split(/\r?\n/).find(Boolean) || `tar exited ${code}`
            reject(new Error(`Could not extract Python runtime: ${detail}`))
        })
    })
}

function rmDirIfExists(dir) {
    try {
        if (fs.existsSync(dir)) fs.rmSync(dir, { recursive: true, force: true })
    } catch (_) { /* ignore */ }
}

async function installManagedPython({
    userData,
    platform = process.platform,
    arch = process.arch,
    onProgress,
    downloadFn = downloadToFile,
    extractFn = extractTarGz,
} = {}) {
    const spec = archiveSpec(platform, arch)
    if (!spec) {
        const err = new Error(
            `No relocatable Python build is available for ${platform}-${arch}. Install Python 3.11+ or set CLIPPY_PYTHON.`,
        )
        err.code = 'python-standalone-unsupported'
        throw err
    }

    const runtimeDir = pythonRuntimeDir(userData)
    const stagingDir = `${runtimeDir}.partial`
    const archivePath = path.join(stagingDir, spec.filename)

    rmDirIfExists(stagingDir)
    fs.mkdirSync(stagingDir, { recursive: true })

    try {
        await downloadFn(spec.url, archivePath, {
            expectedSha256: spec.sha256,
            onProgress,
        })
        await extractFn(archivePath, stagingDir)
        try { fs.unlinkSync(archivePath) } catch (_) { /* ignore */ }

        const exe = managedPythonExecutable(stagingDir, platform)
        if (!fs.existsSync(exe)) {
            throw new Error(`Python runtime extracted but ${exe} is missing`)
        }

        rmDirIfExists(runtimeDir)
        fs.renameSync(stagingDir, runtimeDir)
        return managedPythonExecutable(runtimeDir, platform)
    } catch (err) {
        rmDirIfExists(stagingDir)
        throw err
    }
}

module.exports = {
    PYTHON_RUNTIME_DIRNAME,
    MANAGED_CPYTHON_VERSION,
    PYTHON_BUILD_STANDALONE_TAG,
    PYTHON_BUILD_STANDALONE_BASE,
    ARCHIVES,
    archiveSpec,
    archiveKey,
    normalizeArch,
    pythonRuntimeDir,
    managedPythonExecutable,
    isHealthyManagedPython,
    tarExtractArgs,
    tarCommand,
    isAllowedDownloadUrl,
    sha256FileSync,
    sha256Buffer,
    downloadToFile,
    extractTarGz,
    installManagedPython,
}
