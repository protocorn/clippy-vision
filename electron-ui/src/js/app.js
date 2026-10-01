import {
  nameInput, nameSubmit, nameError, nameView, loadingView, loadingSub,
  appBrand, homeBtn, homeMenuBtn, settingsBtn, captureBtn, captureLabel,
  pauseBtn, pauseMenu, pauseMenuPanel, connectBtn,
  timelineSearch, timelineHint, timelineHintDismiss, timelineLoadMore,
  timelineDetailBack, updateBanner, updateBannerText, updateBannerLink,
  updateBannerDismiss, identityAddBtn, identityNewKey, identityNewVal,
  settingsName, settingsIntro, updateCheckToggle, mcpCopyBtn,
  workspaceRootAddBtn, workspaceRootInput, navMore, navMoreBtn, navMoreMenu, store,
} from './dom.js'
import { showView } from './utils.js'
import { submitName } from './onboarding.js'
import {
  openSettings, addIdentityField, saveProfile, saveUpdateCheck, copyMcpConfig,
  wireSettingsNav, addWorkspaceRootFromInput, browseWorkspaceRoot,
  addWatchAppFromInput, saveRetention, saveModelSetting, clearAllData,
} from './settings.js'
import { openTimeline, loadTimelineSessions, closeTimelineDetail } from './timeline.js'
import { captureRuntimeLabel, setCaptureUI } from './capture-ui.js'

const WAIT_COPY = [
  [/dependenc/i, 'Checking Python, Ollama, and the local model'],
  [/server|loading models/i, 'Starting the local server on this machine'],
  [/text model/i, 'Loading the text model into memory'],
]

function describeWait(sub) {
  const text = String(sub || '').trim()
  for (const [pattern, label] of WAIT_COPY) {
    if (pattern.test(text)) return label
  }
  return text || 'Opening Clippy Vision'
}

function on(el, event, handler) {
  if (!el) return
  el.addEventListener(event, handler)
}

export function showUpdateBanner({ version, url, name }) {
  if (!version || version === store.dismissedUpdateVersion) return
  updateBannerText.textContent = `A new version is available: ${name || version}`
  updateBannerLink.href = url || '#'
  updateBanner.hidden = false
}

function wireUi() {
  on(captureBtn, 'click', async () => {
    captureBtn.disabled = true
    try {
      setCaptureUI(await window.clippy.toggleCapture())
    } finally {
      if (!captureBtn.classList.contains('is-pending')) captureBtn.disabled = false
    }
  })

  function setPauseMenuOpen(open) {
    if (!pauseMenu || !pauseBtn || !pauseMenuPanel) return
    pauseMenu.classList.toggle('is-open', open)
    pauseBtn.setAttribute('aria-expanded', open ? 'true' : 'false')
    pauseMenuPanel.hidden = !open
  }

  on(pauseBtn, 'click', (e) => {
    e.stopPropagation()
    setPauseMenuOpen(pauseMenuPanel?.hidden !== false)
  })
  if (pauseMenuPanel) {
    pauseMenuPanel.addEventListener('click', async (e) => {
      const minutes = Number(e.target?.dataset?.pauseMinutes || 0)
      if (!minutes) return
      setPauseMenuOpen(false)
      try {
        setCaptureUI(await window.clippy.pauseCapture(minutes))
      } catch (error) {
        const { alertDialog } = await import('./dialogs.js')
        await alertDialog({ title: 'Could not pause', message: error.message || 'Pause failed.' })
      }
    })
  }

  on(connectBtn, 'click', () => openSettings('mcp'))

  on(updateBannerDismiss, 'click', () => {
    store.dismissedUpdateVersion = updateBannerText.dataset.version
    updateBanner.hidden = true
  })

  window.clippy.onReleaseAvailable((data) => {
    updateBannerText.dataset.version = data.version
    showUpdateBanner(data)
  })

  window.clippy.onCaptureStatusChanged((status) => {
    setCaptureUI(status)
    const label = document.getElementById('settings-runtime-label')
    const dot = document.querySelector('.settings-runtime-dot')
    if (label) label.textContent = captureRuntimeLabel(status)
    if (dot) dot.classList.toggle('is-off', !status?.active)
  })

  on(nameSubmit, 'click', submitName)
  on(nameInput, 'keydown', e => {
    if (e.key === 'Enter') submitName()
  })

  on(homeBtn, 'click', () => openTimeline())
  on(homeMenuBtn, 'click', () => {
    setNavMoreOpen(false)
    openTimeline()
  })
  on(appBrand, 'click', () => openTimeline())

  function setNavMoreOpen(open) {
    if (!navMore || !navMoreBtn || !navMoreMenu) return
    navMore.classList.toggle('is-open', open)
    navMoreBtn.setAttribute('aria-expanded', open ? 'true' : 'false')
    navMoreMenu.hidden = !open
  }

  on(navMoreBtn, 'click', (e) => {
    e.stopPropagation()
    setNavMoreOpen(navMoreMenu?.hidden !== false)
  })
  for (const item of [homeMenuBtn, settingsBtn]) {
    on(item, 'click', () => setNavMoreOpen(false))
  }
  document.addEventListener('click', (e) => {
    if (navMore && !navMore.contains(e.target)) setNavMoreOpen(false)
    if (pauseMenu && !pauseMenu.contains(e.target)) setPauseMenuOpen(false)
  })
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') setNavMoreOpen(false)
  })

  on(settingsBtn, 'click', () => openSettings())
  wireSettingsNav()
  on(identityAddBtn, 'click', addIdentityField)
  on(identityNewVal, 'keydown', e => {
    if (e.key === 'Enter') addIdentityField()
  })
  on(identityNewKey, 'keydown', e => {
    if (e.key === 'Enter') identityNewVal.focus()
  })
  on(settingsName, 'change', () => saveProfile({ statusText: 'Name saved.' }))
  on(settingsIntro, 'change', () => saveProfile({ statusText: 'Introduction saved.' }))
  on(updateCheckToggle, 'change', saveUpdateCheck)
  on(mcpCopyBtn, 'click', copyMcpConfig)
  on(workspaceRootAddBtn, 'click', addWorkspaceRootFromInput)
  on(document.getElementById('workspace-root-browse-btn'), 'click', browseWorkspaceRoot)
  on(document.getElementById('watch-app-add'), 'click', addWatchAppFromInput)
  on(document.getElementById('watch-app-input'), 'keydown', (e) => {
    if (e.key === 'Enter') addWatchAppFromInput()
  })
  on(document.getElementById('retain-save'), 'click', saveRetention)
  on(document.getElementById('model-save'), 'click', saveModelSetting)
  on(document.getElementById('model-restart'), 'click', () => window.clippy.relaunchApp())
  on(document.getElementById('data-clear-btn'), 'click', clearAllData)
  on(workspaceRootInput, 'keydown', e => {
    if (e.key === 'Enter') addWorkspaceRootFromInput()
  })

  on(timelineLoadMore, 'click', () => loadTimelineSessions({ reset: false }))
  on(timelineSearch, 'input', () => {
    clearTimeout(store.searchTimer)
    store.searchTimer = setTimeout(() => {
      store.timelineQuery = timelineSearch.value.trim()
      loadTimelineSessions({ reset: true })
    }, 250)
  })
  on(timelineHintDismiss, 'click', () => {
    try { localStorage.setItem('clippy.timelineHintDismissed', '1') } catch (_) { /* ignore */ }
    if (timelineHint) timelineHint.hidden = true
  })
  try {
    if (localStorage.getItem('clippy.timelineHintDismissed') === '1' && timelineHint) {
      timelineHint.hidden = true
    }
  } catch (_) { /* ignore */ }
  on(timelineDetailBack, 'click', closeTimelineDetail)
}

export async function init() {
  showView(loadingView)
  const loadingTitle = document.getElementById('loading-title')
  const loadingError = document.getElementById('loading-error')

  if (!window.clippy) {
    if (loadingError) {
      loadingError.textContent = 'Desktop bridge failed to load. Restart Clippy Vision.'
    }
    return
  }

  const LOADING_QUIPS = [
    "Clippy is stretching... don't watch, it's weird.",
    'Clippy is getting ready... no peeking.',
    "Clipping into your system. This won't hurt (much).",
    'Dusting off the models.',
    'Fit-checking the GPU.',
    'Almost clipped. Try not to blink.',
    'Loading brains into RAM. Clip holding strong.',
    'The paperclip has entered the chat.',
    'Getting ready. Still faster than your ex texting back.',
    'Sharpening the clip. Softening the paper.',
    "Clippy's paying more attention than you did in that meeting",
    'Unlocking third eye.',
  ]

  const quipOrder = LOADING_QUIPS
    .map((text, i) => ({ text, i, r: Math.random() }))
    .sort((a, b) => a.r - b.r)
    .map((x) => x.text)
  let quipIdx = 0
  if (loadingTitle) loadingTitle.textContent = quipOrder[0]

  const quipTimer = setInterval(() => {
    quipIdx = (quipIdx + 1) % quipOrder.length
    if (!loadingTitle) return
    loadingTitle.classList.add('is-fading')
    setTimeout(() => {
      loadingTitle.textContent = quipOrder[quipIdx]
      loadingTitle.classList.remove('is-fading')
    }, 350)
  }, 2800)

  window.clippy.onLoadingStatus((data) => {
    if (data?.sub && loadingSub) loadingSub.textContent = describeWait(data.sub)
  })
  if (loadingSub && loadingSub.textContent.trim() === 'Opening Clippy Vision') {
    loadingSub.textContent = 'Starting the local server on this machine'
  }

  try {
    await new Promise((resolve, reject) => {
      let settled = false
      const done = () => {
        if (settled) return
        settled = true
        resolve()
      }
      const fail = (error) => {
        if (settled) return
        settled = true
        reject(error)
      }
      const poll = () => {
        if (settled) return
        window.clippy.checkHealth().then((ok) => {
          if (ok) done()
          else setTimeout(poll, 1000)
        }).catch(() => setTimeout(poll, 1000))
      }
      if (typeof window.clippy.waitForApiReady === 'function') {
        window.clippy.waitForApiReady().then(done).catch(fail)
      }
      if (typeof window.clippy.onApiReady === 'function') window.clippy.onApiReady(done)
      poll()
    })
  } catch (error) {
    clearInterval(quipTimer)
    if (loadingError) {
      loadingError.textContent = `Startup failed: ${error.message || error}`
    }
    return
  }

  clearInterval(quipTimer)
  if (loadingTitle) loadingTitle.classList.remove('is-fading')

  try {
    const { name } = await window.clippy.getName()
    if (name && name.trim()) {
      const active = await window.clippy.getCaptureStatus()
      setCaptureUI(active)
      openTimeline()
    } else {
      showView(nameView)
      nameInput?.focus()
    }
  } catch (error) {
    showView(nameView)
    if (nameError) nameError.textContent = `Could not load profile: ${error.message}`
    nameInput?.focus()
  }
}

wireUi()
init().catch((error) => {
  const loadingError = document.getElementById('loading-error')
  if (loadingError) loadingError.textContent = `UI failed to start: ${error.message || error}`
})
