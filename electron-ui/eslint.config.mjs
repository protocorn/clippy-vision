import globals from 'globals'

/** Lightweight renderer guard: catch missing imports / undefined identifiers. */
export default [
  {
    ignores: ['src/js/**/*.test.js'],
  },
  {
    files: ['src/js/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        ...globals.browser,
      },
    },
    rules: {
      'no-undef': 'error',
    },
  },
]
