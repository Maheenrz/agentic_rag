import { Navigate, Route, Routes } from 'react-router-dom';
import { useApp } from './context/AppContext';
import Layout from './components/Layout';
import LandingPage from './pages/LandingPage';
import LoginPage from './pages/LoginPage';
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

export default function App() {
  const { me, ready } = useApp();

  if (!ready) {
    return (
      <div className="flex h-screen items-center justify-center font-display text-3xl text-muted">
        Docs Assistant
      </div>
    );
  }

  const guestOnly = (page) => (me ? <Navigate to="/chat" replace /> : page);

  return (
    <Routes>
      <Route path="/" element={guestOnly(<LandingPage />)} />
      <Route path="/login" element={guestOnly(<LoginPage mode="login" />)} />
      <Route path="/register" element={guestOnly(<LoginPage mode="register" />)} />

      <Route element={<Layout />}>
        <Route path="chat" element={<ChatPage />} />
        <Route path="chat/:threadId" element={<ChatPage />} />
        <Route path="upload" element={<UploadPage />} />

        <Route path="admin" element={<AdminOnly><AdminLayout /></AdminOnly>}>
          <Route index element={<Navigate to="users" replace />} />
          <Route path="users" element={<UsersPage />} />
          <Route path="groups" element={<GroupsPage />} />
          <Route path="collections" element={<CollectionsPage />} />
          <Route path="guardrails" element={<GuardrailsPage />} />
          <Route path="feedback" element={<FeedbackPage />} />
          <Route path="audit" element={<AuditPage />} />
        </Route>
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}