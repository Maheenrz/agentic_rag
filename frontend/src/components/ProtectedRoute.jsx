import { Navigate, Outlet } from 'react-router-dom';
import { getToken } from '../lib/api';
import Layout from './Layout';

export default function ProtectedRoute() {
  if (!getToken()) return <Navigate to="/login" replace />;
  return <Layout />;
}