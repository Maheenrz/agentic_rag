import { Navigate, Route, Routes } from 'react-router-dom';
import { useApp } from './context/AppContext';
import TopNav from './components/Navbar';
import CommandPalette from './components/CommandPalette';
import Layout from './components/Layout';
import LandingPage from './pages/LandingPage';
import LoginPage from './pages/LoginPage';
import Register from './pages/Register';
import ChatPage from './pages/ChatPage';
import UploadPage from './pages/UploadPage';
import AdminLayout from './pages/admin/AdminLayout';
import UsersPage from './pages/admin/UsersPage';
import GroupsPage from './pages/admin/GroupsPage';
import CollectionsPage from './pages/admin/CollectionsPage';
import GuardrailsPage from './pages/admin/GuardrailsPage';
import FeedbackPage from './pages/admin/FeedbackPage';
import AuditPage from './pages/admin/AuditPage';

function AdminOnly({ children }) {
  const { me } = useApp();
  return me?.role === 'admin' ? children : <Navigate to="/chat" replace />;
}

function Protected() {
  const { me, ready } = useApp();
  if (!ready) return <BootSplash />;
  if (!me) return <Navigate to="/login" replace />;
  return <Layout />;
}

function GuestOnly({ children }) {
  const { me, ready } = useApp();
  if (!ready) return <BootSplash />;
  if (me) return <Navigate to="/chat" replace />;
  return children;
}

function BootSplash() {
  return (
    <div className="flex h-screen items-center justify-center">
      <div className="glass rounded-2xl px-8 py-6 font-display text-3xl text-ink animate-fade-up">
        Docs Assistant
      </div>
    </div>
  );
}

export default function App() {
  return (
    <>
      <TopNav />
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/login"    element={<GuestOnly><LoginPage /></GuestOnly>} />
        <Route path="/register" element={<GuestOnly><Register /></GuestOnly>} />

        <Route element={<Protected />}>
          <Route path="chat" element={<ChatPage />} />
          <Route path="chat/:threadId" element={<ChatPage />} />
          <Route path="upload" element={<UploadPage />} />

          <Route path="admin" element={<AdminOnly><AdminLayout /></AdminOnly>}>
            <Route index element={<Navigate to="users" replace />} />
            <Route path="users"       element={<UsersPage />} />
            <Route path="groups"      element={<GroupsPage />} />
            <Route path="collections" element={<CollectionsPage />} />
            <Route path="guardrails"  element={<GuardrailsPage />} />
            <Route path="feedback"    element={<FeedbackPage />} />
            <Route path="audit"       element={<AuditPage />} />
          </Route>
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <CommandPalette />
    </>
  );
}