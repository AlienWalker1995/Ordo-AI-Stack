import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import { HostStepsProvider } from './components/HostSteps.jsx'
import { ThemeProvider } from './lib/theme.jsx'
import './index.css'

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ThemeProvider>
      <HostStepsProvider>
        <App />
      </HostStepsProvider>
    </ThemeProvider>
  </React.StrictMode>,
)
