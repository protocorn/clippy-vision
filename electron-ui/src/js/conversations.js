import { drawer, drawerBackdrop, convList, convSearch, convSearchHint, chatView, messages, wideLayoutMq, store } from './dom.js'
import { isWideLayout, getTimeBucket, formatConversationTime } from './utils.js'

// NOTE: do not static-import chat.js here — chat.js imports this file (cycle).
// Use dynamic import() inside functions that need chat helpers.

export function openDrawer() {
 if (isWideLayout()) {
 refreshConversationList()
 return
 }
 drawer.classList.add('open')
 drawerBackdrop.classList.add('open')
 refreshConversationList()
 setTimeout(() => convSearch.focus(), 50)
}

export function closeDrawer() {
 if (isWideLayout()) return
 drawer.classList.remove('open')
 drawerBackdrop.classList.remove('open')
}

export function syncDrawerLayout() {
 if (isWideLayout()) {
 drawer.classList.remove('open')
 drawerBackdrop.classList.remove('open')
 if (chatView.classList.contains('active')) {
 refreshConversationList()
 }
 } else {
 drawer.classList.remove('open')
 drawerBackdrop.classList.remove('open')
 }
}

export function highlightActiveConversation() {
 convList.querySelectorAll('.conv-item-row').forEach(el => {
 el.classList.toggle('active', el.dataset.id === store.conversationId)
 })
}

export function groupConversationsByTime(items) {
 const groups = new Map()

 for (const item of items) {
 const bucket = getTimeBucket(item.last_timestamp)
 if (!groups.has(bucket.key)) {
 groups.set(bucket.key, { ...bucket, items: [] })
 }
 groups.get(bucket.key).items.push(item)
 }

 return Array.from(groups.values()).sort((a, b) => {
 if (a.order !== b.order) return a.order - b.order
 return 0
 })
}

export async function deleteConversation(id, title) {
 const label = title || 'this chat'
 const ok = window.confirm(`Delete "${label}"? This cannot be undone.`)
 if (!ok) return

 try {
 await window.clippy.deleteConversation(id)
 if (store.pendingReply && store.pendingReply.conversationId === id) {
 store.pendingReply = null
 }
 if (store.conversationId === id) {
 const { resetConversation } = await import('./chat.js')
 resetConversation()
 }
 await refreshConversationList()
 } catch (error) {
 window.alert(`Could not delete chat: ${error.message}`)
 }
}

export function createConvButton(c) {
 const row = document.createElement('div')
 row.className = 'conv-item-row'
 row.dataset.id = c.conversation_id
 if (c.conversation_id === store.conversationId) row.classList.add('active')

 const btn = document.createElement('button')
 btn.type = 'button'
 btn.className = 'conv-item'

 const title = c.title || c.conversation_id

 const titleEl = document.createElement('span')
 titleEl.className = 'conv-item-title'
 titleEl.textContent = title

 const timeEl = document.createElement('span')
 timeEl.className = 'conv-item-time'
 timeEl.textContent = formatConversationTime(c.last_timestamp)

 btn.appendChild(titleEl)
 btn.appendChild(timeEl)
 btn.title = title
 btn.addEventListener('click', () => loadConversation(c.conversation_id))

 const del = document.createElement('button')
 del.type = 'button'
 del.className = 'conv-item-delete'
 del.title = 'Delete chat'
 del.setAttribute('aria-label', 'Delete chat')
 del.textContent = 'x'
 del.addEventListener('click', (e) => {
 e.stopPropagation()
 deleteConversation(c.conversation_id, c.title || c.conversation_id)
 })

 row.appendChild(btn)
 row.appendChild(del)
 return row
}

export function renderGroupedConversations(items) {
 if (!items.length) {
 convList.innerHTML = '<div class="conv-empty">No conversations yet</div>'
 return
 }

 const groups = groupConversationsByTime(items)
 const openByDefault = new Set(['today', 'yesterday', '3days'])
 convList.innerHTML = ''

 for (const group of groups) {
 const section = document.createElement('div')
 section.className = 'conv-section'
 if (!openByDefault.has(group.key)) {
 section.classList.add('collapsed')
 }

 const label = document.createElement('button')
 label.type = 'button'
 label.className = 'conv-section-label'
 label.innerHTML =
 `<span class="conv-section-chevron">▾</span>` +
 `<span>${group.label}</span>` +
 `<span class="conv-section-count">${group.items.length}</span>`
 label.addEventListener('click', () => {
 section.classList.toggle('collapsed')
 })
 section.appendChild(label)

 const itemsWrap = document.createElement('div')
 itemsWrap.className = 'conv-section-items'
 for (const c of group.items) {
 itemsWrap.appendChild(createConvButton(c))
 }
 section.appendChild(itemsWrap)

 convList.appendChild(section)
 }
}

export function renderSearchResults(items, query) {
 convList.innerHTML = ''
 if (!items.length) {
 convList.innerHTML = `<div class="conv-empty">No chats match "${query}"div>`
 return
 }

 const section = document.createElement('div')
 section.className = 'conv-section'

 const label = document.createElement('div')
 label.className = 'conv-section-label'
 label.style.cursor = 'default'
 label.innerHTML =
 `<span>Best matches</span>` +
 `<span class="conv-section-count">${items.length}</span>`
 section.appendChild(label)

 const itemsWrap = document.createElement('div')
 itemsWrap.className = 'conv-section-items'
 for (const c of items) {
 itemsWrap.appendChild(createConvButton(c))
 }
 section.appendChild(itemsWrap)
 convList.appendChild(section)
}

export async function refreshConversationList() {
 const q = (convSearch.value || '').trim()
 store.activeSearchQuery = q
 const seq = ++store.searchSeq

 if (!q) {
 convSearchHint.textContent = ''
 try {
 const data = await window.clippy.listConversations()
 if (seq !== store.searchSeq) return
 const items = (data.conversations || []).slice().sort(
 (a, b) => (b.last_timestamp || 0) - (a.last_timestamp || 0)
 )
 renderGroupedConversations(items)
 } catch (error) {
 if (seq !== store.searchSeq) return
 convList.innerHTML = `<div class="conv-empty">Failed to load: ${error.message}</div>`
 }
 return
 }

 convSearchHint.textContent = 'Searching...'
 try {
 const data = await window.clippy.searchConversations(q)
 if (seq !== store.searchSeq) return
 const items = data.conversations || []
 renderSearchResults(items, q)
 } catch (error) {
 if (seq !== store.searchSeq) return
 convSearchHint.textContent = ''
 convList.innerHTML = `<div class="conv-empty">Search failed: ${error.message}</div>`
 }
}

export function scheduleConversationSearch() {
 clearTimeout(store.searchTimer)
 store.searchTimer = setTimeout(() => refreshConversationList(), 280)
}

export async function loadConversation(id) {
 try {
 const { setChatMode, addMessage } = await import('./chat.js')
 const data = await window.clippy.getConversation(id)
 store.conversationId = id
 messages.innerHTML = ''
 const msgs = data.messages || []
 if (!msgs.length && !(store.pendingReply && store.pendingReply.conversationId === id)) {
 setChatMode('welcome')
 } else {
 setChatMode('chat')
 for (const m of msgs) {
 addMessage(m.content, m.role)
 }
 // Re-attach in-flight reply if user left and came back mid-stream
 if (store.pendingReply && store.pendingReply.conversationId === id) {
 const last = msgs[msgs.length - 1]
 if (last && (last.role === 'assistant' || last.role === 'bot')) {
 store.pendingReply = null
 } else {
 messages.appendChild(store.pendingReply.shell.root)
 messages.scrollTop = messages.scrollHeight
 }
 }
 }
 highlightActiveConversation()
 closeDrawer()
 } catch (error) {
 const { setChatMode, addMessage } = await import('./chat.js')
 setChatMode('chat')
 addMessage(`Error loading chat: ${error.message}`, 'bot')
 }
}
