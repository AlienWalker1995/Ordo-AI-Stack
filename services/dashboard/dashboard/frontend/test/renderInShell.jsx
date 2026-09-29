// Render a page or panel inside the providers App gives it (toasts, host steps) and the host-step
// banners App draws above every page, so a test sees what an operator would.
import { render } from '@testing-library/react'
import { HostStepBanners, HostStepsProvider } from '../src/components/HostSteps.jsx'
import { ToastProvider } from '../src/components/Toast.jsx'

export function renderInShell(ui) {
  return render(
    <HostStepsProvider>
      <ToastProvider>
        <HostStepBanners idPrefix="test-host-step" />
        {ui}
      </ToastProvider>
    </HostStepsProvider>,
  )
}
