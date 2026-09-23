// Settings: real but rarely used controls, kept out of the main navigation.
//   MCP servers                     McpSettings (/api/mcp/*)
//   Custom-node requirements        POST /api/comfyui/install-node-requirements
import { useState } from 'react'
import { api } from '../api.js'
import McpSettings from './McpSettings.jsx'
import { useToast } from './Toast.jsx'
import { BTN, Drawer, INPUT } from './ui.jsx'

function Section({ title, children }) {
  return (
    <section className="grid gap-3 border-b border-border-subtle pb-6 last:border-b-0">
      <h3 className="text-heading text-fg">{title}</h3>
      {children}
    </section>
  )
}

function NodeRequirements() {
  const toast = useToast()
  const [node, setNode] = useState('')
  const [busy, setBusy] = useState(false)
  const [output, setOutput] = useState('')

  const run = async (e) => {
    e.preventDefault()
    const name = node.trim()
    if (!name) return
    if (!window.confirm(`Install the Python requirements of ${name} inside the ComfyUI container?`)) return
    setBusy(true)
    setOutput('')
    try {
      const r = await api.post('/api/comfyui/install-node-requirements', { node_path: name, confirm: true })
      setOutput(r.output || '')
      toast(`Installed requirements for ${name}`, 'success')
    } catch (err) {
      setOutput(typeof err.body?.detail === 'object' ? err.body.detail.output || '' : '')
      toast(`Install failed: ${err.message}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="grid gap-2" onSubmit={run}>
      <label htmlFor="node-path" className="text-label text-muted">
        A folder under ComfyUI's custom_nodes whose requirements.txt should be installed
      </label>
      <div className="flex flex-wrap gap-2">
        <input id="node-path" className={INPUT + ' min-w-[14rem] flex-1'} placeholder="e.g. ComfyUI-GGUF"
               value={node} disabled={busy} onChange={(e) => setNode(e.target.value)} />
        <button type="submit" className={BTN} disabled={busy || !node.trim()}>{busy ? 'Installing…' : 'Install'}</button>
      </div>
      {output && (
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-sm border border-border-subtle bg-bg p-3 font-mono text-caption text-fg-muted">{output}</pre>
      )}
    </form>
  )
}

export default function SettingsDrawer({ open, onClose }) {
  return (
    <Drawer open={open} title="Settings" onClose={onClose}>
      <div className="grid gap-6">
        <Section title="MCP servers"><McpSettings /></Section>
        <Section title="ComfyUI custom-node requirements"><NodeRequirements /></Section>
      </div>
    </Drawer>
  )
}
