// How a finished GPU lease reads: who held the card, how it ended, for how long.

const HOLDERS = { 'gate-comfyui': 'ComfyUI render' }

export function lease(h) {
  const seconds = h.started && h.ended ? Math.max(0, Math.round(h.ended - h.started)) : null
  let duration = ''
  if (seconds != null) {
    duration = seconds < 60 ? `${seconds} s` : seconds < 3600 ? `${Math.round(seconds / 60)} min` : `${(seconds / 3600).toFixed(1)} h`
  }
  return {
    holder: HOLDERS[h.id] || h.id,
    outcome: h.outcome || '',
    tone: h.outcome === 'completed' ? 'ok' : h.outcome === 'rejected' ? 'critical' : 'warning',
    duration,
  }
}
