const path = require('path')
const fs   = require('fs')
const os   = require('os')

function createHardware({ paths, llmConfig, runCommand }) {
    const { USER_DATA } = paths
    const { usesManagedOllama } = llmConfig

    // Capture is a11y + OCR (no VL), so the old 16 GB / 6 GB VRAM floor is gone.
    // Chat still wants headroom for qwen3:8b; integrated GPUs are allowed at minimum.
    const HW_MIN = { ramGb: 8, vramGb: 0, diskGb: 8 }
    const HW_REC = { ramGb: 16, vramGb: 4, diskGb: 10 }
    const HW_MIN_MAC = { ramGb: 8, vramGb: 8, diskGb: 8 }
    const HW_REC_MAC = { ramGb: 16, vramGb: 16, diskGb: 10 }

    function gradeResource(value, min, rec) {
        if (value < min) return 'fail'
        if (value < rec) return 'warn'
        return 'ok'
    }

    function detectOsLabel() {
        if (process.platform === 'darwin') {
            return process.arch === 'arm64' ? 'macos-apple-silicon' : 'macos-intel'
        }
        if (process.platform !== 'win32') return process.platform
        const build = parseInt(os.release().split('.')[2] || '0', 10)
        return build >= 22000 ? 'windows11' : 'windows10'
    }

    async function getFreeDiskGb(dirPath) {
        // statfs is available on modern Node; the command fallback covers older
        // Electron runtimes and keeps the check working on both Windows and macOS.
        try {
            if (typeof fs.promises.statfs === 'function') {
                const s = await fs.promises.statfs(dirPath)
                return (Number(s.bavail) * Number(s.bsize)) / (1024 ** 3)
            }
        } catch (_) {                    }

        try {
            if (process.platform === 'win32') {
                const root = path.parse(path.resolve(dirPath)).root
                const letter = root.replace(/:\\?$/, '').replace('\\', '')
                const r = await runCommand('powershell', [
                    '-NoProfile', '-Command',
                    `(Get-PSDrive -Name '${letter}').Free`,
                ])
                const bytes = parseFloat(String(r.stdout || '').trim())
                if (!Number.isNaN(bytes) && bytes > 0) return bytes / (1024 ** 3)
            } else {
                const r = await runCommand('df', ['-kP', dirPath])
                const line = String(r.stdout || '').split(/\r?\n/).pop() || ''
                const availableKb = parseFloat(line.trim().split(/\s+/)[3])
                if (!Number.isNaN(availableKb) && availableKb > 0) return availableKb / (1024 ** 2)
            }
        } catch (_) {              }
        return 0
    }

    async function getVramGb() {
        // Apple Silicon shares memory with the GPU and has no nvidia-smi value;
        // getHardwareCheck maps that case to unified system memory.
        if (process.platform === 'darwin') return 0
        const r = await runCommand('nvidia-smi', [
            '--query-gpu=memory.total',
            '--format=csv,noheader,nounits',
        ])
        if (r.code !== 0) return 0
        const mb = parseFloat(String(r.stdout || '').trim().split(/\r?\n/)[0])
        if (Number.isNaN(mb) || mb <= 0) return 0
        return mb / 1024
    }

    async function getHardwareCheck() {
        // Apple Silicon reports shared unified memory rather than discrete VRAM;
        // use total memory for the GPU grade so capable Macs are not blocked.
        // Round RAM to the nearest GB so marketed machines are not blocked by a
        // small amount of reserved memory (e.g. 7.8 GB reported on an 8 GB box).
        const ramGb = Math.round(os.totalmem() / (1024 ** 3))
        const diskRaw = await getFreeDiskGb(USER_DATA)
        const vramRaw = await getVramGb()
        const diskGb = Math.round(diskRaw * 10) / 10
        const osId = detectOsLabel()
        const requirements = process.platform === 'darwin' ? HW_MIN_MAC : HW_MIN
        const recommended = process.platform === 'darwin' ? HW_REC_MAC : HW_REC
        const vramGb = process.platform === 'darwin'
            ? ramGb
            : Math.round(vramRaw * 10) / 10
        const osOk = process.platform === 'win32' || process.platform === 'darwin'

        const grades = {
            ram:  gradeResource(ramGb, requirements.ramGb, recommended.ramGb),
            vram: gradeResource(vramGb, requirements.vramGb, recommended.vramGb),
            disk: gradeResource(diskGb, requirements.diskGb, recommended.diskGb),
            os:   osOk ? 'ok' : 'fail',
        }

        let level = 'ready'
        if (Object.values(grades).includes('fail')) level = 'block'
        else if (Object.values(grades).includes('warn')) level = 'warn'

        return {
            level,
            grades,
            min: requirements,
            recommended,
            yours: {
                ramGb,
                vramGb,
                diskGb,
                os: osId,
                memoryLabel: process.platform === 'darwin' ? 'Unified memory' : 'GPU VRAM',
                osLabel: osId === 'windows11' ? 'Windows 11'
                    : osId === 'windows10' ? 'Windows 10'
                    : osId === 'macos-apple-silicon' ? 'macOS · Apple Silicon'
                    : osId === 'macos-intel' ? 'macOS · Intel'
                    : osId,
            },
            mode: usesManagedOllama() ? 'ollama' : 'external',
        }
    }

    return { getHardwareCheck }
}

module.exports = { createHardware }
