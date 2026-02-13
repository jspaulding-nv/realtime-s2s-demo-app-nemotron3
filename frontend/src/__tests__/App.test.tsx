import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';

// Mock the components to avoid pulling in WebSocket, AudioContext, etc.
vi.mock('../components/TranslationPanel', () => ({
  TranslationPanel: () => <div data-testid="translation-panel">Translation Panel</div>,
}));

vi.mock('../components/TestDashboard', () => ({
  TestDashboard: () => <div data-testid="test-dashboard">Test Dashboard</div>,
}));

import App from '../App';

describe('App routing', () => {
  beforeEach(() => {
    // Reset hash before each test
    window.location.hash = '';
  });

  it('renders TranslationPanel by default (no hash)', () => {
    render(<App />);
    expect(screen.getByTestId('translation-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('test-dashboard')).not.toBeInTheDocument();
  });

  it('renders TestDashboard when hash is #/test', () => {
    window.location.hash = '#/test';
    render(<App />);
    expect(screen.getByTestId('test-dashboard')).toBeInTheDocument();
    expect(screen.queryByTestId('translation-panel')).not.toBeInTheDocument();
  });

  it('switches from translation to test dashboard on hash change', async () => {
    render(<App />);
    expect(screen.getByTestId('translation-panel')).toBeInTheDocument();

    act(() => {
      window.location.hash = '#/test';
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });

    expect(screen.getByTestId('test-dashboard')).toBeInTheDocument();
    expect(screen.queryByTestId('translation-panel')).not.toBeInTheDocument();
  });

  it('switches back to translation panel when hash cleared', async () => {
    window.location.hash = '#/test';
    render(<App />);
    expect(screen.getByTestId('test-dashboard')).toBeInTheDocument();

    act(() => {
      window.location.hash = '#/';
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });

    expect(screen.getByTestId('translation-panel')).toBeInTheDocument();
  });
});
