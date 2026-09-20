import {
  nameInput, nameSubmit, nameError, nameView, loadingView, loadingSub,
  appBrand, settingsBtn, captureBtn, captureLabel, timelineBtn, timelineLoadMore,
  timelineDetailBack, updateBanner, updateBannerText, updateBannerLink,
  updateBannerDismiss, identityAddBtn, identityNewKey, identityNewVal,
  settingsName, settingsIntro, updateCheckToggle, mcpCopyBtn,
  workspaceRootAddBtn, workspaceRootInput, navMore, navMoreBtn, navMoreMenu, store,
  insightsView,
} from './dom.js'
import { showView } from './utils.js'
import { submitName } from './onboarding.js'
import {
  openSettings, addIdentityField, saveProfile, saveUpdateCheck, copyMcpConfig,
  wireSettingsNav, addWorkspaceRootFromInput,
} from './settings.js'
import { openTimeline, loadTimelineSessions, closeTimelineDetail } from './timeline.js'
import { setCaptureUI } from './capture-ui.js'
import { openInsights, wireInsights } from './insights.js'

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
      await window.clippy.toggleCapture()
    } finally {
      captureBtn.disabled = false
    }
  })

  on(updateBannerDismiss, 'click', () => {
    store.dismissedUpdateVersion = updateBannerText.dataset.version
    updateBanner.hidden = true
  })

  window.clippy.onReleaseAvailable((data) => {
    updateBannerText.dataset.version = data.version
    showUpdateBanner(data)
  })

  window.clippy.onCaptureStatusChanged((active) => {
    setCaptureUI(active)
    const label = document.getElementById('settings-runtime-label')
    const dot = document.querySelector('.settings-runtime-dot')
    if (label) {
      label.textContent = active
        ? 'Running locally · capture on'
        : 'Running locally · capture off'
    }
    if (dot) dot.classList.toggle('is-off', !active)
  })

  on(nameSubmit, 'click', submitName)
  on(nameInput, 'keydown', e => {
    if (e.key === 'Enter') submitName()
  })

  on(appBrand, 'click', () => openInsights())

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
  for (const item of [timelineBtn, settingsBtn]) {
    on(item, 'click', () => setNavMoreOpen(false))
  }
  document.addEventListener('click', (e) => {
    if (!navMore || navMore.contains(e.target)) return
    setNavMoreOpen(false)
  })
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') setNavMoreOpen(false)
  })

  on(settingsBtn, 'click', openSettings)
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
  on(workspaceRootInput, 'keydown', e => {
    if (e.key === 'Enter') addWorkspaceRootFromInput()
  })

  on(timelineBtn, 'click', openTimeline)
  on(timelineLoadMore, 'click', () => loadTimelineSessions({ reset: false }))
  on(timelineDetailBack, 'click', closeTimelineDetail)

  wireInsights()
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
    if (data?.sub && loadingSub) loadingSub.textContent = data.sub
  })

  try {
    if (typeof window.clippy.waitForApiReady === 'function') {
      await window.clippy.waitForApiReady()
    } else {
      await new Promise((resolve) => {
        let settled = false
        const done = () => {
          if (settled) return
          settled = true
          resolve()
        }
        const poll = () => {
          if (settled) return
          window.clippy.checkHealth().then((ok) => {
            if (ok) done()
            else setTimeout(poll, 1000)
          }).catch(() => setTimeout(poll, 1000))
        }
        poll()
        window.clippy.onApiReady(done)
      })
    }
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
      showView(insightsView)
      const active = await window.clippy.getCaptureStatus()
      setCaptureUI(active)
      openInsights()
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
