// Lint gate for the dashboard SPA: ESLint's recommended rules plus the two React hook rules the
// source already annotates (rules-of-hooks, exhaustive-deps).
import js from '@eslint/js'
import reactHooks from 'eslint-plugin-react-hooks'
import globals from 'globals'

export default [
  { ignores: ['dist/', 'playwright-report/', 'test-results/'] },
  js.configs.recommended,
  {
    files: ['**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',
    },
  },
  {
    // Run by Node, not the browser: tool config and the Playwright suite.
    files: ['*.config.js', 'e2e/**/*.js'],
    languageOptions: { globals: { ...globals.node } },
  },
]
