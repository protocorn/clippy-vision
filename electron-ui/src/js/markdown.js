/** Shared markdown render (was in archived chat.js). */
const markedApi = window.marked
if (markedApi?.setOptions) {
  markedApi.setOptions({ gfm: true, breaks: true })
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
