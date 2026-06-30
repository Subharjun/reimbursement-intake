import { useState, useCallback, useEffect } from 'react';
import IntakeForm from './IntakeForm';
import './App.css';

const SPLASH_MS = 8000;

function SplashScreen({ fading }: { fading: boolean }) {
  return (
    <div className={`splash${fading ? ' splash--fading' : ''}`}>
      <div className="splash-inner">
        <div className="splash-logo">
          <span className="splash-logo-icon">⚡</span>
          <span className="splash-logo-text">ClaimAgent</span>
        </div>
        <p className="splash-tagline">AI-powered expense reimbursement</p>
        <div className="splash-bar-track">
          <div className="splash-bar-fill" style={{ animationDuration: `${SPLASH_MS}ms` }} />
        </div>
        <p className="splash-status">Starting up…</p>
      </div>
    </div>
  );
}

function App() {
  const [darkTheme, setDarkTheme] = useState(false);
  const [splashDone, setSplashDone] = useState(false);
  const [splashFading, setSplashFading] = useState(false);
  const toggleTheme = useCallback(() => setDarkTheme((d) => !d), []);

  useEffect(() => {
    document.body.className = darkTheme ? 'dark' : 'light';
  }, [darkTheme]);

  useEffect(() => {
    const fadeTimer = setTimeout(() => setSplashFading(true), SPLASH_MS - 600);
    const doneTimer = setTimeout(() => setSplashDone(true), SPLASH_MS);
    return () => { clearTimeout(fadeTimer); clearTimeout(doneTimer); };
  }, []);

  return (
    <div className={`app-shell ${darkTheme ? 'dark' : 'light'}`}>
      {!splashDone && <SplashScreen fading={splashFading} />}
      {splashDone && <IntakeForm darkTheme={darkTheme} onToggleTheme={toggleTheme} />}
    </div>
  );
}

export default App;
