import {
  insightsView,
  insightsDayBody,
  insightsThreadsBody,
  insightsStatus,
  insightsDateLabel,
  insightsPrevDay,
  insightsNextDay,
  insightsRefreshBtn,
  insightsThreadsBtn,
  insightsThreadsPanel,
  insightsThreadsRefresh,
  store,
} from './dom.js'
import { showView } from './utils.js'
import { setMessageMarkdown } from './markdown.js'

function isoToday() {
  const d = new Date()
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function shiftDay(iso, delta) {
  const [y, m, d] = iso.split('-').map(Number)
  const dt = new Date(y, m - 1, d)
  dt.setDate(dt.getDate() + delta)
  const yy = dt.getFullYear()
  const mm = String(dt.getMonth() + 1).padStart(2, '0')
  const dd = String(dt.getDate()).padStart(2, '0')
  return `${yy}-${mm}-${dd}`
}

function formatDayLabel(iso) {
  const [y, m, d] = iso.split('-').map(Number)
  const dt = new Date(y, m - 1, d)
  const today = isoToday()
  const pretty = dt.toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
  if (iso === today) return `Today · ${pretty}`
  if (iso === shiftDay(today, -1)) return `Yesterday · ${pretty}`
  return pretty
}

function setStatus(text, kind = null) {
  if (!insightsStatus) return
  insightsStatus.textContent = text || ''
  insightsStatus.classList.remove('ok', 'error')
  if (kind) insightsStatus.classList.add(kind)
}

function setBusy(busy) {
  store.insightsBusy = busy
  if (insightsRefreshBtn) insightsRefreshBtn.disabled = busy
  if (insightsPrevDay) insightsPrevDay.disabled = busy
  if (insightsNextDay) insightsNextDay.disabled = busy
  if (insightsThreadsBtn) insightsThreadsBtn.disabled = busy
  if (insightsThreadsRefresh) insightsThreadsRefresh.disabled = busy
}

async function fetchDayCard(day, { force = false, generate = true } = {}) {
  if (force) {
    return window.clippy.generateInsightDay(day, true)
  }
  try {
    return await window.clippy.getInsightDay(day, false)
  } catch (_) {
    if (!generate) throw _
    return window.clippy.generateInsightDay(day, false)
  }
}

export async function loadInsightDay(day, { force = false } = {}) {
  const target = day || store.insightDay || isoToday()
  store.insightDay = target
  if (insightsDateLabel) insightsDateLabel.textContent = formatDayLabel(target)
  if (insightsNextDay) {
    insightsNextDay.disabled = target >= isoToday() || store.insightsBusy
  }

  setBusy(true)
  setStatus(force ? 'Regenerating day card…' : 'Loading day card…')
  if (insightsDayBody) {
    insightsDayBody.innerHTML = '<p class="insights-muted">Working…</p>'
  }

  try {
    const card = await fetchDayCard(target, { force, generate: true })
    if (insightsDayBody) {
      setMessageMarkdown(insightsDayBody, card.markdown || '_No card._')
    }
    const meta = []
    if (card.model) meta.push(card.model)
    if (card.capture_hint) meta.push(card.capture_hint)
    if (card.event_count != null) meta.push(`${card.event_count} events`)
    setStatus(meta.join(' · ') || 'Ready', 'ok')
  } catch (error) {
    if (insightsDayBody) {
      insightsDayBody.innerHTML = `<p class="insights-error">Could not load day card: ${
        error.message || error
      }</p>`
    }
    setStatus(error.message || 'Failed', 'error')
  } finally {
    setBusy(false)
    if (insightsNextDay) {
      insightsNextDay.disabled = store.insightDay >= isoToday()
    }
  }
}

export async function loadThreads({ force = false } = {}) {
  if (!insightsThreadsBody) return
  setBusy(true)
  setStatus(force ? 'Regenerating threads…' : 'Loading threads…')
  insightsThreadsBody.innerHTML = '<p class="insights-muted">Working…</p>'
  try {
    let card
    if (force) {
      card = await window.clippy.generateInsightThreads({ lookback_days: 7, force: true })
    } else {
      try {
        card = await window.clippy.getInsightThreads(7, false)
      } catch (_) {
        card = await window.clippy.generateInsightThreads({ lookback_days: 7, force: false })
      }
    }
    setMessageMarkdown(insightsThreadsBody, card.markdown || '_No threads._')
    const range = (card.dates || []).length
      ? `${card.dates[0]} → ${card.dates[card.dates.length - 1]}`
      : ''
    setStatus([card.model, range].filter(Boolean).join(' · ') || 'Threads ready', 'ok')
  } catch (error) {
    insightsThreadsBody.innerHTML = `<p class="insights-error">${error.message || error}</p>`
    setStatus(error.message || 'Failed', 'error')
  } finally {
    setBusy(false)
  }
}

export function setThreadsOpen(open) {
  store.insightsThreadsOpen = open
  if (insightsThreadsPanel) insightsThreadsPanel.hidden = !open
  if (insightsThreadsBtn) {
    insightsThreadsBtn.classList.toggle('is-active', open)
    insightsThreadsBtn.setAttribute('aria-expanded', open ? 'true' : 'false')
  }
  if (open) loadThreads({ force: false })
}

export function openInsights() {
  showView(insightsView)
  loadInsightDay(store.insightDay || isoToday())
}

export function wireInsights() {
  store.insightDay = store.insightDay || isoToday()
  store.insightsThreadsOpen = false
  setThreadsOpen(false)

  if (insightsPrevDay) {
    insightsPrevDay.addEventListener('click', () => {
      loadInsightDay(shiftDay(store.insightDay || isoToday(), -1))
    })
  }
  if (insightsNextDay) {
    insightsNextDay.addEventListener('click', () => {
      const next = shiftDay(store.insightDay || isoToday(), 1)
      if (next > isoToday()) return
      loadInsightDay(next)
    })
  }
  if (insightsRefreshBtn) {
    insightsRefreshBtn.addEventListener('click', () => {
      loadInsightDay(store.insightDay || isoToday(), { force: true })
    })
  }
  if (insightsThreadsBtn) {
    insightsThreadsBtn.addEventListener('click', () => {
      setThreadsOpen(!store.insightsThreadsOpen)
    })
  }
  if (insightsThreadsRefresh) {
    insightsThreadsRefresh.addEventListener('click', () => loadThreads({ force: true }))
  }
}
