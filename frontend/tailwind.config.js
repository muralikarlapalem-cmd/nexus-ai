/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        nexus: {
          bg:      '#0a0e1a',
          surface: '#131929',
          border:  '#1e2a45',
          muted:   '#64748b',
          text:    '#e2e8f0',
          primary: '#6366f1',
          accent:  '#8b5cf6',
        },
      },
      fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] },
    },
  },
  plugins: [],
}
