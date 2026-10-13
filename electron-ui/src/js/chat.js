/**
 * In-app chat UI - restored from archive/in_app_chat/
 */
import { messages, inputBox, sendBtn, welcomeInput, welcomeSend, welcomeCharCount, inputCharCount, chatView, chatMain, USER_MESSAGE_MAX_CHARS, store } from './dom.js'
import { showView, updateCharCount } from './utils.js'
import { syncDrawerLayout, refreshConversationList, highlightActiveConversation, closeDrawer } from './conversations.js'

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
  
  // Escape text for safe fallback
  const escapeHtml = (str) => str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;')
  
  try {
    const parse = markedApi?.parse || markedApi?.marked
    if (typeof parse !== 'function') {
      return `<p>${escapeHtml(source)}</p>`
    }
    
    const html = parse(source)
    
    // SECURITY: Must sanitize with DOMPurify. Never return raw HTML.
    if (!window.DOMPurify?.sanitize) {
      console.warn('[chat] DOMPurify not loaded, falling back to escaped text')
      return `<p>${escapeHtml(source)}</p>`
    }
    
    return window.DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
      ADD_ATTR: ['target', 'rel'],
      FORBID_TAGS: ['script', 'iframe', 'object', 'embed'],
      FORBID_ATTR: ['onerror', 'onload', 'onclick'],
    })
  } catch (err) {
    console.error('[chat] renderMarkdown error:', err)
    return `<p>${escapeHtml(source)}</p>`
  }
}

export function setMessageMarkdown(el, text) {
  const html = renderMarkdown(text)
  el.classList.add('md')
  el.innerHTML = html
  
  // SECURITY: Strip javascript: URLs and other non-http(s) protocols
  el.querySelectorAll('a[href]').forEach((a) => {
    const href = a.getAttribute('href') || ''
    if (/^https?:\/\//i.test(href)) {
      a.setAttribute('target', '_blank')
      a.setAttribute('rel', 'noopener noreferrer')
    } else if (/^javascript:/i.test(href) || href.includes(':')) {
      // Remove dangerous or non-standard protocols
      a.removeAttribute('href')
      a.style.cursor = 'text'
      a.style.textDecoration = 'none'
    } else {
      // Relative or fragment links - remove for safety in Electron
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
      messageBody.classList.remove('md')
      messageBody.textContent = message
      messageBody.style.color = 'var(--danger)'
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

export function openChat() {
  if (!store.conversationId) {
    resetConversation()
  } else {
    showView(chatView)
    syncDrawerLayout()
  }
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

export function looksLikeOllamaError(message) {
  const text = String(message || '').toLowerCase()
  return (
    text.includes('ollama')
    || text.includes('not running')
    || text.includes('not reachable')
    || text.includes('http 404')
    || text.includes('http 500')
    || text.includes('http 503')
    || text.includes('model requires more')
    || text.includes('error loading model')
  )
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
    // Stream from /chat/stream endpoint
    const response = await fetch(await window.clippy.apiUrl('/chat/stream'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: text,
        conversation_id: requestConvId
      })
    })

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`)
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const data = line.slice(6).trim()
        if (!data) continue

        try {
          const event = JSON.parse(data)
          if (!store.pendingReply || store.pendingReply.conversationId !== requestConvId) continue

          const s = store.pendingReply.shell
          if (event.type === 'status') {
            s.setStatus(event.text || 'Thinking')
          } else if (event.type === 'content') {
            s.appendContent(event.delta || '')
          } else if (event.type === 'error') {
            // Handle Ollama unavailable errors inline
            if (event.ollama_unavailable) {
              s.fail('Ollama is not running. Please start Ollama and try again.')
            } else {
              s.fail(`Error: ${event.message || 'unknown'}`)
            }
          } else if (event.type === 'done') {
            store.conversationId = event.conversation_id || requestConvId
            s.finalize()
          }
        } catch (e) {
          console.error('[chat] SSE parse error:', e)
        }
      }
    }

    if (store.pendingReply && store.pendingReply.conversationId === requestConvId) {
      store.pendingReply = null
    }

  } catch (error) {
    console.error('[chat] stream error:', error)
    if (store.pendingReply && store.pendingReply.conversationId === requestConvId) {
      const s = store.pendingReply.shell
      if (looksLikeOllamaError(error.message)) {
        s.fail('Ollama is not running. Please start Ollama and try again.')
      } else {
        s.fail(`Error: ${error.message || 'unknown'}`)
      }
      store.pendingReply = null
    }
  } finally {
    store.isLoading = false
    sendBtn.disabled = false
    welcomeSend.disabled = false
  }
}

export function wireChat() {
  if (!sendBtn || !inputBox) return

  sendBtn.addEventListener('click', () => send(false))
  inputBox.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send(false)
    }
  })
  inputBox.addEventListener('input', () => {
    updateCharCount(inputBox, inputCharCount)
    inputBox.style.height = 'auto'
    inputBox.style.height = Math.min(inputBox.scrollHeight, 200) + 'px'
  })

  if (welcomeSend && welcomeInput) {
    welcomeSend.addEventListener('click', () => send(true))
    welcomeInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        send(true)
      }
    })
    welcomeInput.addEventListener('input', () => {
      updateCharCount(welcomeInput, welcomeCharCount)
      welcomeInput.style.height = 'auto'
      welcomeInput.style.height = Math.min(welcomeInput.scrollHeight, 120) + 'px'
    })
  }
}

// Initialize chat when module loads
wireChat()
