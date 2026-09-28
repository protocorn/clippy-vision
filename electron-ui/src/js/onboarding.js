import { nameInput, nameSubmit, nameError, nameView } from './dom.js'
import { showView } from './utils.js'
import { setCaptureUI } from './capture-ui.js'
import { openTimeline } from './timeline.js'

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
    const active = await window.clippy.getCaptureStatus()
    setCaptureUI(active)
    openTimeline()
  } catch (error) {
    nameError.textContent = `Could not save name: ${error.message}`
  } finally {
    nameSubmit.disabled = false
  }
}
