import { nameView, chatView, loadingView, settingsView, timelineView, appShell, APP_PANELS, timelineBtn, settingsBtn, navMoreBtn, newChatBtn, welcomeInput, welcomeSend, inputBox, sendBtn, inputCharCount, welcomeCharCount, USER_MESSAGE_MAX_CHARS, CHAR_COUNT_SHOW_AT, wideLayoutMq, store } from './dom.js'

export function updateCharCount(inputEl, countEl) {
 const n = (inputEl.value || '').length
 countEl.textContent = `${n} / ${USER_MESSAGE_MAX_CHARS}`
 countEl.classList.toggle('is-visible', n >= CHAR_COUNT_SHOW_AT)
 countEl.classList.toggle('is-warn', n >= CHAR_COUNT_SHOW_AT && n < USER_MESSAGE_MAX_CHARS)
 countEl.classList.toggle('is-limit', n >= USER_MESSAGE_MAX_CHARS)
 if (inputEl === welcomeInput) {
 welcomeSend.disabled = store.isLoading || n > USER_MESSAGE_MAX_CHARS
 } else {
 sendBtn.disabled = store.isLoading || n > USER_MESSAGE_MAX_CHARS
 }
}

function syncNavActive(panel) {
 const onTimeline = panel === timelineView
 const onSettings = panel === settingsView
 if (timelineBtn) timelineBtn.classList.toggle('is-nav-active', onTimeline)
 if (settingsBtn) settingsBtn.classList.toggle('is-nav-active', onSettings)
 if (navMoreBtn) navMoreBtn.classList.toggle('is-nav-active', onTimeline || onSettings)

 if (newChatBtn) {
  const inChat = panel === chatView
  newChatBtn.classList.toggle('is-away', !inChat)
  newChatBtn.title = inChat
   ? 'Start a new chat'
   : 'Start a new chat (takes you back to Chat)'
 }
}

export function showView(view) {
 nameView.classList.remove('active')
 loadingView.classList.remove('active')
 if (appShell) appShell.classList.remove('active')
 for (const panel of APP_PANELS) {
  if (panel) panel.classList.remove('active')
 }

 if (view === nameView || view === loadingView) {
  view.classList.add('active')
  syncNavActive(null)
  return
 }

 // Chat / Settings / Timeline share the persistent top navbar.
 if (appShell) appShell.classList.add('active')
 const panel = APP_PANELS.includes(view) ? view : chatView
 if (panel) panel.classList.add('active')
 syncNavActive(panel)
}

export function isWideLayout() {
 return wideLayoutMq.matches
}

export function startOfDay(d) {
 return new Date(d.getFullYear(), d.getMonth(), d.getDate())
}

export function formatConversationTime(timestamp) {
 if (!timestamp) return ''

 const date = new Date(timestamp * 1000)
 const today = startOfDay(new Date())
 const thatDay = startOfDay(date)
 const diffDays = Math.round((today - thatDay) / 86400000)

 if (diffDays === 0) {
 return date.toLocaleTimeString('en-US', {
 hour: 'numeric',
 minute: '2-digit',
 })
 }

 const options = {
 month: 'short',
 day: 'numeric',
 hour: 'numeric',
 minute: '2-digit',
 }

 if (date.getFullYear() !== today.getFullYear()) {
 options.year = 'numeric'
 }

 return date.toLocaleString('en-US', options)
}

export function getTimeBucket(timestamp) {
 const date = new Date((timestamp || 0) * 1000)
 const today = startOfDay(new Date())
 const thatDay = startOfDay(date)
 const diffDays = Math.round((today - thatDay) / 86400000)

 if (diffDays <= 0) return { key: 'today', label: 'Today', order: 0 }
 if (diffDays === 1) return { key: 'yesterday', label: 'Yesterday', order: 1 }
 if (diffDays <= 3) return { key: '3days', label: '3 days ago', order: 2 }
 if (diffDays <= 7) return { key: 'week', label: '1 week ago', order: 3 }
 if (diffDays <= 30) return { key: 'last-month', label: 'Last month', order: 4 }

 const label = date.toLocaleString('en-US', { month: 'long', year: 'numeric' })
 const ym = date.getFullYear() * 12 + date.getMonth()
 // 5 = one past the last recent bucket order (0-4)
 // subtract from a fixed ceiling so newer months sort before older ones
 const YM_CEILING = 2100 * 12
 return {
 key: `ym-${ym}`,
 label,
 order: 5 + (YM_CEILING - ym),
 }
}

export function setStatus(el, text, kind) {
 el.textContent = text || ''
 el.classList.remove('ok', 'error')
 if (kind) el.classList.add(kind)
}
