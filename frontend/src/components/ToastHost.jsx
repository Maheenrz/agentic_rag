import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { CheckCircle2, AlertTriangle, Info, X } from 'lucide-react';
import { cx } from './ui';

const ToastCtx = createContext(null);

export function ToastProvider({ children }) {
  const [items, setItems] = useState([]);
  const push = useCallback((message, tone = 'info', ttl = 4000) => {
    const id = Math.random().toString(36).slice(2);
    setItems((s) => [...s, { id, message, tone }]);
    if (ttl) setTimeout(() => setItems((s) => s.filter((t) => t.id !== id)), ttl);
  }, []);
  const remove = (id) => setItems((s) => s.filter((t) => t.id !== id));

  const toast = {
    info:    (m) => push(m, 'info'),
    success: (m) => push(m, 'success'),
    error:   (m) => push(m, 'error'),
  };

  return (
    <ToastCtx.Provider value={toast}>
      {children}
      {createPortal(
        <div className="pointer-events-none fixed bottom-5 right-5 z-[200] flex w-full max-w-sm flex-col gap-2">
          {items.map((t) => <ToastItem key={t.id} {...t} onClose={() => remove(t.id)} />)}
        </div>,
        document.body
      )}
    </ToastCtx.Provider>
  );
}

export const useToast = () => {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error('useToast must be used inside ToastProvider');
  return ctx;
};

function ToastItem({ message, tone, onClose }) {
  const Icon = tone === 'success' ? CheckCircle2 : tone === 'error' ? AlertTriangle : Info;
  const color = tone === 'success' ? 'text-success' : tone === 'error' ? 'text-danger' : 'text-accent';
  return (
    <div className="glass-strong pointer-events-auto flex items-start gap-3 rounded-2xl p-3.5 animate-fade-up">
      <Icon size={18} strokeWidth={1.9} className={cx('mt-0.5 shrink-0', color)} />
      <p className="flex-1 text-sm text-ink">{message}</p>
      <button onClick={onClose} className="rounded-full p-1 text-muted hover:bg-surface-2 hover:text-ink">
        <X size={14} />
      </button>
    </div>
  );
}