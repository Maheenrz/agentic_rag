import { useState } from "react";
import UsersPanel from "./Userspanel";
import GroupsPanel from "./Groupspanel";

export default function AdminPage() {
  const [tab, setTab] = useState("users");

  return (
    <div className="min-h-screen bg-slate-50 px-6 py-8">
      <div className="max-w-3xl mx-auto">
        <h1 className="text-xl font-semibold text-slate-900 mb-6">Organization settings</h1>

        <div className="flex gap-1 mb-6 border-b border-slate-200">
          {["users", "groups"].map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px ${
                tab === t
                  ? "border-slate-900 text-slate-900"
                  : "border-transparent text-slate-500 hover:text-slate-700"
              }`}
            >
              {t === "users" ? "Users" : "Groups"}
            </button>
          ))}
        </div>

        {tab === "users" ? <UsersPanel /> : <GroupsPanel />}
      </div>
    </div>
  );
}