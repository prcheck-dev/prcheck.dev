import { useEffect, useState } from 'react'
import './App.css'

function App() {
  const [api, setApi] = useState(null)
  const [error, setError] = useState(null)

  // In dev, VITE_API_URL is empty and the Vite proxy forwards /api to Django.
  // In production it points at the deployed API origin.
  const apiBase = import.meta.env.VITE_API_URL ?? ''

  useEffect(() => {
    fetch(`${apiBase}/api/health/`)
      .then((res) => res.json())
      .then(setApi)
      .catch((err) => setError(err.message))
  }, [])

  return (
    <div className="App">
      <h1>prcheck.dev</h1>
      <p>React + Django starter</p>
      <div className="card">
        <h2>Backend status</h2>
        {api && <pre>{JSON.stringify(api, null, 2)}</pre>}
        {error && <p style={{ color: 'crimson' }}>API unreachable: {error}</p>}
        {!api && !error && <p>Checking…</p>}
      </div>
    </div>
  )
}

export default App
