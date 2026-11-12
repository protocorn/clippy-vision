/**
 * Unsigned macOS packaging contract used by CI (npm run dist:mac).
 * Run with: node --test electron/lib/macos-build.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '../../package.json'), 'utf8'))

function macArchs(targetName) {
    const mac = pkg.build && pkg.build.mac
    assert.ok(mac, 'package.json build.mac must exist')
    const targets = Array.isArray(mac.target) ? mac.target : []
    const match = targets.find((item) => item && item.target === targetName)
    assert.ok(match, `mac target ${targetName} must exist`)
    return match.arch || []
}

test('dist:mac keeps --publish never so CI never needs GH_TOKEN or Apple secrets', () => {
    assert.match(pkg.scripts['dist:mac'], /electron-builder --mac --publish never/)
    assert.match(pkg.scripts.dist, /--publish never/)
})

test('macOS artifacts are unsigned dmg and zip for arm64 and x64', () => {
    const dmgArch = macArchs('dmg')
    const zipArch = macArchs('zip')
    for (const arch of ['arm64', 'x64']) {
        assert.ok(dmgArch.includes(arch), `dmg must include ${arch}`)
        assert.ok(zipArch.includes(arch), `zip must include ${arch}`)
    }
    assert.match(pkg.build.mac.artifactName, /ClippyVision-macOS-\$\{arch\}-\$\{version\}\.\$\{ext\}/)
    assert.equal(pkg.build.mac.forceCodeSigning, false)
    assert.equal(pkg.build.mac.gatekeeperAssess, false)
    assert.equal(pkg.build.mac.notarize, false)
})

test('Info.plist usage strings exist so unsigned Macs can still show TCC prompts', () => {
    const info = pkg.build.mac.extendInfo
    assert.match(info.NSScreenCaptureUsageDescription, /screen/i)
    assert.match(info.NSAccessibilityUsageDescription, /window|password|Accessibility/i)
})
