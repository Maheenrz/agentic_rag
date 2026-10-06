import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';

export default function Layout() {
  return (
    <div className="grid h-[calc(100vh-5rem)] grid-cols-[280px_1fr] gap-3 px-3 pb-3">
      <Sidebar />
      <main className="glass flex min-h-0 flex-col overflow-hidden rounded-2xl border border-white/5">
        <Outlet />
      </main>
    </div>
  );
}