/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        border: 'var(--border)',
        background: 'var(--bg)',
        foreground: 'var(--fg)',
        primary: '#a78bfa',
        muted: '#a1a1aa',
        surface: 'var(--surface)',
      },
      fontSize: {
        '2xs': ['0.625rem', { lineHeight: '0.875rem' }],
      },
    },
  },
  plugins: [require('@tailwindcss/typography')],
};
