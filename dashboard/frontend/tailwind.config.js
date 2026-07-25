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
        // Surfaces
        bg: '#020307',
        'bg-elevated': '#07080e',
        surface: '#0c0e15',
        'surface-hover': '#131620',
        card: '#0c0e15',
        border: '#1c1f2e',
        'border-subtle': '#111320',
        // Text
        fg: '#dde2ee',
        'fg-muted': '#848da4',
        muted: '#8a90a8',
        // Accent: electric cyan
        accent: '#00c9ff',
        'accent-soft': '#4dd8ff',
        // Semantic
        success: '#00d47a',
        warning: '#ffb300',
        danger: '#ff3358',
        info: '#7c4dff',
      },
      fontFamily: {
        display: ['"Barlow Condensed"', 'system-ui', 'sans-serif'],
        sans: ['"DM Sans"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      borderRadius: {
        sm: '5px',
        md: '7px',
        DEFAULT: '10px',
        lg: '14px',
      },
      boxShadow: {
        card: '0 3px 18px rgba(2,3,7,.75), 0 1px 4px rgba(2,3,7,.4)',
        'card-lg': '0 8px 36px rgba(2,3,7,.88), 0 2px 8px rgba(2,3,7,.5)',
        'glow-accent': '0 0 16px rgba(0,201,255,.2)',
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
