import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';
import './upgrade.css';
import './forge42.css';
class ErrorBoundary extends React.Component<{ children: React.ReactNode }, { error: string }> {
  state = { error: '' };
  static getDerivedStateFromError(error: Error) { return { error: error.message }; }
  render() {
    if (this.state.error) return <main className="fatal"><img src="./forge.svg" width="48" alt="Forge" /><h1>The workspace needs to reload</h1><p>{this.state.error}</p><button onClick={() => location.reload()}>Reload Forge</button></main>;
    return this.props.children;
  }
}
createRoot(document.getElementById('root')!).render(<ErrorBoundary><App /></ErrorBoundary>);
