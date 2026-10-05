import { Navigate, Outlet } from 'react-router-dom';
import { useApp } from '../context/AppContext';
import Sidebar from './Sidebar';

export default function Layout() {
  const { me } = useApp();

  if (!me) return <Navigate to="/login" replace />;

  return (
    <div className="grid h-screen grid-cols-[280px_1fr]">
      <Sidebar />
      <main className="flex min-h-0 flex-col">
        <Outlet />
      </main>
    </div>
  );
}