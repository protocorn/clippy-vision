import {
 settingsView, chatView, chatMain, welcomeInput, inputBox, settingsName, settingsIntro,
 identityFields, identityNewKey, identityNewVal, identityAddBtn, profileStatus,
 updateCheckToggle, updatesStatus, aboutVersion, aboutPlatform, aboutModel, aboutMemory,
 aboutBadge, privacyList, privacyStatus, privacyCount, settingsUserLabel, settingsRuntimeLabel,
 settingsRuntimeDot, settingsNavItems, settingsPanels, mcpReadyLabel, mcpReadyDetail,
 mcpConfigPreview, mcpClientList, mcpCopyBtn, mcpStatus, MCP_CLIENT_ICONS, PRIVACY_TARGET_ICONS,
 store, updateBanner,
} from './dom.js'
import { showView, setStatus } from './utils.js'
import { closeDrawer } from './conversations.js'

let profileSaveInFlight = false

export function showSettingsPanel(panelId) {
 const id = panelId || 'profile'
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
 empty.textContent = 'Nothing stored yet - Clippy will fill this in as you chat, or add fields below.'
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
 const ok = window.confirm(`Remove “${key}” from your memory profile?`)
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
 let captureOn = false
 try {
  if (window.clippy?.getCaptureStatus) {
   captureOn = !!(await window.clippy.getCaptureStatus())
  }
 } catch (_) { /* ignore */ }
 settingsRuntimeLabel.textContent = captureOn
  ? 'Running locally · capture on'
  : 'Running locally · capture off'
 if (settingsRuntimeDot) settingsRuntimeDot.classList.toggle('is-off', !captureOn)
}

export async function loadSettings() {
 showSettingsPanel('profile')
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
  setStatus(profileStatus, '', null)
 } catch (error) {
  setStatus(profileStatus, `Could not load profile: ${error.message}`, 'error')
 }
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

export async function openSettings() {
 closeDrawer()
 showView(settingsView)
 await loadSettings()
}

export function closeSettings() {
 showView(chatView)
 if (chatMain.classList.contains('is-welcome')) welcomeInput.focus()
 else inputBox.focus()
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
