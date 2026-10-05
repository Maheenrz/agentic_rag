import { useEffect, useState } from "react";
import { useAuth } from "../../context/AuthContext";
import { createOrgUser, deleteOrgUser, listOrgUsers } from "../../api/client";

export default function UsersPanel() {
  const { token, user: currentUser } = useAuth();
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("member");
  const [creating, setCreating] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      setUsers(await listOrgUsers(token));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleCreate = async (e) => {
    e.preventDefault();
    setError("");
    setCreating(true);
    try {
      await createOrgUser(token, { username, password, role });
      setUsername("");
      setPassword("");
      setRole("member");
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (userId, uname) => {
    if (!window.confirm(`Delete ${uname}? This also deletes their threads and uploads.`)) return;
    setError("");
    try {
      await deleteOrgUser(token, userId);
      await refresh();
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <div className="space-y-6">
      <form onSubmit={handleCreate} className="bg-white border border-slate-200 rounded-xl p-5 space-y-3">
        <h2 className="text-sm font-semibold text-slate-900">Add a user</h2>
        <div className="flex flex-wrap gap-3">
          <input
            className="flex-1 min-w-[140px] rounded-lg border border-slate-300 px-3 py-2 text-sm"
            placeholder="Username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
          <input
            className="flex-1 min-w-[140px] rounded-lg border border-slate-300 px-3 py-2 text-sm"
            type="password"
            placeholder="Password (min 8 chars)"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={8}
            required
          />
          <select
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
            value={role}
            onChange={(e) => setRole(e.target.value)}
          >
            <option value="member">Member</option>
            <option value="admin">Admin</option>
          </select>
          <button
            type="submit"
            disabled={creating}
            className="rounded-lg bg-slate-900 text-white text-sm font-medium px-4 py-2 hover:bg-slate-800 disabled:opacity-50"
          >
            {creating ? "Adding..." : "Add"}
          </button>
        </div>
      </form>

      {error && <p className="text-sm text-red-600">{error}</p>}

      <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-500 text-left">
            <tr>
              <th className="px-4 py-2 font-medium">Username</th>
              <th className="px-4 py-2 font-medium">Role</th>
              <th className="px-4 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td className="px-4 py-3 text-slate-400" colSpan={3}>Loading...</td></tr>
            ) : users.length === 0 ? (
              <tr><td className="px-4 py-3 text-slate-400" colSpan={3}>No users yet.</td></tr>
            ) : (
              users.map((u) => (
                <tr key={u.user_id} className="border-t border-slate-100">
                  <td className="px-4 py-2 text-slate-900">{u.username}</td>
                  <td className="px-4 py-2 text-slate-500">{u.role}</td>
                  <td className="px-4 py-2 text-right">
                    {u.user_id !== currentUser?.user_id && (
                      <button
                        onClick={() => handleDelete(u.user_id, u.username)}
                        className="text-red-600 text-xs font-medium hover:underline"
                      >
                        Delete
                      </button>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}