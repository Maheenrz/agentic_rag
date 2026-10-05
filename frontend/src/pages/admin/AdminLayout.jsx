import { NavLink, Outlet } from 'react-router-dom';
import { cx } from '../../components/ui';

const TABS = [
  ['/admin/users', 'Users'],
  ['/admin/groups', 'Groups'],
  ['/admin/collections', 'Collections'],
  ['/admin/guardrails', 'Guardrails'],
  ['/admin/feedback', 'Feedback'],
  ['/admin/audit', 'Audit log'],
];

export default function AdminLayout() {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-6">
      <h1 className="mb-4 text-2xl font-semibold">Admin console</h1>

      <nav className="mb-6 flex flex-wrap gap-2">
        {TABS.map(([to, label]) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              cx(
                'rounded-lg border px-3 py-1.5 text-sm',
                isActive
                  ? 'border-accent bg-accent font-semibold text-canvas'
                  : 'border-border bg-surface-2 hover:border-accent'
              )
            }
          >
            {label}
          </NavLink>
        ))}
      </nav>

      <Outlet />
    </div>
  );
}