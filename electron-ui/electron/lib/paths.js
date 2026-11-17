const path = require('path')
const fs   = require('fs')
const os   = require('os')

const OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
const RELEASE_REPOSITORY = 'protocorn/clippy-vision'
const LOCAL_EMBEDDING_MODEL = 'local:sentence-transformers/all-MiniLM-L6-v2'
const OLLAMA_MAX_LOADED_MODELS = '1'
const OLLAMA_NUM_PARALLEL = '1'
const DEFAULT_API_PORT = 8000
const PYTHON_VENV_DIRNAME = 'python-venv'

const DEFAULT_LLM_CONFIG = {
    provider: 'ollama',
    base_url: OLLAMA_BASE_URL,
    api_key: '',
    cli_command: '',
    chat_model: 'qwen3:8b',
    // Capture uses accessibility + OCR; no vision model is downloaded or required.
    embedding_model: LOCAL_EMBEDDING_MODEL,
}

function pythonVenvDir(userData) {
    return path.join(userData, PYTHON_VENV_DIRNAME)
}

function venvPythonPath(venvDir, platform = process.platform) {
    const p = platform === 'win32' ? path.win32 : path.posix
    return platform === 'win32'
        ? p.join(venvDir, 'Scripts', 'python.exe')
        : p.join(venvDir, 'bin', 'python')
}

function isHealthyVenv(venvDir, platform = process.platform, existsFn = fs.existsSync) {
    if (!venvDir) return false
    try {
        return Boolean(existsFn(venvPythonPath(venvDir, platform)))
    } catch (_) {
        return false
    }
}

function createPaths(app, electronDir) {
    const IS_PACKAGED = app.isPackaged
    // Packaged builds keep mutable state in Electron's user-data directory. During
    // development the repository data directory is used so local runs behave the
    // same way without copying state into an installation folder.
    const ROOT            = IS_PACKAGED
        ? path.join(process.resourcesPath, 'clippy')
        : path.join(electronDir, '../..')
    const USER_DATA       = IS_PACKAGED ? app.getPath('userData') : ROOT
    const DATA_DIR        = IS_PACKAGED
        ? path.join(USER_DATA, 'data')
        : path.join(ROOT, 'core', 'data')
    const ASSETS          = path.join(electronDir, '../assets')
    const ICON_INACTIVE   = path.join(ASSETS, 'logo_inactive.png')
    const ICON_ACTIVE     = path.join(ASSETS, 'logo_active.png')
    const CAPTURE_SCRIPT  = path.join(ROOT, 'core', 'screen_capture.py')
    const API_SCRIPT      = path.join(ROOT, 'api_server.py')
    const REQUIREMENTS    = path.join(ROOT, 'requirements.txt')
    const SETUP_FLAG      = path.join(USER_DATA, 'setup_complete.json')
    const RESIDENCY_FILE  = path.join(DATA_DIR, 'model_residency.json')
    const CAPTURE_STATE_FILE = path.join(DATA_DIR, 'capture_status.json')
    const LLM_CONFIG_FILE = path.join(DATA_DIR, 'llm_config.json')
    const API_STATE_FILE  = path.join(USER_DATA, 'api_process.json')
    const DESKTOP_SETTINGS_FILE = path.join(USER_DATA, 'desktop_settings.json')
    const RELEASE_CHECK_FILE = path.join(USER_DATA, 'release_check.json')
    const PYTHON_VENV_DIR = pythonVenvDir(USER_DATA)
    // Electron does not always inherit the interactive shell PATH. These common
    // macOS locations cover Homebrew, npm/pnpm, Volta, and nvm installations.
    const PATH_HINTS = process.platform === 'darwin'
        ? [
            '/opt/homebrew/bin',
            '/usr/local/bin',
            '/Applications/Ollama.app/Contents/Resources',
            '/Library/Frameworks/Python.framework/Versions/3.12/bin',
            '/Library/Frameworks/Python.framework/Versions/3.11/bin',
            path.join(os.homedir(), '.local/bin'),
            path.join(os.homedir(), '.npm-global/bin'),
            path.join(os.homedir(), 'Library/pnpm'),
            path.join(os.homedir(), '.volta/bin'),
            path.join(os.homedir(), '.nvm/current/bin'),
        ]
        : []

    function commandFromKnownPaths(name, fallback) {
        // Prefer an explicitly discoverable executable before falling back to the
        // shell name, which lets spawn() still resolve system installations.
        const candidate = PATH_HINTS.map((dir) => path.join(dir, name)).find((file) => fs.existsSync(file))
        return candidate || fallback
    }

    const SYSTEM_PYTHON_COMMAND = process.env.CLIPPY_PYTHON || commandFromKnownPaths(
        'python3',
        process.platform === 'win32' ? 'python' : 'python3',
    )
    // Prefer an existing healthy app venv so packaged launches and post-setup
    // runs never install into (or depend on) the system site-packages.
    let PYTHON_COMMAND = SYSTEM_PYTHON_COMMAND
    if (!process.env.CLIPPY_PYTHON && isHealthyVenv(PYTHON_VENV_DIR)) {
        PYTHON_COMMAND = venvPythonPath(PYTHON_VENV_DIR)
    }
    const OLLAMA_COMMAND = process.env.CLIPPY_OLLAMA || commandFromKnownPaths('ollama', 'ollama')

    return {
        ELECTRON_DIR: electronDir,
        IS_PACKAGED,
        ROOT,
        USER_DATA,
        DATA_DIR,
        ASSETS,
        ICON_INACTIVE,
        ICON_ACTIVE,
        CAPTURE_SCRIPT,
        API_SCRIPT,
        REQUIREMENTS,
        SETUP_FLAG,
        RESIDENCY_FILE,
        CAPTURE_STATE_FILE,
        LLM_CONFIG_FILE,
        API_STATE_FILE,
        DESKTOP_SETTINGS_FILE,
        RELEASE_CHECK_FILE,
        PYTHON_VENV_DIR,
        PATH_HINTS,
        commandFromKnownPaths,
        SYSTEM_PYTHON_COMMAND,
        get PYTHON_COMMAND() {
            return PYTHON_COMMAND
        },
        set PYTHON_COMMAND(value) {
            PYTHON_COMMAND = value
        },
        OLLAMA_COMMAND,
        DEFAULT_API_PORT,
        OLLAMA_BASE_URL,
        RELEASE_REPOSITORY,
        LOCAL_EMBEDDING_MODEL,
        OLLAMA_MAX_LOADED_MODELS,
        OLLAMA_NUM_PARALLEL,
        DEFAULT_LLM_CONFIG,
    }
}

module.exports = {
    createPaths,
    pythonVenvDir,
    venvPythonPath,
    isHealthyVenv,
    PYTHON_VENV_DIRNAME,
}
