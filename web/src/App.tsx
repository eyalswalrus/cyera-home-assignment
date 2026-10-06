import { useEffect, useState } from 'react'

// Placeholder shell - replaced by the real UI in step 5.
export default function App() {
  const [status, setStatus] = useState('checking…')

  useEffect(() => {
    fetch('/api/health')
      .then((res) => res.json())
      .then((body) => setStatus(body.jira_configured ? 'ok' : 'ok (Jira OAuth app not configured)'))
      .catch(() => setStatus('API unreachable'))
  }, [])

  return (
    <main>
      <h1>IdentityHub</h1>
      <p>Backend status: {status}</p>
    </main>
  )
}
