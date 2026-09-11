import { messages, inputBox, sendBtn, welcomeInput, welcomeSend, welcomeCharCount, inputCharCount, nameInput, nameSubmit, nameError, nameView, chatView, chatMain, USER_MESSAGE_MAX_CHARS, store } from './dom.js'
import { showView, updateCharCount } from './utils.js'
import { syncDrawerLayout, refreshConversationList, highlightActiveConversation, closeDrawer } from './conversations.js'
import { setCaptureUI } from './capture-ui.js'

const markedApi = window.marked
if (markedApi?.setOptions) {
 markedApi.setOptions({
 gfm: true,
 breaks: true,
 })
}

export function parseAssistantContent(raw) {
 const text = raw || ''
 const match = text.match(/^\s*<thinking>\s*([\s\S]*?)\s*<\/thinking>\s*([\s\S]*)$/i)
 || text.match(/^\s*<think>\s*([\s\S]*?)\s*<\/think>\s*([\s\S]*)$/i)
 if (match) {
 return { thinking: match[1].trim(), content: match[2].trim() }
 }
 return { thinking: '', content: text }
}

export function renderMarkdown(text) {
 const source = text || ''
 if (!source) return ''
 try {
 const parse = markedApi?.parse || markedApi?.marked
 const html = typeof parse === 'function' ? parse(source) : source
 if (window.DOMPurify?.sanitize) {
 return window.DOMPurify.sanitize(html, {
 USE_PROFILES: { html: true },
 ADD_ATTR: ['target', 'rel'],
 })
 }
 return html
 } catch (_) {
 const escaped = source
 .replace(/&/g, '&amp;')
 .replace(/</g, '&lt;')
 .replace(/>/g, '&gt;')
 return `<p>${escaped}</p>`
 }
}

export function setMessageMarkdown(el, text) {
 const html = renderMarkdown(text)
 el.classList.add('md')
 el.innerHTML = html
 el.querySelectorAll('a[href]').forEach((a) => {
 const href = a.getAttribute('href') || ''
 if (/^https?:\/\//i.test(href)) {
 a.setAttribute('target', '_blank')
 a.setAttribute('rel', 'noopener noreferrer')
 } else {
 a.removeAttribute('href')
 }
 })
}

export function createBotReplyShell({ thinking = '', content = '', pending = false } = {}) {
 const root = document.createElement('div')
 root.className = 'message bot has-think'

 const thinkBlock = document.createElement('div')
 thinkBlock.className = 'think-block' + (pending ? ' is-pending' : '')

 const toggle = document.createElement('button')
 toggle.type = 'button'
 toggle.className = 'think-toggle'
 toggle.setAttribute('aria-expanded', 'false')
 toggle.innerHTML = `<span class="think-chevron">▾</span><span class="think-label">${pending ? 'Thinking' : 'Thought'}</span>`

 const thinkBody = document.createElement('div')
 thinkBody.className = 'think-body'
 thinkBody.textContent = thinking

 toggle.addEventListener('click', () => {
 const open = thinkBlock.classList.toggle('is-open')
 toggle.setAttribute('aria-expanded', open ? 'true' : 'false')
 })

 thinkBlock.appendChild(toggle)
 thinkBlock.appendChild(thinkBody)

 const messageBody = document.createElement('div')
 messageBody.className = 'message-body'
 let rawContent = content || ''
 if (rawContent) setMessageMarkdown(messageBody, rawContent)

 const copyBtn = document.createElement('button')
 copyBtn.type = 'button'
 copyBtn.className = 'copy-reply'
 copyBtn.textContent = 'Copy'
 copyBtn.style.display = rawContent ? '' : 'none'

 let copyResetTimer = null
 copyBtn.addEventListener('click', async () => {
 if (!rawContent) return
 try {
 await navigator.clipboard.writeText(rawContent)
 } catch {
 return
 }
 copyBtn.textContent = 'Copied'
 if (copyResetTimer) clearTimeout(copyResetTimer)
 copyResetTimer = setTimeout(() => {
 copyBtn.textContent = 'Copy'
 copyResetTimer = null
 }, 1500)
 })

 function updateCopyVisibility() {
 copyBtn.style.display = rawContent ? '' : 'none'
 }

 root.appendChild(thinkBlock)
 root.appendChild(messageBody)
 root.appendChild(copyBtn)

 if (!thinking && !pending) {
 thinkBlock.style.display = 'none'
 }

 const label = toggle.querySelector('.think-label')
 let failed = false

 return {
 root,
 appendThinking(delta) {
 thinkBlock.style.display = ''
 thinkBlock.classList.add('is-pending')
 thinkBody.textContent += delta
 if (thinkBlock.classList.contains('is-open')) {
 thinkBody.scrollTop = thinkBody.scrollHeight
 }
 },
 appendContent(delta) {
 rawContent += delta || ''
 setMessageMarkdown(messageBody, rawContent)
 updateCopyVisibility()
 messages.scrollTop = messages.scrollHeight
 },
 resetContent() {
 rawContent = ''
 messageBody.classList.remove('md')
 messageBody.innerHTML = ''
 updateCopyVisibility()
 },
 setStatus(text) {
 thinkBlock.style.display = ''
 thinkBlock.classList.add('is-pending')
 label.textContent = text || 'Thinking'
 },
 beginFail() {
 failed = true
 thinkBlock.classList.remove('is-pending')
 thinkBlock.style.display = 'none'
 },
 finalize() {
 if (failed) return
 thinkBlock.classList.remove('is-pending')
 label.textContent = thinkBody.textContent.trim() ? 'Thought' : 'Thinking'
 if (!thinkBody.textContent.trim()) {
 thinkBlock.style.display = 'none'
 }
 if (rawContent) setMessageMarkdown(messageBody, rawContent)
 updateCopyVisibility()
 },
 fail(message) {
 failed = true
 thinkBlock.classList.remove('is-pending')
 thinkBlock.style.display = 'none'
 rawContent = ''
 root.classList.remove('has-repair')
 messageBody.classList.remove('md')
 messageBody.textContent = message
 messageBody.style.color = 'var(--danger)'
 updateCopyVisibility()
 },
 failRepair(issue, rawMessage) {
 failed = true
 thinkBlock.classList.remove('is-pending')
 thinkBlock.style.display = 'none'
 rawContent = ''
 root.classList.add('has-repair')
 messageBody.classList.remove('md')
 messageBody.style.color = ''
 messageBody.replaceChildren()

 const card = document.createElement('div')
 card.className = 'runtime-issue'
 const title = document.createElement('div')
 title.className = 'runtime-issue-title'
 title.textContent = issue.title || 'Clippy is not ready'
 const detail = document.createElement('div')
 detail.className = 'runtime-issue-detail'
 detail.textContent = issue.detail || 'A required dependency is missing.'
 const raw = document.createElement('div')
 raw.className = 'runtime-issue-raw'
 raw.textContent = rawMessage || issue.reason || ''
 const fixBtn = document.createElement('button')
 fixBtn.type = 'button'
 fixBtn.className = 'runtime-issue-fix'
 fixBtn.textContent = 'Fix and continue'
 fixBtn.addEventListener('click', async () => {
 if (!window.clippy.fixRuntimeIssue) return
 fixBtn.disabled = true
 fixBtn.textContent = 'Opening setup…'
 try {
 await window.clippy.fixRuntimeIssue(issue)
 } catch (err) {
 fixBtn.disabled = false
 fixBtn.textContent = 'Fix and continue'
 raw.textContent = err.message || 'Could not open setup.'
 }
 })
 card.appendChild(title)
 card.appendChild(detail)
 if (raw.textContent) card.appendChild(raw)
 card.appendChild(fixBtn)
 messageBody.appendChild(card)
 updateCopyVisibility()
 },
 }
}


export function setChatMode(mode) {
 const welcome = mode === 'welcome'
 chatMain.classList.toggle('is-welcome', welcome)
 if (welcome) {
 welcomeInput.value = ''
 welcomeInput.style.height = 'auto'
 setTimeout(() => welcomeInput.focus(), 40)
 } else {
 setTimeout(() => inputBox.focus(), 40)
 }
}

export function resetConversation() {
 store.conversationId = crypto.randomUUID()
 messages.innerHTML = ''
 inputBox.value = ''
 inputBox.style.height = 'auto'
 setChatMode('welcome')
 highlightActiveConversation()
 closeDrawer()
 showView(chatView)
}

export function addMessage(text, role) {
 const uiRole = role === 'assistant' ? 'bot' : role
 if (uiRole === 'bot') {
 const parsed = parseAssistantContent(text)
 const shell = createBotReplyShell({
 thinking: parsed.thinking,
 content: parsed.content,
 pending: false,
 })
 messages.appendChild(shell.root)
 messages.scrollTop = messages.scrollHeight
 return shell.root
 }

 const div = document.createElement('div')
 div.className = 'message user'
 div.textContent = text
 messages.appendChild(div)
 messages.scrollTop = messages.scrollHeight
 return div
}

export function looksLikeRuntimeFailure(message) {
 const text = String(message || '').toLowerCase()
 return (
 text.includes('ollama')
 || text.includes('not found')
 || text.includes('not reachable')
 || text.includes('http 404')
 || text.includes('http 500')
 || text.includes('model requires more')
 || text.includes('error loading model')
 )
}

export async function handleChatFailure(shell, rawMessage) {
 const text = String(rawMessage || 'unknown')
 if (shell.beginFail) shell.beginFail()
 if (!looksLikeRuntimeFailure(text) || !window.clippy.checkRuntimeHealth) {
 shell.fail(`Error: ${text}`)
 return
 }
 let issue = null
 try {
 issue = await window.clippy.checkRuntimeHealth(text)
 } catch (_) { /* fall through to the raw error */ }
 if (issue && issue.ok === false) {
 shell.failRepair(issue, text)
 return
 }
 shell.fail(`Error: ${text}`)
}

export async function send(fromWelcome = false) {
 const source = fromWelcome ? welcomeInput : inputBox
 const text = source.value.trim()
 if (!text || store.isLoading) return
 if (text.length > USER_MESSAGE_MAX_CHARS) {
 updateCharCount(source, fromWelcome ? welcomeCharCount : inputCharCount)
 return
 }

 if (fromWelcome || chatMain.classList.contains('is-welcome')) {
 setChatMode('chat')
 }

 const requestConvId = store.conversationId
 store.isLoading = true
 addMessage(text, 'user')
 source.value = ''
 source.style.height = 'auto'
 sendBtn.disabled = true
 welcomeSend.disabled = true

 const shell = createBotReplyShell({ pending: true })
 messages.appendChild(shell.root)
 messages.scrollTop = messages.scrollHeight
 store.pendingReply = { conversationId: requestConvId, shell }

 try {
 await window.clippy.chatStream(text, requestConvId, (event) => {
 if (!store.pendingReply || store.pendingReply.conversationId !== requestConvId) return
 const s = store.pendingReply.shell
 if (event.type === 'status') s.setStatus(event.text || 'Thinking')
 else if (event.type === 'thinking') s.appendThinking(event.delta || '')
 else if (event.type === 'content') s.appendContent(event.delta || '')
 else if (event.type === 'reset_content') s.resetContent()
 else if (event.type === 'error') {
 handleChatFailure(s, event.message || 'unknown')
 }
 else if (event.type === 'done') s.finalize()
 })
 if (store.pendingReply && store.pendingReply.conversationId === requestConvId) {
 store.pendingReply.shell.finalize()
 }
 refreshConversationList()
 } catch (error) {
 if (store.pendingReply && store.pendingReply.conversationId === requestConvId) {
 await handleChatFailure(store.pendingReply.shell, error.message)
 }
 } finally {
 if (store.pendingReply && store.pendingReply.conversationId === requestConvId) {
 store.pendingReply = null
 }
 store.isLoading = false
 sendBtn.disabled = false
 welcomeSend.disabled = false
 if (store.conversationId === requestConvId) inputBox.focus()
 }
}

export async function submitName() {
 const name = nameInput.value.trim()
 if (!name) {
 nameError.textContent = 'Please enter your name.'
 return
 }

 nameSubmit.disabled = true
 nameError.textContent = ''

 try {
 await window.clippy.setName(name)
 showView(chatView)
 const active = await window.clippy.getCaptureStatus()
 setCaptureUI(active)
 setChatMode('welcome')
 syncDrawerLayout()
 } catch (error) {
 nameError.textContent = `Could not save name: ${error.message}`
 } finally {
 nameSubmit.disabled = false
 }
}
