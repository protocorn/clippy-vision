import { captureBtn, captureLabel, pauseBtn } from './dom.js'

let pausedUntil = 0
let tick = null

function formatResume(until) {
  return new Date(until).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

export function captureIsActive(status) {
  if (status && typeof status === 'object') return !!status.active
  return !!status
}

export function capturePending(status) {
  const pending = status && typeof status === 'object' ? status.pending : ''
  return pending === 'starting' || pending === 'stopping' ? pending : ''
}

export function captureRuntimeLabel(status) {
  const pending = capturePending(status)
  if (pending === 'starting') return 'Running locally · starting capture'
  if (pending === 'stopping') return 'Running locally · stopping capture'
  if (captureIsActive(status)) return 'Running locally · capture on'
  const until = status && typeof status === 'object' ? Number(status.pausedUntil || 0) : 0
  if (until > Date.now()) return 'Running locally · capture paused'
  return 'Running locally · capture off'
}

export function setCaptureUI(status) {
  if (!captureBtn || !captureLabel) return
  const active = captureIsActive(status)
  const pending = capturePending(status)
  pausedUntil = status && typeof status === 'object' ? Number(status.pausedUntil || 0) : 0
  const paused = !active && !pending && pausedUntil > Date.now()

  captureBtn.classList.toggle('active', active || pending === 'stopping')
  captureBtn.classList.toggle('is-paused', paused)
  captureBtn.classList.toggle('is-pending', !!pending)
  captureBtn.disabled = !!pending
  captureBtn.title = pending === 'starting'
    ? 'Starting capture'
    : pending === 'stopping'
      ? 'Stopping capture'
      : 'Toggle screen capture'
  if (pending === 'starting') captureLabel.textContent = 'Starting Capture'
  else if (pending === 'stopping') captureLabel.textContent = 'Stopping Capture'
  else if (active) captureLabel.textContent = 'Stop Capture'
  else if (paused) captureLabel.textContent = `Resume · ${formatResume(pausedUntil)}`
  else captureLabel.textContent = 'Start Capture'

  if (pauseBtn) {
    pauseBtn.hidden = !active
    pauseBtn.textContent = 'Pause'
  }

  if (tick) {
    clearInterval(tick)
    tick = null
  }
  if (paused) {
    tick = setInterval(() => {
      if (pausedUntil <= Date.now()) {
        clearInterval(tick)
        tick = null
        captureLabel.textContent = 'Start Capture'
        captureBtn.classList.remove('is-paused')
        return
      }
      captureLabel.textContent = `Resume · ${formatResume(pausedUntil)}`
    }, 15000)
  }
}
