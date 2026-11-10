/** @type {import('tailwindcss').Config} */
// The "Ordo Nexus" theme as Tailwind design tokens: a dark and a light palette, chosen per viewer
// (src/lib/theme.jsx). Components now style with utilities that reference these
// (bg-bg, text-fg, border-border, text-accent, …); a small @layer in src/index.css keeps
// only the keyframe/pseudo-element effects that are awkward as inline utilities.
export default {
  content: ['./index.html', './src/**/*.{js,jsx,ts,tsx}'],
  theme: {
    extend: {
      // Every colour is a theme variable (src/index.css defines the dark and light values), written
      // as RGB channels so opacity modifiers such as bg-accent/[0.12] keep working.
      colors: {
        bg: 'rgb(var(--c-bg) / <alpha-value>)',
        'bg-elevated': 'rgb(var(--c-bg-elevated) / <alpha-value>)',
        surface: 'rgb(var(--c-surface) / <alpha-value>)',
        'surface-hover': 'rgb(var(--c-surface-hover) / <alpha-value>)',
        card: 'rgb(var(--c-card) / <alpha-value>)',
        border: 'rgb(var(--c-border) / <alpha-value>)',
        'border-subtle': 'rgb(var(--c-border-subtle) / <alpha-value>)',
        fg: 'rgb(var(--c-fg) / <alpha-value>)',
        'fg-muted': 'rgb(var(--c-fg-muted) / <alpha-value>)',
        muted: 'rgb(var(--c-muted) / <alpha-value>)',
        accent: 'rgb(var(--c-accent) / <alpha-value>)',
        'accent-soft': 'rgb(var(--c-accent-soft) / <alpha-value>)',
        success: 'rgb(var(--c-success) / <alpha-value>)',
        warning: 'rgb(var(--c-warning) / <alpha-value>)',
        danger: 'rgb(var(--c-danger) / <alpha-value>)',
        info: 'rgb(var(--c-info) / <alpha-value>)',
      },
      fontFamily: {
        // Native system-font stacks — fully self-contained, zero external font hosts.
        display: ['system-ui', '-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Roboto', 'Helvetica', 'Arial', 'sans-serif'],
        sans: ['-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Roboto', 'Helvetica', 'Arial', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', '"SF Mono"', 'Menlo', 'Consolas', '"Liberation Mono"', 'monospace'],
      },
      // A deliberate role-based type scale (extends, does not replace, Tailwind's defaults).
      // Hierarchy is carried by SIZE + WEIGHT + LINE-HEIGHT + TONE — not by uppercasing and
      // letter-spacing every small label. Large display is optically tightened (needs no
      // caps to read as a title); small roles stay near-normal tracking so sentence-case
      // labels read as calm text, not a uniform small-caps stamp.
      //   display  — the app wordmark (one per screen)
      //   title    — dialog / prominent inline titles
      //   heading  — in-panel sub-section headings (sentence case, brighter tone)
      //   body     — descriptions and running text
      //   label    — form + field labels (sentence case, muted)
      //   caption  — captions, table headers, secondary meta
      //   micro    — dense stat labels
      // NOTE: the single UPPERCASE treatment left in the system is the `.section-rule`
      // panel header (see index.css) — the one deliberate eyebrow, nothing else.
      fontSize: {
        display: ['2.25rem', { lineHeight: '1.1', letterSpacing: '-0.02em', fontWeight: '700' }],
        title: ['1.0625rem', { lineHeight: '1.3', letterSpacing: '-0.01em', fontWeight: '700' }],
        heading: ['0.9rem', { lineHeight: '1.35', fontWeight: '600' }],
        body: ['0.8125rem', { lineHeight: '1.6' }],
        label: ['0.75rem', { lineHeight: '1.4', fontWeight: '500' }],
        caption: ['0.7rem', { lineHeight: '1.45' }],
        micro: ['0.62rem', { lineHeight: '1.4', letterSpacing: '0.01em' }],
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
        // Content is visible by default — the entrance animates TRANSFORM only, never
        // opacity (nothing is hidden behind opacity:0 waiting on JS/animation to reveal it).
        'fade-up': {
          from: { transform: 'translateY(6px)' },
          to: { transform: 'none' },
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
