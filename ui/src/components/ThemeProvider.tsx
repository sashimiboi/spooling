'use client';

import { createContext, useContext } from 'react';

interface ThemeContextType {
  theme: 'dark';
  setTheme: (t?: string) => void;
  resolved: 'dark';
}

const ThemeContext = createContext<ThemeContextType>({
  theme: 'dark',
  setTheme: () => {},
  resolved: 'dark',
});

export function useTheme() {
  return useContext(ThemeContext);
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  return (
    <ThemeContext.Provider value={{ theme: 'dark', setTheme: (_t?: string) => {}, resolved: 'dark' }}>
      {children}
    </ThemeContext.Provider>
  );
}
