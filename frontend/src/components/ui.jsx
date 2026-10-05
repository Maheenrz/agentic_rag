export function cx(...parts) {
  return parts.filter(Boolean).join(' ');
}

const VARIANTS = {
  primary: 'border-accent bg-accent font-semibold text-canvas hover:bg-accent-hover hover:border-accent-hover',
  subtle: 'border-border bg-surface-2 text-ink hover:border-muted',
  ghost: 'border-transparent bg-transparent text-muted hover:text-ink',
  danger: 'border-border bg-transparent text-danger hover:border-danger',
};

const SIZES = {
  md: 'rounded-full px-4 py-2 text-sm',
  sm: 'rounded-full px-3 py-1 text-xs',
};

export function Button({ variant = 'subtle', size = 'md', className, type = 'button', ...props }) {
  return (
    <button
      type={type}
      className={cx(
        'border transition disabled:cursor-not-allowed disabled:opacity-50',
        VARIANTS[variant],
        SIZES[size],
        className
      )}
      {...props}
    />
  );
}

const field =
  'rounded-xl border border-border bg-surface-2 px-3.5 py-2.5 text-sm text-ink placeholder:text-muted focus:border-accent focus:outline-none';

export function Input({ className, ...props }) {
  return <input className={cx(field, 'w-full', className)} {...props} />;
}

export function Textarea({ className, ...props }) {
  return <textarea className={cx(field, 'w-full', className)} {...props} />;
}

export function Select({ className, children, ...props }) {
  return <select className={cx(field, className)} {...props}>{children}</select>;
}

export function Label({ className, children }) {
  return <label className={cx('mb-1.5 block text-xs font-medium text-muted', className)}>{children}</label>;
}

export function Card({ className, children }) {
  return <div className={cx('rounded-2xl border border-border bg-surface p-6', className)}>{children}</div>;
}

export function Pill({ children }) {
  return (
    <span className="inline-block rounded-full border border-border px-2 py-0.5 text-xs text-muted">
      {children}
    </span>
  );
}

export function Check({ label, ...props }) {
  return (
    <label className="flex items-center gap-2.5 py-1 text-sm">
      <input type="checkbox" className="h-4 w-4 accent-accent" {...props} />
      {label}
    </label>
  );
}

export function Stat({ value, label }) {
  return (
    <div className="rounded-2xl border border-border bg-surface p-5">
      <div className="font-display text-3xl text-ink">{value}</div>
      <div className="mt-1 text-xs text-muted">{label}</div>
    </div>
  );
}

export function Table({ head, rows }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr>
            {head.map((h, i) => (
              <th key={i} className="border-b border-border px-3 py-2.5 text-left text-xs font-medium text-muted">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-border/60 last:border-0">
              {row.map((cell, j) => (
                <td key={j} className="px-3 py-3 align-top">{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Loading({ error }) {
  return <p className={error ? 'text-danger' : 'text-muted'}>{error || 'Loading…'}</p>;
}