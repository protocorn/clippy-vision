import {
 settingsView, settingsName, settingsIntro,
 identityFields, identityNewKey, identityNewVal, identityAddBtn, memoryFacts, profileStatus,
 updateCheckToggle, updatesStatus, aboutVersion, aboutPlatform, aboutModel, aboutMemory,
 aboutBadge, privacyList, privacyStatus, privacyCount, workspaceRootsList, workspaceRootInput,
 workspaceRootAddBtn, workspaceRootsStatus, settingsUserLabel, settingsRuntimeLabel,
 settingsRuntimeDot, settingsNavItems, settingsPanels, mcpReadyLabel, mcpReadyDetail,
 mcpConfigPreview, mcpClientList, mcpCopyBtn, mcpStatus, MCP_CLIENT_ICONS, PRIVACY_TARGET_ICONS,
 store, updateBanner,
} from './dom.js'
import { showView, setStatus } from './utils.js'
import { openTimeline } from './timeline.js'
import { confirmDialog } from './dialogs.js'
import { captureRuntimeLabel } from './capture-ui.js'

let profileSaveInFlight = false

export function showSettingsPanel(panelId) {
 const id = typeof panelId === 'string' && panelId ? panelId : 'profile'
 for (const item of settingsNavItems()) {
 item.classList.toggle('active', item.dataset.settingsPanel === id)
 }
 for (const panel of settingsPanels()) {
 const match = panel.dataset.settingsPanel === id
 panel.classList.toggle('active', match)
 panel.hidden = !match
 }
}

export function wireSettingsNav() {
 for (const item of settingsNavItems()) {
 item.addEventListener('click', () => showSettingsPanel(item.dataset.settingsPanel))
 }
}

export function renderIdentityFields() {
 identityFields.innerHTML = ''
 const keys = Object.keys(store.identityDraft).sort()
 if (!keys.length) {
 const empty = document.createElement('div')
 empty.className = 'settings-hint'
 empty.style.marginBottom = '0'
 empty.textContent = 'No identity fields yet. Clippy adds these from captured work, or you can add one below.'
 identityFields.appendChild(empty)
 return
 }
 for (const key of keys) {
 const row = document.createElement('div')
 row.className = 'identity-row'
 row.dataset.key = key

 const keyInput = document.createElement('input')
 keyInput.className = 'identity-key'
 keyInput.type = 'text'
 keyInput.value = key
 keyInput.readOnly = true
 keyInput.title = key

 const valInput = document.createElement('input')
 valInput.className = 'identity-val'
 valInput.type = 'text'
 valInput.value = store.identityDraft[key] || ''
 valInput.addEventListener('input', () => {
 store.identityDraft[key] = valInput.value
 })
 valInput.addEventListener('change', () => {
 saveProfile({ statusText: 'Memory updated.' })
 })

 const removeBtn = document.createElement('button')
 removeBtn.type = 'button'
 removeBtn.className = 'identity-remove'
 removeBtn.title = 'Remove this field'
 removeBtn.textContent = 'x'
 removeBtn.addEventListener('click', () => removeIdentityField(key))

 row.appendChild(keyInput)
 row.appendChild(valInput)
 row.appendChild(removeBtn)
 identityFields.appendChild(row)
 }
}

export async function removeIdentityField(key) {
 const { confirmDialog } = await import('./dialogs.js')
 const ok = await confirmDialog({
  title: 'Remove memory',
  message: `Remove “${key}” from your memory profile?`,
  confirmLabel: 'Remove',
  danger: true,
 })
 if (!ok) return
 store.identityCleared.add(key)
 delete store.identityDraft[key]
 renderIdentityFields()
 await saveProfile({ statusText: 'Memory removed.' })
}

export async function addIdentityField() {
 const key = identityNewKey.value.trim().toLowerCase().replace(/\s+/g, '_')
 const val = identityNewVal.value.trim()
 if (!key) {
 setStatus(profileStatus, 'Enter a field name to add.', 'error')
 return
 }
 store.identityDraft[key] = val
 store.identityCleared.delete(key)
 identityNewKey.value = ''
 identityNewVal.value = ''
 renderIdentityFields()
 await saveProfile({ statusText: 'Memory added.' })
}

export async function loadUpdateCheck() {
 try {
 updateCheckToggle.checked = await window.clippy.getUpdateCheckEnabled()
 const bannerOpen = updateBanner && !updateBanner.hidden
 if (updatesStatus) {
 updatesStatus.className = 'settings-foot-muted'
 updatesStatus.textContent = bannerOpen
  ? 'Update available · see banner'
  : (updateCheckToggle.checked ? 'Automatic checks on · up to date' : 'Automatic checks off')
 }
 } catch (error) {
 setStatus(updatesStatus, `Could not read update setting: ${error.message}`, 'error')
 }
}

function platformLabel() {
 const ua = navigator.userAgentData
 if (ua?.platform) {
  return ua.platform === 'Windows' ? 'Windows, x64' : ua.platform
 }
 const raw = navigator.userAgent || ''
 if (/Windows NT 10/i.test(raw)) return 'Windows 11, x64'
 if (/Windows/i.test(raw)) return 'Windows'
 if (/Mac OS X/i.test(raw)) return 'macOS'
 if (/Linux/i.test(raw)) return 'Linux'
 return navigator.platform || 'Unknown'
}

async function refinePlatformLabel() {
 if (!aboutPlatform || !navigator.userAgentData?.getHighEntropyValues) return
 try {
  const ua = await navigator.userAgentData.getHighEntropyValues(['architecture', 'bitness', 'platformVersion'])
  const platform = navigator.userAgentData.platform || 'Unknown'
  const arch = ua.architecture || (ua.bitness === '64' ? 'x64' : '')
  if (platform === 'Windows') {
   const major = parseInt(String(ua.platformVersion || '').split('.')[0], 10)
   const win = Number.isFinite(major) && major >= 13 ? 'Windows 11' : 'Windows 10'
   aboutPlatform.textContent = arch ? `${win}, ${arch}` : win
   return
  }
  aboutPlatform.textContent = arch ? `${platform}, ${arch}` : platform
 } catch (_) { /* keep fallback */ }
}

export async function loadAboutInfo() {
 if (aboutPlatform) aboutPlatform.textContent = platformLabel()
 refinePlatformLabel()

 if (aboutMemory) aboutMemory.textContent = '…'
 if (aboutModel) aboutModel.textContent = '…'

 try {
  if (window.clippy?.getAboutInfo) {
   const info = await window.clippy.getAboutInfo()
   if (aboutVersion) aboutVersion.textContent = info.version || 'Unavailable'
   if (aboutMemory) aboutMemory.textContent = info.memoryLabel || 'Unavailable'
   if (aboutModel) aboutModel.textContent = info.chatModel || 'Local'
  } else {
   const version = await window.clippy.getAppVersion()
   if (aboutVersion) aboutVersion.textContent = version || 'Unavailable'
   if (aboutMemory) aboutMemory.textContent = 'Unavailable'
   if (aboutModel) aboutModel.textContent = 'Local (Ollama)'
  }
 } catch (_) {
  if (aboutVersion) aboutVersion.textContent = 'Unavailable'
  if (aboutMemory) aboutMemory.textContent = 'Unavailable'
  if (aboutModel) aboutModel.textContent = 'Local'
 }

 if (aboutBadge) {
  const bannerOpen = updateBanner && !updateBanner.hidden
  aboutBadge.textContent = bannerOpen ? 'Update available' : 'Up to date'
  aboutBadge.classList.toggle('is-update', bannerOpen)
 }
}

function updatePrivacyCount(targets) {
 if (!privacyCount) return
 const total = targets.length
 const on = targets.filter((t) => t.enabled).length
 privacyCount.textContent = total
  ? `${on} of ${total} apps redacted`
  : 'No apps listed'
}

export function renderPrivacyTargets(targets) {
 privacyList.innerHTML = ''
 updatePrivacyCount(targets)
 if (!targets.length) {
  const empty = document.createElement('div')
  empty.className = 'settings-hint'
  empty.style.marginBottom = '0'
  empty.textContent = 'No privacy targets available.'
  privacyList.appendChild(empty)
  return
 }
 for (const target of targets) {
  const row = document.createElement('div')
  row.className = 'privacy-row'

  const main = document.createElement('div')
  main.className = 'privacy-row-main'
  appendSettingsRowIcon(main, PRIVACY_TARGET_ICONS[target.id], target.label)

  const textWrap = document.createElement('div')
  textWrap.className = 'privacy-row-text'
  const strong = document.createElement('strong')
  strong.textContent = target.label
  const span = document.createElement('span')
  span.textContent = target.description
  textWrap.appendChild(strong)
  textWrap.appendChild(span)
  main.appendChild(textWrap)

  const toggleLabel = document.createElement('label')
  toggleLabel.className = 'toggle'
  const checkbox = document.createElement('input')
  checkbox.type = 'checkbox'
  checkbox.checked = !!target.enabled
  const track = document.createElement('span')
  track.className = 'toggle-track'
  toggleLabel.appendChild(checkbox)
  toggleLabel.appendChild(track)

  checkbox.addEventListener('change', () => togglePrivacyTarget(target.id, checkbox))

  row.appendChild(main)
  row.appendChild(toggleLabel)
  privacyList.appendChild(row)
 }
}

export async function loadPrivacySettings() {
 setStatus(privacyStatus, 'Loading...', null)
 try {
  const data = await window.clippy.getPrivacySettings()
  renderPrivacyTargets(data.targets || [])
  setStatus(privacyStatus, '', null)
 } catch (error) {
  privacyList.innerHTML = ''
  if (privacyCount) privacyCount.textContent = '—'
  setStatus(privacyStatus, `Could not load privacy settings: ${error.message}`, 'error')
 }
 loadWorkspaceRoots()
 loadWatchSettings()
}

export function renderWorkspaceRoots(roots) {
 if (!workspaceRootsList) return
 workspaceRootsList.innerHTML = ''
 const items = Array.isArray(roots) ? roots : []
 if (!items.length) {
  const empty = document.createElement('div')
  empty.className = 'settings-hint'
  empty.textContent = 'No trusted folders yet. Browse to a project folder, or paste its path.'
  workspaceRootsList.appendChild(empty)
  return
 }
 for (const root of items) {
  const row = document.createElement('div')
  row.className = 'privacy-row'

  const text = document.createElement('div')
  text.className = 'privacy-row-text'
  const title = document.createElement('strong')
  title.textContent = root.label || 'Folder'
  const detail = document.createElement('span')
  detail.textContent = root.path || ''
  text.appendChild(title)
  text.appendChild(detail)

  const removeBtn = document.createElement('button')
  removeBtn.type = 'button'
  removeBtn.className = 'quiet-btn'
  removeBtn.textContent = 'Remove'
  removeBtn.addEventListener('click', () => removeWorkspaceRoot(root.root_id))

  row.appendChild(text)
  row.appendChild(removeBtn)
  workspaceRootsList.appendChild(row)
 }
}

export async function loadWorkspaceRoots() {
 if (!workspaceRootsList) return
 if (workspaceRootsStatus) setStatus(workspaceRootsStatus, 'Loading...', null)
 try {
  const data = await window.clippy.listWorkspaceRoots()
  renderWorkspaceRoots(data.roots || [])
  if (workspaceRootsStatus) setStatus(workspaceRootsStatus, '', null)
 } catch (error) {
  workspaceRootsList.innerHTML = ''
  if (workspaceRootsStatus) {
   setStatus(workspaceRootsStatus, `Could not load trusted folders: ${error.message}`, 'error')
  }
 }
}

export async function addWorkspaceRootFromInput() {
 if (!workspaceRootInput) return
 const path = workspaceRootInput.value.trim()
 if (!path) {
  setStatus(workspaceRootsStatus, 'Enter a folder path first.', 'error')
  return
 }
 if (workspaceRootAddBtn) workspaceRootAddBtn.disabled = true
 setStatus(workspaceRootsStatus, 'Saving...', null)
 try {
  const data = await window.clippy.addWorkspaceRoot(path)
  workspaceRootInput.value = ''
  renderWorkspaceRoots(data.roots || [])
  setStatus(workspaceRootsStatus, 'Trusted folder added.', 'ok')
  setTimeout(() => setStatus(workspaceRootsStatus, '', null), 1500)
 } catch (error) {
  setStatus(workspaceRootsStatus, `Could not add folder: ${error.message}`, 'error')
 } finally {
  if (workspaceRootAddBtn) workspaceRootAddBtn.disabled = false
 }
}

export async function removeWorkspaceRoot(rootId) {
 if (!rootId) return
 setStatus(workspaceRootsStatus, 'Removing...', null)
 try {
  const data = await window.clippy.removeWorkspaceRoot(rootId)
  renderWorkspaceRoots(data.roots || [])
  setStatus(workspaceRootsStatus, 'Removed.', 'ok')
  setTimeout(() => setStatus(workspaceRootsStatus, '', null), 1500)
 } catch (error) {
  setStatus(workspaceRootsStatus, `Could not remove: ${error.message}`, 'error')
 }
}

export async function togglePrivacyTarget(id, checkbox) {
 const desired = checkbox.checked
 checkbox.disabled = true
 setStatus(privacyStatus, 'Saving...', null)
 try {
  await window.clippy.updatePrivacySettings({ [id]: desired })
  const rows = privacyList.querySelectorAll('.privacy-row input[type="checkbox"]')
  const total = rows.length
  let on = 0
  rows.forEach((el) => { if (el.checked) on += 1 })
  if (privacyCount) privacyCount.textContent = `${on} of ${total} apps redacted`
  setStatus(privacyStatus, 'Saved.', 'ok')
  setTimeout(() => setStatus(privacyStatus, '', null), 1500)
 } catch (error) {
  checkbox.checked = !desired
  setStatus(privacyStatus, `Could not save: ${error.message}`, 'error')
 } finally {
  checkbox.disabled = false
 }
}

async function refreshRuntimeStatus() {
 if (!settingsRuntimeLabel) return
 let status = null
 try {
  if (window.clippy?.getCaptureStatus) status = await window.clippy.getCaptureStatus()
 } catch (_) { /* ignore */ }
 settingsRuntimeLabel.textContent = captureRuntimeLabel(status)
 if (settingsRuntimeDot) settingsRuntimeDot.classList.toggle('is-off', !(status && status.active))
}

export async function loadSettings(panelId) {
 showSettingsPanel(panelId || 'profile')
 loadUpdateCheck()
 loadAboutInfo()
 loadPrivacySettings()
 loadMcpSettings()
 refreshRuntimeStatus()
 setStatus(profileStatus, 'Loading...', null)
 try {
  const profile = await window.clippy.getProfile()
  settingsName.value = profile.name || ''
  settingsIntro.value = profile.introduction || ''
  if (settingsUserLabel) settingsUserLabel.textContent = profile.name || '—'
  store.identityDraft = { ...(profile.identity || {}) }
  store.identityCleared = new Set()
  renderIdentityFields()
  renderMemoryFacts(profile.facts)
  setStatus(profileStatus, '', null)
 } catch (error) {
  setStatus(profileStatus, `Could not load profile: ${error.message}`, 'error')
 }
 loadCaptureSettings()
}

export async function saveProfile({ statusText = 'Saved.' } = {}) {
 const name = settingsName.value.trim()
 if (!name) {
  setStatus(profileStatus, 'Display name cannot be empty.', 'error')
  return false
 }
 if (profileSaveInFlight) return false
 profileSaveInFlight = true
 if (identityAddBtn) identityAddBtn.disabled = true
 setStatus(profileStatus, 'Saving...', null)

 // Include cleared fields as empty strings so the backend can override them away
 const identityPayload = { ...store.identityDraft }
 for (const key of store.identityCleared) {
  if (!(key in identityPayload)) identityPayload[key] = ''
 }

 try {
  const updated = await window.clippy.updateProfile({
   name,
   introduction: settingsIntro.value,
   identity: identityPayload,
  })
  settingsName.value = updated.name || ''
  settingsIntro.value = updated.introduction || ''
  if (settingsUserLabel) settingsUserLabel.textContent = updated.name || '—'
  store.identityDraft = { ...(updated.identity || {}) }
  store.identityCleared = new Set()
  renderIdentityFields()
  renderMemoryFacts(updated.facts)
  setStatus(profileStatus, statusText, 'ok')
  setTimeout(() => setStatus(profileStatus, '', null), 2000)
  return true
 } catch (error) {
  setStatus(profileStatus, `Could not save: ${error.message}`, 'error')
  return false
 } finally {
  profileSaveInFlight = false
  if (identityAddBtn) identityAddBtn.disabled = false
 }
}

export async function openSettings(panelId) {
 showView(settingsView)
 await loadSettings(panelId)
}

export function closeSettings() {
 openTimeline()
}

export async function saveUpdateCheck() {
 const desired = updateCheckToggle.checked
 updateCheckToggle.disabled = true
 try {
  const saved = await window.clippy.setUpdateCheckEnabled(desired)
  updateCheckToggle.checked = saved
  if (updatesStatus) {
   updatesStatus.className = 'settings-foot-muted'
   updatesStatus.textContent = saved ? 'Automatic checks on · up to date' : 'Automatic checks off'
  }
 } catch (error) {
  updateCheckToggle.checked = !desired
  setStatus(updatesStatus, `Could not save: ${error.message}`, 'error')
 } finally {
  updateCheckToggle.disabled = false
 }
}

export async function loadMcpSettings() {
 wireOtherMcpApps()
 if (!window.clippy?.mcp?.getLaunchConfig) {
 mcpReadyLabel.textContent = 'Unavailable'
 mcpReadyDetail.textContent = 'Restart Clippy Vision to load MCP helpers.'
 mcpCopyBtn.disabled = true
 mcpClientList.innerHTML = ''
 return
 }
 setStatus(mcpStatus, '', null)
 try {
 const status = window.clippy.mcp.listClients
 ? await window.clippy.mcp.listClients()
 : null
 const config = status?.launch || await window.clippy.mcp.getLaunchConfig()
 const payload = {
 mcpServers: {
 [config.name]: config.clientConfig,
 },
 }
 mcpConfigPreview.value = JSON.stringify(payload, null, 2)
 if (config.ready) {
 mcpReadyLabel.textContent = 'Ready'
 mcpReadyDetail.textContent = 'Launcher and Python paths are pinned. Connect an app below, or copy the JSON.'
 mcpCopyBtn.disabled = false
 } else {
 mcpReadyLabel.textContent = 'Not ready'
 mcpReadyDetail.textContent = (config.issues || []).join(' ') || 'Missing launcher or Python.'
 mcpCopyBtn.disabled = false
 }
 renderMcpClients(status?.clients || [], Boolean(config.ready))
 } catch (error) {
 mcpReadyLabel.textContent = 'Error'
 mcpReadyDetail.textContent = error.message || String(error)
 mcpConfigPreview.value = ''
 mcpClientList.innerHTML = ''
 setStatus(mcpStatus, `Could not load MCP config: ${error.message}`, 'error')
 }
}

export function appendSettingsRowIcon(parent, src, alt) {
 if (!src) return
 const icon = document.createElement('img')
 icon.className = 'privacy-row-icon'
 icon.src = src
 icon.alt = alt || ''
 icon.loading = 'lazy'
 parent.appendChild(icon)
}

// Paste-the-JSON apps. icon is filled in when the asset exists.
const OTHER_MCP_APPS = [
 { id: 'devin', label: 'Devin Desktop', hint: 'Paste the copied JSON into Devin\u2019s mcp_config.json. This app was Windsurf.', docs: 'https://docs.devin.ai/cli/extensibility/mcp/configuration', icon: '../assets/devin_desktop.png' },
 { id: 'claude-code', label: 'Claude Code', hint: 'Add the copied JSON with the command in the docs.', docs: 'https://code.claude.com/docs/en/mcp#option-3-add-a-local-stdio-server', icon: '../assets/claude_code.png' },
 { id: 'cline', label: 'Cline', hint: 'Paste the copied JSON into Cline\u2019s MCP settings.', docs: 'https://docs.cline.bot/mcp/mcp-overview#local-server-stdio', icon: '../assets/cline.png' },
 { id: 'roo-code', label: 'Roo Code', hint: 'Paste the copied JSON into Roo\u2019s MCP config.', docs: 'https://roocodeinc.github.io/Roo-Code/features/mcp/using-mcp-in-roo/#stdio-transport', icon: '../assets/roo_code.png' },
 { id: 'continue', label: 'Continue', hint: 'Drop the copied JSON into .continue/mcpServers.', docs: 'https://docs.continue.dev/customize/deep-dives/mcp#how-to-use-standard-inputoutput-stdio', icon: '../assets/continue.png' },
 { id: 'jetbrains', label: 'JetBrains', hint: 'Paste the copied JSON into AI Assistant\u2019s MCP dialog.', docs: 'https://www.jetbrains.com/help/ai-assistant/mcp.html#connect-to-an-mcp-server', icon: '../assets/jetbrains.png' },
 { id: 'kiro', label: 'Kiro', hint: 'Paste the copied JSON into Kiro\u2019s MCP config.', docs: 'https://kiro.dev/docs/mcp/', icon: '../assets/kiro.png' },
 { id: 'lm-studio', label: 'LM Studio', hint: 'Paste the copied JSON into mcp.json.', docs: 'https://lmstudio.ai/docs/app/mcp', icon: '../assets/lm_studio.png' },
]

function renderOtherMcpApps(list) {
 list.innerHTML = ''
 for (const app of OTHER_MCP_APPS) {
  const row = document.createElement('div')
  row.className = 'privacy-row'
  const main = document.createElement('div')
  main.className = 'privacy-row-main'
  if (app.icon) {
   appendSettingsRowIcon(main, app.icon, app.label)
  } else {
   const mark = document.createElement('span')
   mark.className = 'privacy-row-icon mcp-app-mark'
   mark.setAttribute('aria-hidden', 'true')
   mark.textContent = app.label.trim().charAt(0).toUpperCase()
   main.appendChild(mark)
  }
  const textWrap = document.createElement('div')
  textWrap.className = 'privacy-row-text'
  const title = document.createElement('strong')
  title.textContent = app.label
  const detail = document.createElement('span')
  detail.textContent = app.hint
  textWrap.appendChild(title)
  textWrap.appendChild(detail)
  main.appendChild(textWrap)
  const link = document.createElement('a')
  link.className = 'header-btn'
  link.href = app.docs
  link.target = '_blank'
  link.rel = 'noopener noreferrer'
  link.textContent = 'View documentation'
  row.appendChild(main)
  row.appendChild(link)
  list.appendChild(row)
 }
}

function wireOtherMcpApps() {
 const toggle = document.getElementById('mcp-other-apps-toggle')
 const list = document.getElementById('mcp-other-apps')
 if (!toggle || !list || toggle.dataset.wired === '1') return
 toggle.dataset.wired = '1'
 toggle.addEventListener('click', () => {
  const open = list.hidden
  if (open && !list.childElementCount) renderOtherMcpApps(list)
  list.hidden = !open
  toggle.setAttribute('aria-expanded', open ? 'true' : 'false')
  toggle.textContent = open ? 'Hide other apps' : 'Also connect with other apps'
 })
}

export function renderMcpClients(clients, ready) {
 mcpClientList.innerHTML = ''
 if (!clients.length) {
 const empty = document.createElement('div')
 empty.className = 'settings-hint'
 empty.style.marginBottom = '0'
 empty.textContent = 'No MCP clients registered yet.'
 mcpClientList.appendChild(empty)
 return
 }
 for (const client of clients) {
 const row = document.createElement('div')
 row.className = 'privacy-row'

 const main = document.createElement('div')
 main.className = 'privacy-row-main'
 appendSettingsRowIcon(main, MCP_CLIENT_ICONS[client.id], client.label)

 const textWrap = document.createElement('div')
 textWrap.className = 'privacy-row-text'
 const title = document.createElement('strong')
 title.textContent = client.label
 const detail = document.createElement('span')
 const state = client.connected ? 'Connected' : 'Not connected'
 detail.textContent = `${state} - ${client.hint || ''}`
 textWrap.appendChild(title)
 textWrap.appendChild(detail)
 main.appendChild(textWrap)

 const actions = document.createElement('div')
 actions.style.display = 'flex'
 actions.style.gap = '8px'

 if (client.connected) {
 const disconnectBtn = document.createElement('button')
 disconnectBtn.type = 'button'
 disconnectBtn.className = 'header-btn'
 disconnectBtn.textContent = 'Disconnect'
 disconnectBtn.disabled = !window.clippy?.mcp?.disconnect
 disconnectBtn.addEventListener('click', () => disconnectMcpClient(client.id, disconnectBtn))
 actions.appendChild(disconnectBtn)
 } else {
 const connectBtn = document.createElement('button')
 connectBtn.type = 'button'
 connectBtn.className = 'header-btn'
 connectBtn.textContent = 'Connect'
 connectBtn.disabled = !ready || !window.clippy?.mcp?.connect
 connectBtn.title = ready ? `Connect ${client.label}` : 'Fix launcher/Python first'
 connectBtn.addEventListener('click', () => connectMcpClient(client.id, connectBtn))
 actions.appendChild(connectBtn)
 }

 row.appendChild(main)
 row.appendChild(actions)
 mcpClientList.appendChild(row)
 }
}

export async function connectMcpClient(clientId, button) {
 button.disabled = true
 setStatus(mcpStatus, 'Connecting...', null)
 try {
 const result = await window.clippy.mcp.connect(clientId)
 if (!result.ok) {
 setStatus(mcpStatus, result.error || 'Connect failed.', 'error')
 return
 }
 setStatus(mcpStatus, result.message || 'Connected.', 'ok')
 await loadMcpSettings()
 } catch (error) {
 setStatus(mcpStatus, `Could not connect: ${error.message}`, 'error')
 } finally {
 button.disabled = false
 }
}

export async function disconnectMcpClient(clientId, button) {
 button.disabled = true
 setStatus(mcpStatus, 'Disconnecting...', null)
 try {
 const result = await window.clippy.mcp.disconnect(clientId)
 if (!result.ok) {
 setStatus(mcpStatus, result.error || 'Disconnect failed.', 'error')
 await loadMcpSettings()
 return
 }
 setStatus(mcpStatus, result.message || 'Disconnected.', 'ok')
 await loadMcpSettings()
 } catch (error) {
 setStatus(mcpStatus, `Could not disconnect: ${error.message}`, 'error')
 } finally {
 button.disabled = false
 }
}

export async function copyMcpConfig() {
 mcpCopyBtn.disabled = true
 setStatus(mcpStatus, 'Copying...', null)
 try {
 const result = await window.clippy.mcp.copyConfig()
 if (result.ready) {
      setStatus(mcpStatus, "Copied. Paste into your app's MCP settings.", 'ok')
 } else {
 setStatus(
 mcpStatus,
 `Copied, but fix: ${(result.issues || []).join(' ')}`,
 'error',
 )
 }
 setTimeout(() => setStatus(mcpStatus, '', null), 4000)
 } catch (error) {
 setStatus(mcpStatus, `Could not copy: ${error.message}`, 'error')
 } finally {
 mcpCopyBtn.disabled = false
 }
}

function factStem(name) {
 return String(name || '').replace(/\.exe$/i, '').trim().toLowerCase()
}

// Same identities as core/process_names.py. A saved chrome.exe matches
// the macOS process Google Chrome.
const PROCESS_KEYS = {
 chrome: 'chrome',
 'google chrome': 'chrome',
 msedge: 'edge',
 'microsoft edge': 'edge',
 brave: 'brave',
 'brave browser': 'brave',
 firefox: 'firefox',
 'mozilla firefox': 'firefox',
 telegram: 'telegram',
 telegramdesktop: 'telegram',
 whatsapp: 'whatsapp',
 discord: 'discord',
 slack: 'slack',
 signal: 'signal',
 instagram: 'instagram',
 cursor: 'cursor',
 'clippy vision': 'clippy',
 'clippy-vision': 'clippy',
}

function processKey(name) {
 const stem = factStem(name)
 return PROCESS_KEYS[stem] || stem
}

const FACT_PAGE_SIZE = 4
let factPage = 0
let factItems = []

export function renderMemoryFacts(facts) {
 if (!memoryFacts) return
 factItems = Array.isArray(facts) ? facts : []
 const pageCount = Math.max(1, Math.ceil(factItems.length / FACT_PAGE_SIZE))
 if (factPage > pageCount - 1) factPage = pageCount - 1
 if (factPage < 0) factPage = 0
 memoryFacts.innerHTML = ''

 const title = document.createElement('h3')
 title.className = 'settings-subhead'
 title.textContent = 'What Clippy remembers'
 memoryFacts.appendChild(title)

 const hint = document.createElement('p')
 hint.className = 'settings-hint'
 hint.textContent = 'Facts distilled from captured sessions. These are separate from the named fields above.'
 memoryFacts.appendChild(hint)

 if (!factItems.length) {
  const empty = document.createElement('div')
  empty.className = 'settings-hint'
  empty.textContent = 'Nothing yet. Facts appear after Clippy summarizes enough of your work.'
  memoryFacts.appendChild(empty)
  return
 }

 const start = factPage * FACT_PAGE_SIZE
 const pageItems = factItems.slice(start, start + FACT_PAGE_SIZE)
 const list = document.createElement('div')
 list.className = 'privacy-list'
 for (const fact of pageItems) {
  const row = document.createElement('div')
  row.className = 'privacy-row'
  const text = document.createElement('div')
  text.className = 'privacy-row-text'
  const strong = document.createElement('strong')
  strong.textContent = fact.text
  text.appendChild(strong)
  if (fact.label) {
   const span = document.createElement('span')
   span.textContent = fact.label
   text.appendChild(span)
  }
  const remove = document.createElement('button')
  remove.type = 'button'
  remove.className = 'memory-remove'
  remove.textContent = 'Remove'
  remove.addEventListener('click', () => removeMemoryFact(fact.fact_id, remove))
  row.appendChild(text)
  row.appendChild(remove)
  list.appendChild(row)
 }
 memoryFacts.appendChild(list)

 if (factItems.length > FACT_PAGE_SIZE) {
  const pager = document.createElement('div')
  pager.className = 'memory-pager'
  const prev = document.createElement('button')
  prev.type = 'button'
  prev.className = 'memory-page-btn'
  prev.textContent = 'Previous'
  prev.disabled = factPage === 0
  prev.addEventListener('click', () => {
   factPage -= 1
   renderMemoryFacts(factItems)
  })
  const label = document.createElement('span')
  label.textContent = `${start + 1}–${start + pageItems.length} of ${factItems.length}`
  const next = document.createElement('button')
  next.type = 'button'
  next.className = 'memory-page-btn'
  next.textContent = 'Next'
  next.disabled = factPage >= pageCount - 1
  next.addEventListener('click', () => {
   factPage += 1
   renderMemoryFacts(factItems)
  })
  pager.appendChild(prev)
  pager.appendChild(label)
  pager.appendChild(next)
  memoryFacts.appendChild(pager)
 }
}

async function removeMemoryFact(factId, button) {
 const ok = await confirmDialog({
  title: 'Remove this memory?',
  message: 'Clippy will stop using this fact. It does not delete the sessions it came from.',
  confirmLabel: 'Remove',
  danger: true,
 })
 if (!ok) return
 button.disabled = true
 try {
  const profile = await window.clippy.deleteMemoryFact(factId)
  renderMemoryFacts(profile.facts)
 } catch (error) {
  setStatus(profileStatus, `Could not remove fact: ${error.message}`, 'error')
  button.disabled = false
 }
}

let watchSettings = { watch_mode: 'all', watch_apps: [] }

function choiceControl(input) {
 const wrap = document.createElement('span')
 wrap.className = input.type === 'radio' ? 'choice choice-radio' : 'choice choice-check'
 const face = document.createElement('span')
 face.className = 'choice-face'
 input.classList.add('choice-input')
 wrap.appendChild(input)
 wrap.appendChild(face)
 return wrap
}

function renderWatchSettings(settings, apps) {
 const modeList = document.getElementById('watch-mode-list')
 const appBox = document.getElementById('watch-apps')
 const appList = document.getElementById('watch-app-list')
 if (!modeList || !appBox || !appList) return
 watchSettings = {
  watch_mode: settings.watch_mode === 'selected' ? 'selected' : 'all',
  watch_apps: [...(settings.watch_apps || [])],
 }
 modeList.innerHTML = ''
 for (const mode of [
  ['all', 'Watch everything', 'Record every app, except windows you black out below.'],
  ['selected', 'Only selected apps', 'Record just the apps you check. Everything else is ignored.'],
 ]) {
  const row = document.createElement('label')
  row.className = 'privacy-row'
  const text = document.createElement('div')
  text.className = 'privacy-row-text'
  const strong = document.createElement('strong')
  strong.textContent = mode[1]
  const span = document.createElement('span')
  span.textContent = mode[2]
  text.appendChild(strong)
  text.appendChild(span)
  const input = document.createElement('input')
  input.type = 'radio'
  input.name = 'watch-mode'
  input.checked = watchSettings.watch_mode === mode[0]
  input.addEventListener('change', () => saveWatchMode(mode[0]))
  row.appendChild(text)
  row.appendChild(choiceControl(input))
  modeList.appendChild(row)
 }
 appBox.hidden = watchSettings.watch_mode !== 'selected'
 const seen = new Map()
 for (const app of apps || []) {
  const stem = processKey(app.process_name)
  if (stem && stem !== 'unknown') seen.set(stem, app.process_name)
 }
 for (const name of watchSettings.watch_apps) {
  if (!seen.has(processKey(name))) seen.set(processKey(name), name)
 }
 appList.innerHTML = ''
 if (!seen.size) {
  const empty = document.createElement('div')
  empty.className = 'settings-hint'
  empty.textContent = 'No apps recorded yet. Add a process name, then start capture.'
  appList.appendChild(empty)
  return
 }
 for (const [stem, label] of seen) {
  const row = document.createElement('label')
  row.className = 'privacy-row'
  const text = document.createElement('div')
  text.className = 'privacy-row-text'
  const strong = document.createElement('strong')
  strong.textContent = label
  text.appendChild(strong)
  const box = document.createElement('input')
  box.type = 'checkbox'
  box.checked = watchSettings.watch_apps.some((name) => processKey(name) === stem)
  box.addEventListener('change', () => toggleWatchApp(label, box.checked))
  row.appendChild(text)
  row.appendChild(choiceControl(box))
  appList.appendChild(row)
 }
}

async function loadWatchSettings() {
 const status = document.getElementById('watch-status')
 try {
  const [settings, seen] = await Promise.all([
   window.clippy.getCaptureSettings(),
   window.clippy.listSeenApps(),
  ])
  renderWatchSettings(settings, seen.apps || [])
  if (status) setStatus(status, '', null)
 } catch (error) {
  if (status) setStatus(status, `Could not load watch list: ${error.message}`, 'error')
 }
}

async function saveWatchMode(mode) {
 const status = document.getElementById('watch-status')
 try {
  const settings = await window.clippy.updateCaptureSettings({ watch_mode: mode })
  const seen = await window.clippy.listSeenApps()
  renderWatchSettings(settings, seen.apps || [])
  if (status) setStatus(status, mode === 'selected' ? 'Only selected apps are recorded.' : 'Every app is recorded.', 'ok')
 } catch (error) {
  if (status) setStatus(status, error.message, 'error')
 }
}

async function toggleWatchApp(name, enabled) {
 const apps = watchSettings.watch_apps.filter((item) => processKey(item) !== processKey(name))
 if (enabled) apps.push(name)
 const status = document.getElementById('watch-status')
 try {
  const settings = await window.clippy.updateCaptureSettings({ watch_apps: apps, watch_mode: 'selected' })
  const seen = await window.clippy.listSeenApps()
  renderWatchSettings(settings, seen.apps || [])
 } catch (error) {
  if (status) setStatus(status, error.message, 'error')
 }
}

export async function addWatchAppFromInput() {
 const input = document.getElementById('watch-app-input')
 const name = (input?.value || '').trim()
 if (!name) return
 await toggleWatchApp(name, true)
 if (input) input.value = ''
}

let loadedRetention = null

function renderDataStats(data) {
 const counts = {
  'stat-events': data?.events || 0,
  'stat-sessions': data?.sessions || 0,
  'stat-facts': data?.memory_facts || 0,
  'stat-shots': data?.screenshots || 0,
 }
 for (const [id, value] of Object.entries(counts)) {
  const node = document.getElementById(id)
  if (node) node.textContent = Number(value).toLocaleString()
 }
}

async function loadCaptureSettings() {
 const events = document.getElementById('retain-events')
 const shots = document.getElementById('retain-shots')
 const shotsMax = document.getElementById('retain-shots-max')
 const sessions = document.getElementById('retain-sessions')
 const model = document.getElementById('settings-model')
 try {
  const [settings, data, llm] = await Promise.all([
   window.clippy.getCaptureSettings(),
   window.clippy.getDataStats(),
   window.clippy.getLLMConfig(),
  ])
  loadedRetention = settings
  if (events) events.value = settings.raw_retention_days
  if (shots) shots.value = settings.screenshot_retention_days
  if (shotsMax) shotsMax.value = settings.screenshot_retention_max_days
  if (sessions) sessions.value = settings.summary_retention_days
  if (model) model.value = llm.chat_model || ''
  renderDataStats(data)
 } catch (error) {
  const status = document.getElementById('retain-status')
  if (status) setStatus(status, error.message, 'error')
 }
}

const RETENTION_LIMITS = {
 raw_retention_days: { min: 1, max: 30, label: 'Events' },
 screenshot_retention_days: { min: 1, max: 7, label: 'Screenshots' },
 screenshot_retention_max_days: { min: 1, max: 14, label: 'Longest screenshot stay' },
 summary_retention_days: { min: 1, max: 180, label: 'Session summaries' },
}

function readRetentionInput(id) {
 const value = Number(document.getElementById(id)?.value)
 return Number.isFinite(value) ? value : NaN
}

export async function saveRetention() {
 const status = document.getElementById('retain-status')
 const next = {
  raw_retention_days: readRetentionInput('retain-events'),
  screenshot_retention_days: readRetentionInput('retain-shots'),
  screenshot_retention_max_days: readRetentionInput('retain-shots-max'),
  summary_retention_days: readRetentionInput('retain-sessions'),
 }
 for (const [key, rule] of Object.entries(RETENTION_LIMITS)) {
  const value = next[key]
  if (!Number.isInteger(value) || value < rule.min || value > rule.max) {
   setStatus(status, `${rule.label} must be a whole number from ${rule.min} to ${rule.max}.`, 'error')
   return
  }
 }
 if (next.screenshot_retention_days > next.screenshot_retention_max_days) {
  setStatus(status, 'Ordinary screenshots cannot be kept longer than the longest screenshot stay.', 'error')
  return
 }
 if (next.screenshot_retention_max_days > next.raw_retention_days) {
  setStatus(status, 'A screenshot cannot be kept longer than the event it belongs to.', 'error')
  return
 }
 const shrinking = loadedRetention && (
  next.raw_retention_days < loadedRetention.raw_retention_days
  || next.summary_retention_days < loadedRetention.summary_retention_days
  || next.screenshot_retention_days < loadedRetention.screenshot_retention_days
 )
 if (shrinking) {
  const ok = await confirmDialog({
   title: 'Shorten how long data is kept?',
   message: 'Older events and sessions outside the new limit are deleted now.',
   confirmLabel: 'Delete older data',
   danger: true,
  })
  if (!ok) return
 }
 setStatus(status, 'Saving...', null)
 try {
  loadedRetention = await window.clippy.updateCaptureSettings(next)
  setStatus(status, 'Retention saved.', 'ok')
  await loadCaptureSettings()
 } catch (error) {
  setStatus(status, error.message, 'error')
 }
}

export async function saveModelSetting() {
 const status = document.getElementById('model-status')
 const chatModel = (document.getElementById('settings-model')?.value || '').trim()
 if (!chatModel) {
  setStatus(status, 'Enter a model tag.', 'error')
  return
 }
 setStatus(status, 'Saving...', null)
 try {
  await window.clippy.saveLLMConfig({ chat_model: chatModel })
  setStatus(status, 'Saved. Restart Clippy. If this model is not installed, setup downloads it first.', 'ok')
 } catch (error) {
  setStatus(status, error.message, 'error')
 }
}

export async function clearAllData() {
 const ok = await confirmDialog({
  title: 'Delete everything Clippy stored?',
  message: 'Events, screenshots, sessions, and memory facts are removed. Your display name stays.',
  confirmLabel: 'Delete everything',
  danger: true,
 })
 if (!ok) return
 const status = document.getElementById('data-clear-status')
 setStatus(status, 'Deleting...', null)
 try {
  await window.clippy.clearData(['all', 'memory'])
  setStatus(status, 'Stored data deleted.', 'ok')
  await loadCaptureSettings()
  const profile = await window.clippy.getProfile()
  renderMemoryFacts(profile.facts)
 } catch (error) {
  setStatus(status, error.message, 'error')
 }
}

export async function browseWorkspaceRoot() {
 const picked = await window.clippy.pickFolder()
 if (!picked || picked.canceled || !picked.path || !workspaceRootInput) return
 workspaceRootInput.value = picked.path
 await addWorkspaceRootFromInput()
}
