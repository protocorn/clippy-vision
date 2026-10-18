import { timelineHint } from './dom.js'

const DISMISSED_KEY = 'clippy.firstAnswerDismissed'

function readDismissed() {
 try {
  return localStorage.getItem(DISMISSED_KEY) === '1'
 } catch (_) {
  return false
 }
}

function writeDismissed() {
 try { localStorage.setItem(DISMISSED_KEY, '1') } catch (_) { /* ignore */ }
}

function shouldShow({ eligible, searching }) {
 return Boolean(eligible) && !readDismissed() && !searching
}

function anyClientConnected(clients) {
 return (clients || []).some((client) => client && client.connected)
}

export function hideFirstAnswerPrompt() {
 const el = document.getElementById('first-answer-prompt')
 if (el) el.hidden = true
}

async function copyQuestion(text, button) {
 const value = String(text || '').trim()
 if (!value) return
 const original = button.textContent
 try {
  if (navigator.clipboard && navigator.clipboard.writeText) {
   await navigator.clipboard.writeText(value)
  } else {
   const area = document.createElement('textarea')
   area.value = value
   area.setAttribute('readonly', '')
   area.style.position = 'fixed'
   area.style.left = '-9999px'
   document.body.appendChild(area)
   area.select()
   document.execCommand('copy')
   area.remove()
  }
  button.textContent = 'Copied'
  setTimeout(() => { button.textContent = original }, 1600)
 } catch (_) {
  button.textContent = 'Copy failed'
  setTimeout(() => { button.textContent = original }, 1600)
 }
}

export async function refreshFirstAnswerPrompt({ searching = false } = {}) {
 const root = document.getElementById('first-answer-prompt')
 if (!root) return
 if (readDismissed() || searching) {
  root.hidden = true
  return
 }
 if (!window.clippy?.getFirstAnswerPrompt) {
  root.hidden = true
  return
 }

 let prompt
 try {
  prompt = await window.clippy.getFirstAnswerPrompt()
 } catch (_) {
  root.hidden = true
  return
 }

 if (!shouldShow({ eligible: prompt?.eligible, searching })) {
  root.hidden = true
  return
 }

 const questionEl = document.getElementById('first-answer-question')
 const copyBtn = document.getElementById('first-answer-copy')
 const connectBtn = document.getElementById('first-answer-connect')
 const question = String(prompt.question || 'What was I working on just now?')
 if (questionEl) questionEl.textContent = question
 if (copyBtn) copyBtn.dataset.question = question

 let connected = false
 try {
  const status = await window.clippy.mcp?.listClients?.()
  connected = anyClientConnected(status?.clients)
 } catch (_) {
  connected = false
 }
 if (connectBtn) connectBtn.hidden = connected

 root.hidden = false
 if (timelineHint) timelineHint.hidden = true
}

export function wireFirstAnswerPrompt() {
 const copyBtn = document.getElementById('first-answer-copy')
 const dismissBtn = document.getElementById('first-answer-dismiss')
 const connectBtn = document.getElementById('first-answer-connect')
 copyBtn?.addEventListener('click', () => copyQuestion(copyBtn.dataset.question || '', copyBtn))
 dismissBtn?.addEventListener('click', () => {
  writeDismissed()
  hideFirstAnswerPrompt()
 })
 connectBtn?.addEventListener('click', () => {
  document.getElementById('connect-btn')?.click()
 })
}
