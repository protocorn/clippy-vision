#!/usr/bin/env node
/**
 * CI: actually download the pinned python-build-standalone archive,
 * verify SHA-256 (inside installManagedPython), extract, and create a venv.
 *
 * Does not pip-install requirements.txt — torch/transformers are too large
 * for this job. The point is the first-run download-to-venv path that
 * CLIPPY_PYTHON skips in the packaged launch tests.
 */
const fs = require('fs')
const os = require('os')
const path = require('path')
const { spawnSync } = require('child_process')
const {
    installManagedPython,
    archiveSpec,
} = require('../electron/lib/managed-python')
const { venvPythonPath } = require('../electron/lib/paths')

async function main() {
    const spec = archiveSpec()
    if (!spec) {
        console.error(`No relocatable Python archive for ${process.platform}-${process.arch}`)
        process.exit(1)
    }

    const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'clippy-managed-py-'))
    console.log(`platform=${process.platform} arch=${process.arch}`)
    console.log(`Downloading ${spec.url}`)
    console.log(`Expected SHA-256 ${spec.sha256}`)

    try {
        const exe = await installManagedPython({ userData })
        console.log(`Runtime interpreter: ${exe}`)
        if (!fs.existsSync(exe)) {
            throw new Error(`Managed Python missing at ${exe}`)
        }

        const version = spawnSync(exe, ['--version'], { encoding: 'utf8' })
        if (version.status !== 0) {
            throw new Error(version.stderr || version.stdout || `python --version exited ${version.status}`)
        }
        console.log((version.stdout || version.stderr).trim())

        const venvDir = path.join(userData, 'python-venv')
        console.log(`Creating venv at ${venvDir}`)
        const created = spawnSync(exe, ['-m', 'venv', venvDir], { encoding: 'utf8' })
        if (created.status !== 0) {
            throw new Error(created.stderr || created.stdout || `python -m venv exited ${created.status}`)
        }

        const venvPy = venvPythonPath(venvDir)
        if (!fs.existsSync(venvPy)) {
            throw new Error(`venv interpreter missing at ${venvPy}`)
        }
        const probe = spawnSync(
            venvPy,
            ['-c', 'import sys; print(sys.version.split()[0]); print(sys.executable)'],
            { encoding: 'utf8' },
        )
        if (probe.status !== 0) {
            throw new Error(probe.stderr || `venv python probe exited ${probe.status}`)
        }
        console.log(probe.stdout.trim())
        console.log('Managed Python download-to-venv OK')
    } finally {
        try { fs.rmSync(userData, { recursive: true, force: true }) } catch (_) { /* ignore */ }
    }
}

main().catch((err) => {
    console.error(err && err.stack ? err.stack : err)
    process.exit(1)
})
