import { Navigate } from 'react-router-dom';
import { useApp } from '../context/AppContext';
import Layout from './Layout';

export default function ProtectedRoute() {
  const { me, ready } = useApp();
  if (!ready) return <div className="p-6 text-sm text-muted">Loading your session…</div>;
  if (!me) return <Navigate to="/login" replace />;
  return <Layout />;
}