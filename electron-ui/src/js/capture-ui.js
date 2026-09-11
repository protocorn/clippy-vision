import { captureBtn, captureLabel } from './dom.js'

export function setCaptureUI(active) {
  if (!captureBtn || !captureLabel) return
  if (active) {
    captureBtn.classList.add('active')
    captureLabel.textContent = 'Stop Capture'
  } else {
    captureBtn.classList.remove('active')
    captureLabel.textContent = 'Start Capture'
  }
}
