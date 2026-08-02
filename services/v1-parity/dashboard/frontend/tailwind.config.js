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
