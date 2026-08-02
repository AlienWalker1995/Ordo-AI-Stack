/** @type {import('tailwindcss').Config} */
// The "Ordo Nexus" dark theme, ported from the legacy dashboard's CSS custom properties
// into Tailwind design tokens. Components now style with utilities that reference these
// (bg-bg, text-fg, border-border, text-accent, …); a small @layer in src/index.css keeps
// only the keyframe/pseudo-element effects that are awkward as inline utilities.
export default {
  content: ['./index.html', './src/**/*.{js,jsx,ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Surfaces — neutral graphite, a hair warm. Deliberately NOT the slate-indigo /
        // blue-charcoal (#0c0e15) default dark theme; elevation reads through tone, not hue.
        bg: '#0a0a0b',
        'bg-elevated': '#111113',
        surface: '#161618',
        'surface-hover': '#1e1e21',
        card: '#161618',
        border: '#2b2b30',
        'border-subtle': '#1f1f23',
        // Text — neutral, blue cast removed so it reads as ink on graphite, not tinted glass.
        fg: '#eaeaec',
        'fg-muted': '#9a9aa3',
        muted: '#9a9aa3',
        // Accent: a single disciplined cyan, slightly tempered from electric so it can
        // carry state without shouting. Held as the ONLY chromatic accent across the UI.
        accent: '#2bb8e6',
        'accent-soft': '#63cdee',
        // Semantic — reserved for state only, tuned to sit on graphite without neon glare.
        success: '#2bb673',
        warning: '#e0a52e',
        danger: '#e5495f',
        info: '#5a7290',
      },
      fontFamily: {
        // Native system-font stacks — fully self-contained, zero external font hosts.
        display: ['system-ui', '-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Roboto', 'Helvetica', 'Arial', 'sans-serif'],
        sans: ['-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Roboto', 'Helvetica', 'Arial', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', '"SF Mono"', 'Menlo', 'Consolas', '"Liberation Mono"', 'monospace'],
      },
      borderRadius: {
        sm: '5px',
        md: '7px',
        DEFAULT: '10px',
        lg: '14px',
      },
      boxShadow: {
        // Tight, low-offset, single-direction fall-off tinted to the near-black base —
        // depth from a grounded soft shadow, not a big symmetric black halo.
        card: '0 1px 2px rgba(0,0,0,.5), 0 4px 12px rgba(0,0,0,.28)',
        'card-lg': '0 2px 6px rgba(0,0,0,.55), 0 12px 32px rgba(0,0,0,.42)',
      },
      maxWidth: {
        container: '1200px',
      },
      keyframes: {
        'fade-up': {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'none' },
        },
        'toast-in': {
          from: { opacity: '0', transform: 'translateX(16px) scale(.96)' },
          to: { opacity: '1', transform: 'translateX(0) scale(1)' },
        },
        skeleton: {
          '0%': { backgroundPosition: '200% 0' },
          '100%': { backgroundPosition: '-200% 0' },
        },
      },
      animation: {
        'fade-up': 'fade-up .45s cubic-bezier(.16,1,.3,1)',
        'toast-in': 'toast-in .3s cubic-bezier(.16,1,.3,1)',
        skeleton: 'skeleton 1.8s ease-in-out infinite',
      },
    },
  },
  plugins: [],
}
