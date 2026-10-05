import { useEffect, useState } from "react";
import { useAuth } from "../../context/AuthContext";
import {
  addGroupMember, createOrgGroup, deleteOrgGroup,
  listOrgGroups, listOrgUsers, removeGroupMember,
} from "../../api/client";

export default function GroupsPanel() {
  const { token } = useAuth();
  const [groups, setGroups] = useState([]);
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [newGroupName, setNewGroupName] = useState("");
  const [addingTo, setAddingTo] = useState(null); // group_id currently picking a member for
  const [pickedUserId, setPickedUserId] = useState("");

  const refresh = async () => {
    setLoading(true);
    try {
      const [g, u] = await Promise.all([listOrgGroups(token), listOrgUsers(token)]);
      setGroups(g);
      setUsers(u);
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

  const usernameOf = (userId) => users.find((u) => u.user_id === userId)?.username || userId;

  const handleCreateGroup = async (e) => {
    e.preventDefault();
    setError("");
    try {
      await createOrgGroup(token, newGroupName);
      setNewGroupName("");
      await refresh();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDeleteGroup = async (groupId, name) => {
    if (!window.confirm(`Delete group "${name}"?`)) return;
    try {
      await deleteOrgGroup(token, groupId);
      await refresh();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleAddMember = async (groupId) => {
    if (!pickedUserId) return;
    try {
      await addGroupMember(token, groupId, pickedUserId);
      setAddingTo(null);
      setPickedUserId("");
      await refresh();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleRemoveMember = async (groupId, userId) => {
    try {
      await removeGroupMember(token, groupId, userId);
      await refresh();
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <div className="space-y-6">
      <form onSubmit={handleCreateGroup} className="bg-white border border-slate-200 rounded-xl p-5 flex gap-3">
        <input
          className="flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm"
          placeholder="New group name"
          value={newGroupName}
          onChange={(e) => setNewGroupName(e.target.value)}
          required
        />
        <button
          type="submit"
          className="rounded-lg bg-slate-900 text-white text-sm font-medium px-4 py-2 hover:bg-slate-800"
        >
          Create group
        </button>
      </form>

      {error && <p className="text-sm text-red-600">{error}</p>}

      {loading ? (
        <p className="text-sm text-slate-400">Loading...</p>
      ) : groups.length === 0 ? (
        <p className="text-sm text-slate-400">No groups yet.</p>
      ) : (
        <div className="space-y-4">
          {groups.map((g) => (
            <div key={g.group_id} className="bg-white border border-slate-200 rounded-xl p-5">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-semibold text-slate-900">{g.name}</h3>
                <button
                  onClick={() => handleDeleteGroup(g.group_id, g.name)}
                  className="text-red-600 text-xs font-medium hover:underline"
                >
                  Delete group
                </button>
              </div>

              <ul className="space-y-1 mb-3">
                {g.member_ids.length === 0 && (
                  <li className="text-sm text-slate-400">No members yet.</li>
                )}
                {g.member_ids.map((uid) => (
                  <li key={uid} className="flex items-center justify-between text-sm">
                    <span className="text-slate-700">{usernameOf(uid)}</span>
                    <button
                      onClick={() => handleRemoveMember(g.group_id, uid)}
                      className="text-slate-400 text-xs hover:text-red-600 hover:underline"
                    >
                      Remove
                    </button>
                  </li>
                ))}
              </ul>

              {addingTo === g.group_id ? (
                <div className="flex gap-2">
                  <select
                    className="flex-1 rounded-lg border border-slate-300 px-2 py-1.5 text-sm"
                    value={pickedUserId}
                    onChange={(e) => setPickedUserId(e.target.value)}
                  >
                    <option value="">Select a user...</option>
                    {users
                      .filter((u) => !g.member_ids.includes(u.user_id))
                      .map((u) => (
                        <option key={u.user_id} value={u.user_id}>{u.username}</option>
                      ))}
                  </select>
                  <button
                    onClick={() => handleAddMember(g.group_id)}
                    className="rounded-lg bg-slate-900 text-white text-xs font-medium px-3 py-1.5 hover:bg-slate-800"
                  >
                    Add
                  </button>
                  <button
                    onClick={() => { setAddingTo(null); setPickedUserId(""); }}
                    className="text-xs text-slate-400 hover:underline"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setAddingTo(g.group_id)}
                  className="text-xs text-slate-900 font-medium hover:underline"
                >
                  + Add member
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}