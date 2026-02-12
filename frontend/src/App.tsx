import { useEffect, useState } from 'react';
import { TranslationPanel } from './components/TranslationPanel';
import { TestDashboard } from './components/TestDashboard';

function App() {
  const [route, setRoute] = useState(window.location.hash);

  useEffect(() => {
    const onHashChange = () => setRoute(window.location.hash);
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  if (route === '#/test') {
    return <TestDashboard />;
  }

  return <TranslationPanel />;
}

export default App;
