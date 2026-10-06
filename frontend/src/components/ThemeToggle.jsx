import { Sun, Moon } from 'lucide-react';
import { useTheme } from '../context/ThemeContext';
import { cx } from './ui';

export default function ThemeToggle({ className }) {
  const { theme, toggle } = useTheme();
  const dark = theme === 'dark';
  return (
    <button
      onClick={toggle}
      title={dark ? 'Switch to light mode' : 'Switch to dark mode'}
      className={cx(
        'grid h-9 w-9 place-items-center rounded-full border border-border bg-surface-2/60 text-muted transition hover:text-ink hover:bg-surface-2',
        className
      )}
    >
      {dark ? <Sun size={16} strokeWidth={1.9} /> : <Moon size={16} strokeWidth={1.9} />}
    </button>
  );
}