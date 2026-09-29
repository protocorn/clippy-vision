import { timelineView, timelineBody, timelineList, timelineLoadMore, timelineDetailWrap, timelineDetailScroll, timelineDetailContent, TIMELINE_PAGE_SIZE, store } from './dom.js'
import { showView, startOfDay, formatConversationTime } from './utils.js'
import { confirmDialog } from './dialogs.js'

const shotUrls = []

function releaseShotUrls() {
 while (shotUrls.length) {
  const url = shotUrls.pop()
  try { URL.revokeObjectURL(url) } catch (_) { /* ignore */ }
 }
}

const APP_ICON_COLORS = [
 '#c9a24a', '#5b8def', '#5cbf8a', '#d67a8a', '#9b7bff', '#e07a5f', '#4db6ac', '#81a1c1',
]

function friendlyAppName(processName) {
 const raw = String(processName || '').replace(/\.exe$/i, '').trim()
 if (!raw) return ''
 const key = raw.toLowerCase()
 const map = {
 chrome: 'Google Chrome',
 msedge: 'Microsoft Edge',
 firefox: 'Firefox',
 code: 'Visual Studio Code',
 cursor: 'Cursor',
 slack: 'Slack',
 discord: 'Discord',
 notepad: 'Notepad',
 explorer: 'File Explorer',
 windowsTerminal: 'Windows Terminal',
 windowsterminal: 'Windows Terminal',
 powershell: 'PowerShell',
}
 return map[key] || raw
}

function appIconLetter(processName, windowTitle) {
 const proc = String(processName || '').replace(/\.exe$/i, '').trim()
 if (proc) return proc.charAt(0).toUpperCase()
 const title = String(windowTitle || '').trim()
 return title ? title.charAt(0).toUpperCase() : '?'
}

function appIconColor(processName) {
 const key = String(processName || 'app').toLowerCase()
 if (key.includes('chrome')) return '#c9a24a'
 if (key.includes('code') || key.includes('cursor')) return '#5b8def'
 if (key.includes('edge')) return '#4db6ac'
 if (key.includes('firefox')) return '#e07a5f'
 if (key.includes('slack')) return '#9b7bff'
 if (key.includes('discord')) return '#7b83eb'
 let hash = 0
 for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) >>> 0
 return APP_ICON_COLORS[hash % APP_ICON_COLORS.length]
}

export function formatSessionClockRange(windowStart, windowEnd) {
 if (!windowStart && !windowEnd) return ''
 const start = new Date((windowStart || windowEnd) * 1000)
 const end = new Date((windowEnd || windowStart) * 1000)
 const timeOpts = { hour: 'numeric', minute: '2-digit' }
 const startTime = start.toLocaleTimeString('en-US', timeOpts)
 const endTime = end.toLocaleTimeString('en-US', timeOpts)
 // Prefer "8:25 to 8:32 PM" when both share the same AM/PM marker.
 const startParts = startTime.match(/^(.*)\s(AM|PM)$/i)
 const endParts = endTime.match(/^(.*)\s(AM|PM)$/i)
 if (startParts && endParts && startParts[2].toUpperCase() === endParts[2].toUpperCase()) {
  return `${startParts[1]} to ${endParts[1]} ${endParts[2]}`
 }
 return `${startTime} to ${endTime}`
}

export function formatSessionDetailWhen(windowStart, windowEnd) {
 if (!windowStart && !windowEnd) return ''
 const start = new Date((windowStart || windowEnd) * 1000)
 const datePart = start.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
 const clock = formatSessionClockRange(windowStart, windowEnd)
 return clock ? `${datePart} · ${clock}` : datePart
}

export function formatSessionTimeRange(windowStart, windowEnd) {
 return formatSessionDetailWhen(windowStart, windowEnd)
}

export function sessionCardTitle(session) {
 const task = (session.active_task || '').trim()
 if (task) return task
 const summary = (session.summary || '').trim()
 if (!summary) return 'Captured session'
 const firstLine = summary.split('\n').find((line) => line.trim()) || summary
 return firstLine.length > 120 ? `${firstLine.slice(0, 117)}...` : firstLine
}

export function groupTimelineSessions(items) {
 const groups = new Map()
 for (const item of items) {
  const ts = item.window_end || item.created_at || item.window_start
  const date = new Date((ts || 0) * 1000)
  const day = startOfDay(date)
  const key = `${day.getFullYear()}-${day.getMonth()}-${day.getDate()}`
  const label = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  if (!groups.has(key)) {
   groups.set(key, { key, label, order: -day.getTime(), items: [] })
  }
  groups.get(key).items.push(item)
 }
 return Array.from(groups.values()).sort((a, b) => a.order - b.order)
}

export function highlightTimelineSession(summaryId) {
 timelineList.querySelectorAll('.timeline-session').forEach((el) => {
  el.classList.toggle('active', el.dataset.id === summaryId)
 })
}

export function setTimelineDetailMode(open) {
 timelineDetailWrap.classList.toggle('is-open', open)
 timelineDetailScroll.hidden = !open
 timelineBody.classList.toggle('is-detail-only', open)
}

export function closeTimelineDetail() {
 store.timelineSelectedId = null
 setTimelineDetailMode(false)
 highlightTimelineSession(null)
}

export async function renderTimelineDetail(summaryId) {
 store.timelineSelectedId = summaryId
 highlightTimelineSession(summaryId)
 setTimelineDetailMode(true)
 releaseShotUrls()
 timelineDetailContent.innerHTML = '<div class="timeline-empty">Loading session…</div>'

 try {
  const data = await window.clippy.getSessionDetail(summaryId)
  timelineDetailContent.innerHTML = ''

  const head = document.createElement('div')
  head.className = 'timeline-detail-head'

  const title = document.createElement('h2')
  title.textContent = sessionCardTitle(data)

  const time = document.createElement('div')
  time.className = 'timeline-detail-time'
  time.textContent = formatSessionDetailWhen(data.window_start, data.window_end)

  head.appendChild(title)
  head.appendChild(time)

  if ((data.summary || '').trim()) {
   const summary = document.createElement('p')
   summary.className = 'timeline-detail-summary'
   summary.textContent = data.summary
   head.appendChild(summary)
  }

  timelineDetailContent.appendChild(head)
  timelineDetailContent.appendChild(createSessionActions(data))

  const listedEvents = data.events || []
  if (!listedEvents.length) {
   const empty = document.createElement('div')
   empty.className = 'timeline-empty'
   empty.textContent = 'No individual events stored for this session window.'
   timelineDetailContent.appendChild(empty)
   return
  }

  const rail = document.createElement('div')
  rail.className = 'timeline-event-rail'
  for (const event of listedEvents) {
   rail.appendChild(createTimelineEventCard(event))
  }
  timelineDetailContent.appendChild(rail)
  applyTimelineAppIcons(rail)
 } catch (error) {
  timelineDetailContent.innerHTML = `<div class="timeline-error">Could not load session: ${error.message}</div>`
 }
}

export function truncateEventText(text, maxLen = 800) {
 const trimmed = (text || '').trim()
 if (!trimmed) return ''
 return trimmed.length > maxLen ? `${trimmed.slice(0, maxLen - 3)}...` : trimmed
}

export function eventTextsOverlap(primary, secondary) {
 const a = (primary || '').trim().replace(/\s+/g, ' ').toLowerCase()
 const b = (secondary || '').trim().replace(/\s+/g, ' ').toLowerCase()
 if (!a || !b) return false
 if (a === b) return true
 if (a.length >= 40 && b.includes(a.slice(0, 40))) return true
 if (b.length >= 40 && a.includes(b.slice(0, 40))) return true
 return false
}

export function createTimelineEventCard(event) {
 const row = document.createElement('div')
 row.className = 'timeline-event'

 const marker = document.createElement('div')
 marker.className = 'timeline-event-marker'
 const stamp = document.createElement('time')
 stamp.className = 'timeline-event-time'
 stamp.textContent = formatConversationTime(event.timestamp)
 marker.appendChild(stamp)

 const card = document.createElement('article')
 card.className = 'timeline-event-card'

 const icon = document.createElement('div')
 icon.className = 'timeline-event-icon'
 icon.dataset.process = (event.process_name || '').trim()
 icon.textContent = appIconLetter(event.process_name, event.window_title)
 icon.style.background = appIconColor(event.process_name)

 const body = document.createElement('div')
 body.className = 'timeline-event-body'

 const windowTitle = (event.window_title || '').trim()
 const title = document.createElement('strong')
 title.className = 'timeline-event-window'
 title.textContent = windowTitle || (EVENT_TYPE_FALLBACK(event.event_type))

 const processName = (event.process_name || '').trim()
 const friendly = friendlyAppName(processName)
 if (processName || friendly) {
  const proc = document.createElement('div')
  proc.className = 'timeline-event-process'
  proc.textContent = friendly && processName && friendly.toLowerCase() !== processName.replace(/\.exe$/i, '').toLowerCase()
   ? `${processName} · ${friendly}`
   : (processName || friendly)
  body.appendChild(title)
  body.appendChild(proc)
 } else {
  body.appendChild(title)
 }

 const summary = truncateEventText(event.summary)
 const activity = truncateEventText(event.vision_activity)
 const primary = summary || activity
 if (primary) {
  const text = document.createElement('p')
  text.className = 'timeline-event-text'
  text.textContent = primary
  body.appendChild(text)
 }

 const ocr = truncateEventText(event.vision_ocr_text)
 if (ocr && !eventTextsOverlap(primary, ocr)) {
  const toggle = document.createElement('button')
  toggle.type = 'button'
  toggle.className = 'timeline-event-ocr-toggle'
  toggle.textContent = 'View captured text'
  const ocrEl = document.createElement('div')
  ocrEl.className = 'timeline-event-ocr'
  ocrEl.hidden = true
  ocrEl.textContent = ocr
  toggle.addEventListener('click', () => {
   const open = ocrEl.hidden
   ocrEl.hidden = !open
   toggle.textContent = open ? 'Hide captured text' : 'View captured text'
  })
  body.appendChild(toggle)
  body.appendChild(ocrEl)
 }

 const url = (event.active_url || '').trim()
 if (url) {
  const urlEl = document.createElement('div')
  urlEl.className = 'timeline-event-url'
  urlEl.textContent = url
  body.appendChild(urlEl)
 }

 if (event.screenshot_filename) {
  attachScreenshot(body, event.screenshot_filename)
 }

 const remove = document.createElement('button')
 remove.type = 'button'
 remove.className = 'timeline-event-delete'
 remove.textContent = 'Delete this moment'
 remove.addEventListener('click', () => deleteTimelineEvent(event.event_id))
 body.appendChild(remove)

 card.appendChild(icon)
 card.appendChild(body)
 row.appendChild(marker)
 row.appendChild(card)
 return row
}

async function applyTimelineAppIcons(root) {
 if (!root || !window.clippy?.getAppIcons) return
 const icons = root.querySelectorAll('.timeline-event-icon[data-process]')
 const names = []
 icons.forEach((el) => {
  const name = el.dataset.process
  if (name) names.push(name)
 })
 if (!names.length) return
 try {
  const map = await window.clippy.getAppIcons(names)
  icons.forEach((el) => {
   const key = String(el.dataset.process || '').trim().toLowerCase()
   const dataUrl = map && map[key]
   if (!dataUrl) return
   el.textContent = ''
   el.style.background = 'transparent'
   const img = document.createElement('img')
   img.className = 'timeline-event-icon-img'
   img.alt = ''
   img.draggable = false
   img.src = dataUrl
   el.appendChild(img)
  })
 } catch (_) { /* keep letter fallback */ }
}

function EVENT_TYPE_FALLBACK(eventType) {
 const labels = {
  context_change: 'App switch',
  screenshot_analysis: 'Screen capture',
  paste: 'Paste',
  clipboard_change: 'Clipboard',
  typing_burst: 'Typing',
 }
 return labels[eventType] || eventType || 'Event'
}

export function createTimelineSessionButton(session) {
 const btn = document.createElement('button')
 btn.type = 'button'
 btn.className = 'timeline-session'
 btn.dataset.id = session.summary_id
 if (session.summary_id === store.timelineSelectedId) btn.classList.add('active')

 const meta = document.createElement('span')
 meta.className = 'timeline-session-meta'
 meta.textContent = formatSessionClockRange(session.window_start, session.window_end)

 const title = document.createElement('span')
 title.className = 'timeline-session-title'
 title.textContent = sessionCardTitle(session)

 const badge = document.createElement('span')
 badge.className = 'timeline-session-badge'
 const count = session.event_count || 0
 badge.textContent = count === 1 ? '1 event' : `${count} events`

 btn.appendChild(meta)
 btn.appendChild(title)
 btn.appendChild(badge)
 btn.addEventListener('click', () => renderTimelineDetail(session.summary_id))
 return btn
}

export function renderTimelineList() {
 if (!store.timelineSessions.length) {
  const emptyText = store.timelineQuery
   ? 'No sessions match that search.'
   : 'No captured sessions yet. Turn on capture and Clippy will summarize your work here.'
  timelineList.innerHTML = `<div class="timeline-empty">${emptyText}</div>`
  timelineLoadMore.hidden = true
  return
 }

 const groups = groupTimelineSessions(store.timelineSessions)
 timelineList.innerHTML = ''

 for (const group of groups) {
  const section = document.createElement('div')
  section.className = 'timeline-section'

  const labelRow = document.createElement('div')
  labelRow.className = 'timeline-section-label'
  const label = document.createElement('span')
  label.textContent = group.label
  const erase = document.createElement('button')
  erase.type = 'button'
  erase.className = 'timeline-day-delete'
  erase.textContent = 'Delete day'
  const sample = group.items[0]
  const stamp = sample.window_end || sample.created_at || sample.window_start
  erase.addEventListener('click', () => deleteTimelineDay(stamp, group.label))
  labelRow.appendChild(label)
  labelRow.appendChild(erase)
  section.appendChild(labelRow)

  const itemsWrap = document.createElement('div')
  itemsWrap.className = 'timeline-section-items'
  for (const session of group.items) {
   itemsWrap.appendChild(createTimelineSessionButton(session))
  }
  section.appendChild(itemsWrap)
  timelineList.appendChild(section)
 }

 timelineLoadMore.hidden = store.timelineSessions.length >= store.timelineTotal
}

export async function loadTimelineSessions({ reset = false } = {}) {
 if (store.timelineLoading) return
 store.timelineLoading = true
 if (reset) {
  store.timelineSessions = []
  store.timelineOffset = 0
  store.timelineTotal = 0
  timelineList.innerHTML = '<div class="timeline-empty">Loading sessions…</div>'
  timelineLoadMore.hidden = true
 } else {
  timelineLoadMore.disabled = true
  timelineLoadMore.textContent = 'Loading…'
 }

 try {
  const data = await window.clippy.listTimelineSessions({
   limit: TIMELINE_PAGE_SIZE,
   offset: store.timelineOffset,
   q: store.timelineQuery || '',
  })
  store.timelineTotal = data.total || 0
  store.timelineSessions = reset
   ? (data.sessions || [])
   : store.timelineSessions.concat(data.sessions || [])
  store.timelineOffset = store.timelineSessions.length
  renderTimelineList()
 } catch (error) {
  if (reset) {
   timelineList.innerHTML = `<div class="timeline-error">Failed to load sessions: ${error.message}</div>`
  } else {
   const { alertDialog } = await import('./dialogs.js')
   await alertDialog({
    title: 'Could not load more sessions',
    message: error.message || 'Something went wrong.',
   })
  }
 } finally {
  store.timelineLoading = false
  timelineLoadMore.disabled = false
  timelineLoadMore.textContent = 'Load more'
 }
}

export async function openTimeline() {
 closeTimelineDetail()
 showView(timelineView)
 await loadTimelineSessions({ reset: true })
}

export function closeTimeline() {
 closeTimelineDetail()
}

function localDayBounds(tsSeconds) {
 const date = new Date((tsSeconds || 0) * 1000)
 const start = new Date(date.getFullYear(), date.getMonth(), date.getDate())
 const end = new Date(start)
 end.setDate(end.getDate() + 1)
 return { since: start.getTime() / 1000, until: end.getTime() / 1000 }
}

async function deleteTimelineDay(tsSeconds, label) {
 const ok = await confirmDialog({
  title: `Delete ${label}?`,
  message: 'Sessions and captured moments from that day are removed from this computer.',
  confirmLabel: 'Delete day',
  danger: true,
 })
 if (!ok) return
 const bounds = localDayBounds(tsSeconds)
 await window.clippy.deleteTimeSpan(bounds.since, bounds.until)
 closeTimelineDetail()
 await loadTimelineSessions({ reset: true })
}

async function deleteTimelineEvent(eventId) {
 const ok = await confirmDialog({
  title: 'Delete this moment?',
  message: 'This captured moment and its screenshot are removed. The rest of the session stays.',
  confirmLabel: 'Delete moment',
  danger: true,
 })
 if (!ok || !eventId) return
 await window.clippy.deleteEvent(eventId)
 if (store.timelineSelectedId) await renderTimelineDetail(store.timelineSelectedId)
}

function createSessionActions(data) {
 const wrap = document.createElement('div')
 wrap.className = 'timeline-detail-actions'

 if ((data.user_correction || '').trim()) {
  const note = document.createElement('p')
  note.className = 'timeline-correction'
  note.textContent = `You marked this summary wrong. ${data.user_correction}`
  wrap.appendChild(note)
 }

 const row = document.createElement('div')
 row.className = 'timeline-detail-action-row'

 const wrong = document.createElement('button')
 wrong.type = 'button'
 wrong.className = 'header-btn'
 wrong.textContent = 'This summary is wrong'
 wrong.addEventListener('click', () => showCorrectionForm(wrap, data.summary_id, data.user_correction))

 const erase = document.createElement('button')
 erase.type = 'button'
 erase.className = 'timeline-event-delete'
 erase.textContent = 'Delete session'
 erase.addEventListener('click', async () => {
  const ok = await confirmDialog({
   title: 'Delete this session?',
   message: 'The summary and the moments inside its time window are removed.',
   confirmLabel: 'Delete session',
   danger: true,
  })
  if (!ok) return
  await window.clippy.deleteSession(data.summary_id)
  closeTimelineDetail()
  await loadTimelineSessions({ reset: true })
 })

 row.appendChild(wrong)
 row.appendChild(erase)
 wrap.appendChild(row)
 return wrap
}

function showCorrectionForm(wrap, summaryId, current) {
 let form = wrap.querySelector('.timeline-correction-form')
 if (form) {
  form.hidden = false
  return
 }
 form = document.createElement('form')
 form.className = 'timeline-correction-form'
 const input = document.createElement('textarea')
 input.className = 'timeline-correction-input'
 input.placeholder = 'What should Clippy remember instead? Leave blank to just flag the summary.'
 input.value = current || ''
 const save = document.createElement('button')
 save.type = 'submit'
 save.className = 'header-btn'
 save.textContent = 'Save correction'
 form.appendChild(input)
 form.appendChild(save)
 form.addEventListener('submit', async (event) => {
  event.preventDefault()
  save.disabled = true
  try {
   await window.clippy.correctSession(summaryId, input.value)
   await renderTimelineDetail(summaryId)
  } catch (error) {
   save.disabled = false
   save.textContent = error.message || 'Could not save'
  }
 })
 wrap.appendChild(form)
}

async function attachScreenshot(body, filename) {
 try {
  const url = await window.clippy.screenshotObjectUrl(filename)
  shotUrls.push(url)
  const img = document.createElement('img')
  img.className = 'timeline-shot'
  img.alt = 'Captured screen'
  img.src = url
  img.addEventListener('click', () => openScreenshot(url))
  body.appendChild(img)
 } catch (_) { /* frame already expired */ }
}

function openScreenshot(url) {
 const existing = document.getElementById('timeline-shot-overlay')
 if (existing) existing.remove()
 const overlay = document.createElement('div')
 overlay.id = 'timeline-shot-overlay'
 overlay.className = 'timeline-shot-overlay'
 const img = document.createElement('img')
 img.src = url
 img.alt = 'Captured screen'
 overlay.appendChild(img)
 overlay.addEventListener('click', () => overlay.remove())
 document.body.appendChild(overlay)
}
