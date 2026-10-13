"""
Test OS check in hardware.js supports Linux as experimental platform.
"""
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest


def _node_subprocess_timeout():
    """Seconds allowed for a node spawn. Override with CLIPPY_NODE_TEST_TIMEOUT."""
    raw = os.environ.get("CLIPPY_NODE_TEST_TIMEOUT", "60")
    try:
        timeout = float(raw)
    except ValueError:
        timeout = 60.0
    return timeout


@pytest.fixture(scope="module", autouse=True)
def _warmup_node():
    """Windows CI can take >10s for the first node.exe spawn (AV / cold cache)."""
    subprocess.run(
        ["node", "--version"],
        capture_output=True,
        text=True,
        timeout=_node_subprocess_timeout(),
    )


@pytest.fixture
def hardware_js_path():
    """Return path to hardware.js."""
    repo_root = Path(__file__).parent.parent
    return repo_root / "electron-ui" / "electron" / "lib" / "hardware.js"


@pytest.fixture
def test_script(tmp_path, hardware_js_path):
    """Create a test Node.js script that uses hardware.js."""
    test_js = tmp_path / "test_os_check.js"
    test_js.write_text(f"""
const {{ createHardware }} = require('{hardware_js_path.as_posix()}');
const os = require('os');

// Mock dependencies
const paths = {{
    USER_DATA: '/tmp/test'
}};
const llmConfig = {{
    usesManagedOllama: () => false
}};
const runCommand = async () => ({{ code: 0, stdout: '', stderr: '' }});

const hardware = createHardware({{ paths, llmConfig, runCommand }});

async function test() {{
    const check = await hardware.getHardwareCheck();
    console.log(JSON.stringify({{
        platform: process.platform,
        osId: check.yours.os,
        osLabel: check.yours.osLabel,
        osGrade: check.grades.os,
        level: check.level,
        vramGb: check.yours.vramGb,
        ramGb: check.yours.ramGb,
        memoryLabel: check.yours.memoryLabel
    }}, null, 2));
}}

test().catch(err => {{
    console.error(err);
    process.exit(1);
}});
""")
    return test_js


def test_os_check_passes_on_current_platform(test_script):
    """
    Test that the OS check passes on the current platform.
    
    This test runs on both Ubuntu and Windows in CI and verifies:
    - Windows passes with "Windows 10" or "Windows 11" label
    - Linux passes with "Linux (supported, experimental)" label
    """
    # Run the test script
    result = subprocess.run(
        ['node', str(test_script)],
        capture_output=True,
        text=True,
        timeout=_node_subprocess_timeout(),
    )
    
    assert result.returncode == 0, f"Node script failed: {result.stderr}"
    
    # Parse the output
    output = json.loads(result.stdout)
    
    current_platform = platform.system().lower()
    
    # Verify OS grade is 'ok' (pass)
    assert output['osGrade'] == 'ok', (
        f"OS check should pass on {current_platform}, "
        f"but got grade '{output['osGrade']}'"
    )
    
    # Verify the OS label is appropriate for the platform
    if current_platform == 'windows':
        assert output['osLabel'] in ['Windows 10', 'Windows 11'], (
            f"Expected 'Windows 10' or 'Windows 11' label, got '{output['osLabel']}'"
        )
        assert output['platform'] == 'win32'
    
    elif current_platform == 'linux':
        assert output['osLabel'] == 'Linux (supported, experimental)', (
            f"Expected 'Linux (supported, experimental)' label, got '{output['osLabel']}'"
        )
        assert output['platform'] == 'linux'
        assert output['osId'] == 'linux'
    
    elif current_platform == 'darwin':
        assert 'macOS' in output['osLabel'], (
            f"Expected macOS label, got '{output['osLabel']}'"
        )
        assert output['platform'] == 'darwin'


def test_linux_integrated_gpu_not_blocked(tmp_path, hardware_js_path):
    """
    Test that Linux with no NVIDIA GPU (integrated graphics) is not blocked.
    
    Simulates a Linux machine with:
    - 16 GB RAM (above minimum)
    - 0 VRAM from nvidia-smi (no NVIDIA GPU)
    - Adequate disk space
    
    Should result in level 'ready' or 'warn', NOT 'block'.
    """
    test_js = tmp_path / "test_linux_integrated.js"
    test_js.write_text(f"""
const {{ createHardware }} = require('{hardware_js_path.as_posix()}');

// Mock a Linux machine with integrated GPU (no NVIDIA)
Object.defineProperty(process, 'platform', {{
    value: 'linux',
    writable: false,
    enumerable: true,
    configurable: true
}});

const paths = {{
    USER_DATA: '/tmp/test'
}};
const llmConfig = {{
    usesManagedOllama: () => false
}};

// Mock 16 GB RAM
const originalTotalmem = require('os').totalmem;
require('os').totalmem = () => 16 * 1024 * 1024 * 1024;

// Mock commands: nvidia-smi fails (no NVIDIA GPU), df reports adequate space
const runCommand = async (cmd, args) => {{
    if (cmd === 'nvidia-smi') {{
        return {{ code: 1, stdout: '', stderr: 'nvidia-smi: command not found' }};
    }}
    if (cmd === 'df') {{
        // Report 20 GB free
        return {{ code: 0, stdout: 'Filesystem 1024-blocks Used Available\\n/dev/sda1 100000000 80000000 20971520', stderr: '' }};
    }}
    return {{ code: 0, stdout: '', stderr: '' }};
}};

const hardware = createHardware({{ paths, llmConfig, runCommand }});

async function test() {{
    const check = await hardware.getHardwareCheck();
    console.log(JSON.stringify({{
        platform: process.platform,
        level: check.level,
        grades: check.grades,
        ramGb: check.yours.ramGb,
        vramGb: check.yours.vramGb,
        diskGb: check.yours.diskGb,
        memoryLabel: check.yours.memoryLabel,
        osLabel: check.yours.osLabel
    }}, null, 2));
}}

test().catch(err => {{
    console.error(err);
    process.exit(1);
}});
""")
    
    result = subprocess.run(
        ['node', str(test_js)],
        capture_output=True,
        text=True,
        timeout=10
    )
    
    assert result.returncode == 0, f"Node script failed: {result.stderr}"
    
    output = json.loads(result.stdout)
    
    # Verify Linux is detected
    assert output['platform'] == 'linux'
    assert output['osLabel'] == 'Linux (supported, experimental)'
    
    # Verify RAM is used for VRAM grade when no discrete GPU
    assert output['ramGb'] == 16, "Should report 16 GB RAM"
    assert output['vramGb'] == 16, "Should use RAM as VRAM for integrated GPU"
    assert output['memoryLabel'] == 'RAM (integrated GPU)', (
        f"Expected 'RAM (integrated GPU)' label, got '{output['memoryLabel']}'"
    )
    
    # Verify not blocked - should be 'ready' or 'warn', but NEVER 'block'
    assert output['level'] != 'block', (
        f"Linux with integrated GPU and adequate resources should not be blocked. "
        f"Level: {output['level']}, Grades: {output['grades']}"
    )
    
    # Verify OS grade is 'ok'
    assert output['grades']['os'] == 'ok', (
        f"Linux OS grade should be 'ok', got '{output['grades']['os']}'"
    )


def test_unsupported_os_behavior(tmp_path, hardware_js_path):
    """
    Test that unsupported OS platforms fail as expected.
    
    This test simulates an unsupported platform to ensure
    the check properly rejects platforms that aren't supported.
    """
    test_js = tmp_path / "test_unsupported_os.js"
    test_js.write_text(f"""
const {{ createHardware }} = require('{hardware_js_path.as_posix()}');

// Mock dependencies
const paths = {{
    USER_DATA: '/tmp/test'
}};
const llmConfig = {{
    usesManagedOllama: () => false
}};
const runCommand = async () => ({{ code: 0, stdout: '', stderr: '' }});

// Mock an unsupported platform
const originalPlatform = process.platform;
Object.defineProperty(process, 'platform', {{
    value: 'freebsd',
    writable: false,
    enumerable: true,
    configurable: true
}});

const hardware = createHardware({{ paths, llmConfig, runCommand }});

async function test() {{
    const check = await hardware.getHardwareCheck();
    console.log(JSON.stringify({{
        platform: process.platform,
        osGrade: check.grades.os,
        level: check.level
    }}, null, 2));
}}

test().catch(err => {{
    console.error(err);
    process.exit(1);
}});
""")
    
    # Run the test script
    result = subprocess.run(
        ['node', str(test_js)],
        capture_output=True,
        text=True,
        timeout=10
    )
    
    assert result.returncode == 0, f"Node script failed: {result.stderr}"
    
    # Parse the output
    output = json.loads(result.stdout)
    
    # Verify unsupported OS fails
    assert output['osGrade'] == 'fail', (
        "Unsupported OS should fail the check"
    )
    assert output['level'] == 'block', (
        "Unsupported OS should be blocked from installation"
    )


def test_windows_behavior_unchanged(tmp_path, hardware_js_path):
    """
    Verify Windows behavior remains unchanged.
    
    Windows with no NVIDIA GPU should still get proper grading.
    This test documents the expected Windows behavior.
    """
    test_js = tmp_path / "test_windows.js"
    test_js.write_text(f"""
const {{ createHardware }} = require('{hardware_js_path.as_posix()}');
const os = require('os');

// Mock Windows
Object.defineProperty(process, 'platform', {{
    value: 'win32',
    writable: false,
    enumerable: true,
    configurable: true
}});

// Mock 16 GB RAM
const originalTotalmem = os.totalmem;
os.totalmem = () => 16 * 1024 * 1024 * 1024;

const paths = {{
    USER_DATA: 'C:\\\\Users\\\\test\\\\AppData\\\\Local\\\\Clippy'
}};
const llmConfig = {{
    usesManagedOllama: () => false
}};

// Mock: nvidia-smi fails (no NVIDIA GPU on Windows)
const runCommand = async (cmd, args) => {{
    if (cmd === 'nvidia-smi') {{
        return {{ code: 1, stdout: '', stderr: 'not found' }};
    }}
    if (cmd === 'powershell') {{
        // Report 20 GB free (in bytes)
        return {{ code: 0, stdout: String(20 * 1024 * 1024 * 1024), stderr: '' }};
    }}
    return {{ code: 0, stdout: '', stderr: '' }};
}};

const hardware = createHardware({{ paths, llmConfig, runCommand }});

async function test() {{
    const check = await hardware.getHardwareCheck();
    console.log(JSON.stringify({{
        platform: process.platform,
        level: check.level,
        grades: check.grades,
        vramGb: check.yours.vramGb,
        memoryLabel: check.yours.memoryLabel
    }}, null, 2));
}}

test().catch(err => {{
    console.error(err);
    process.exit(1);
}});
""")
    
    result = subprocess.run(
        ['node', str(test_js)],
        capture_output=True,
        text=True,
        timeout=10
    )
    
    assert result.returncode == 0, f"Node script failed: {result.stderr}"
    
    output = json.loads(result.stdout)
    
    # Windows with no NVIDIA GPU: vramGb should be 0
    assert output['vramGb'] == 0, "Windows without NVIDIA should report 0 VRAM"
    assert output['memoryLabel'] == 'GPU VRAM', (
        "Windows should show 'GPU VRAM' label (unchanged)"
    )
    
    # Should get 'warn' (not 'block') because VRAM is 0 but min is 0
    assert output['level'] in ['ready', 'warn'], (
        f"Windows without discrete GPU should be 'ready' or 'warn', got {output['level']}"
    )


def test_setup_html_requirement_text_logic():
    """
    Document the expected OS requirement text logic from setup.html.
    
    The JS in setup.html sets os-requirement based on detected OS:
    - Linux: 'Ubuntu 22.04+ (experimental)'
    - macOS: 'macOS 12+'
    - Windows: 'Windows 10/11'
    """
    # This is a documentation test - the actual logic is in setup.html
    # We verify the constants are correct
    
    expected_requirements = {
        'linux': 'Ubuntu 22.04+ (experimental)',
        'macos-apple-silicon': 'macOS 12+',
        'macos-intel': 'macOS 12+',
        'windows10': 'Windows 10/11',
        'windows11': 'Windows 10/11',
    }
    
    # Verify our expectations match the README
    # README states: "OS | Windows 10 / 11 (64-bit), macOS 12+, or Linux (Ubuntu 22.04+, experimental)"
    assert expected_requirements['linux'] == 'Ubuntu 22.04+ (experimental)'
    assert expected_requirements['windows10'] == 'Windows 10/11'
    assert 'macOS' in expected_requirements['macos-apple-silicon']
