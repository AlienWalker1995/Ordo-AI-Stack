import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import { HostStepsProvider } from './components/HostSteps.jsx'
import './index.css'

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <HostStepsProvider>
      <App />
    </HostStepsProvider>
  </React.StrictMode>,
)
