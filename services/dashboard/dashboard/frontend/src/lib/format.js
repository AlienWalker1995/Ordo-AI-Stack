// Formatting helpers shared by every page. Pure functions, no React.

export function formatBytes(bytes) {
  if (bytes == null || Number.isNaN(bytes)) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = bytes
  let unit = 0
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000
    unit += 1
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`
}

export function formatGb(value) {
  if (value == null) return '—'
  return value.toLocaleString(undefined, { maximumFractionDigits: 1, minimumFractionDigits: 1 })
}

export function formatRate(value) {
  if (value == null) return '—'
  return value >= 10 ? value.toFixed(0) : value.toFixed(1)
}

// "3 min ago" from an epoch in SECONDS.
export function timeAgo(epochSeconds, now = Date.now()) {
  if (!epochSeconds) return ''
  const seconds = Math.max(0, Math.round(now / 1000 - epochSeconds))
  if (seconds < 45) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} h ago`
  const days = Math.round(hours / 24)
  return `${days} d ago`
}

// "13:45" from an epoch in SECONDS, local time; "Sep 23" when it is not today, so an old event
// never reads as one from this morning.
export function clock(epochSeconds, now = Date.now()) {
  if (!epochSeconds) return ''
  const when = new Date(epochSeconds * 1000)
  if (when.toDateString() !== new Date(now).toDateString()) {
    return when.toLocaleDateString([], { month: 'short', day: 'numeric' })
  }
  return when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}

export function pct(used, total) {
  if (!total) return 0
  return Math.max(0, Math.min(100, (used / total) * 100))
}
