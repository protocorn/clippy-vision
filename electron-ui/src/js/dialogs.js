/**
 * In-app confirm/alert dialogs.
 *
 * Electron on Windows loses renderer input focus after window.confirm / window.alert
 * until the BrowserWindow is deactivated and reactivated (minimize/restore).
 * Keep dialogs inside Chromium so focus never leaves the page.
 */

let activeDialog = null

function ensureHost() {
  let host = document.getElementById('app-dialog-host')
  if (host) return host
  host = document.createElement('div')
  host.id = 'app-dialog-host'
  document.body.appendChild(host)
  return host
}

function closeActive() {
  if (!activeDialog) return
  const { root, onKey } = activeDialog
  document.removeEventListener('keydown', onKey, true)
  root.remove()
  activeDialog = null
}

function openDialog({ title, message, detail, confirmLabel, cancelLabel, danger }) {
  closeActive()

  return new Promise((resolve) => {
    const host = ensureHost()
    const root = document.createElement('div')
    root.className = 'app-dialog-overlay'
    root.setAttribute('role', 'presentation')

    const panel = document.createElement('div')
    panel.className = 'app-dialog'
    panel.setAttribute('role', 'alertdialog')
    panel.setAttribute('aria-modal', 'true')
    panel.setAttribute('aria-labelledby', 'app-dialog-title')
    panel.setAttribute('aria-describedby', 'app-dialog-message')

    const titleEl = document.createElement('h2')
    titleEl.id = 'app-dialog-title'
    titleEl.className = 'app-dialog-title'
    titleEl.textContent = title || 'Confirm'

    const messageEl = document.createElement('p')
    messageEl.id = 'app-dialog-message'
    messageEl.className = 'app-dialog-message'
    messageEl.textContent = message || ''

    panel.appendChild(titleEl)
    panel.appendChild(messageEl)

    if (detail) {
      const detailEl = document.createElement('p')
      detailEl.className = 'app-dialog-detail'
      detailEl.textContent = detail
      panel.appendChild(detailEl)
    }

    const actions = document.createElement('div')
    actions.className = 'app-dialog-actions'

    const finish = (value) => {
      closeActive()
      resolve(value)
    }

    if (cancelLabel !== null) {
      const cancelBtn = document.createElement('button')
      cancelBtn.type = 'button'
      cancelBtn.className = 'app-dialog-btn app-dialog-btn-secondary'
      cancelBtn.textContent = cancelLabel || 'Cancel'
      cancelBtn.addEventListener('click', () => finish(false))
      actions.appendChild(cancelBtn)
    }

    const confirmBtn = document.createElement('button')
    confirmBtn.type = 'button'
    confirmBtn.className = danger
      ? 'app-dialog-btn app-dialog-btn-danger'
      : 'app-dialog-btn app-dialog-btn-primary'
    confirmBtn.textContent = confirmLabel || 'OK'
    confirmBtn.addEventListener('click', () => finish(true))
    actions.appendChild(confirmBtn)

    panel.appendChild(actions)
    root.appendChild(panel)

    root.addEventListener('click', (e) => {
      if (e.target === root && cancelLabel !== null) finish(false)
    })

    const onKey = (e) => {
      if (e.key === 'Escape' && cancelLabel !== null) {
        e.preventDefault()
        finish(false)
      } else if (e.key === 'Enter') {
        e.preventDefault()
        finish(true)
      }
    }

    activeDialog = { root, onKey }
    document.addEventListener('keydown', onKey, true)
    host.appendChild(root)
    confirmBtn.focus()
  })
}

export function confirmDialog({
  title = 'Confirm',
  message = '',
  detail = '',
  confirmLabel = 'OK',
  cancelLabel = 'Cancel',
  danger = false,
} = {}) {
  return openDialog({ title, message, detail, confirmLabel, cancelLabel, danger })
}

export function alertDialog({
  title = 'Clippy Vision',
  message = '',
  detail = '',
  confirmLabel = 'OK',
} = {}) {
  return openDialog({
    title,
    message,
    detail,
    confirmLabel,
    cancelLabel: null,
    danger: false,
  }).then(() => undefined)
}
