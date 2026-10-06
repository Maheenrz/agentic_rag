import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  ArrowRight, ShieldCheck, Quote, Eye, ScrollText, Lock, Library, Send,
  Sparkles, FileText,
} from 'lucide-react';
import { useApp } from '../context/AppContext';
import { Button } from '../components/ui';
import GuestChat from '../components/GuestChat';

const principles = [
  ['Access first',  'Search only looks inside collections the person is allowed to read. The database filter enforces this, not the language model.', Eye],
  ['Quoted sources','Every answer names the file and section, and quotes the exact sentence it used, so you can check it yourself.',                 Quote],
  ['Guardrails',    'Card numbers and national IDs are masked or blocked, risky uploads are held back, and answers are checked against their sources.', ShieldCheck],
  ['Audit trail',   'Logins, uploads, and permission changes are recorded with who made them and when.',                                                  ScrollText],
];

const steps = [
  ['Upload', 'Admins add PDF, Word, Excel, HTML, Markdown, or text files into a shared collection.', Library],
  ['Share',  'Choose who can read it: everyone in the organisation, or specific people and groups.',  Lock],
  ['Ask',    'Members ask questions in plain language and get answers with the source behind each one.', Send],
];

export default function LandingPage() {
  const { me } = useApp();
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  const [guestOpen, setGuestOpen] = useState(false);

  function ask(e) {
    e.preventDefault();
    if (!q.trim()) return;
    if (me) navigate(`/chat?q=${encodeURIComponent(q)}`);
    else setGuestOpen(true);
  }

  return (
    <div className="px-3 pb-3">
      <div className="mx-auto max-w-[1400px]">
        {/* HERO */}
        <section className="relative mt-3 overflow-hidden rounded-3xl border border-white/5 bg-surface/60 p-10 md:p-16">
          <div className="relative grid items-center gap-14 md:grid-cols-[1.15fr_1fr]">
            <div>
              <p className="mb-6 text-sm uppercase tracking-[0.2em]" style={{ color: 'var(--color-blue)' }}>
                Private document Q&amp;A
              </p>

              <h1 className="font-display text-6xl leading-[1.02] md:text-8xl">
                Ask your documents.
                <br />
                <em style={{ color: 'var(--color-blue)' }}>Trust every answer.</em>
              </h1>

              <p className="mt-8 max-w-lg text-lg text-muted">
                Answers come only from the files each person is allowed to read with the exact sentence they came from.
              </p>

              {/* Try-it input */}
              <form onSubmit={ask} className="glass mt-10 flex max-w-xl items-center gap-2 rounded-2xl p-2">
                <Send size={18} className="ml-3" style={{ color: 'var(--color-blue)' }} />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="Ask anything about your documents…"
                  className="flex-1 bg-transparent px-2 py-3 text-sm text-ink placeholder:text-faint focus:outline-none"
                />
                <button
                  type="submit"
                  className="rounded-full px-5 py-2.5 text-sm font-semibold transition"
                  style={{ background: 'var(--color-blue)', color: '#ffffff' }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--color-blue-hover)')}
                  onMouseLeave={(e) => (e.currentTarget.style.background = 'var(--color-blue)')}
                >
                  Ask
                </button>
              </form>
              <p className="mt-3 text-xs text-muted">
                No account needed to try. Sign in to save chats, upload files, and search your own collections.
              </p>

              {!me && (
                <div className="mt-8 flex flex-wrap gap-3">
                  <Link
                    to="/register"
                    className="inline-flex items-center justify-center rounded-full px-6 py-3 text-sm font-semibold transition"
                    style={{ background: 'var(--color-blue)', color: '#ffffff' }}
                  >
                    Create an organisation
                  </Link>
                  <Link
                    to="/login"
                    className="inline-flex items-center justify-center rounded-full border border-border px-6 py-3 text-sm font-medium text-ink transition hover:bg-surface-2"
                  >
                    Sign in
                  </Link>
                </div>
              )}
            </div>

            <ProductPreview />
          </div>
        </section>

        {/* PRINCIPLES */}
        <section className="mt-3 rounded-3xl border border-white/5 bg-surface/40 p-10 md:p-14">
          <h2 className="font-display text-5xl leading-tight md:text-6xl">Built around who can see what.</h2>
          <div className="mt-14 grid gap-x-14 gap-y-12 md:grid-cols-2">
            {principles.map(([title, body, Icon]) => (
              <div key={title} className="border-t border-border pt-6">
                <div
                  className="mb-4 grid h-10 w-10 place-items-center rounded-xl"
                  style={{
                    background: 'color-mix(in srgb, var(--color-blue) 18%, transparent)',
                    color: 'var(--color-blue)',
                  }}
                >
                  <Icon size={18} strokeWidth={1.8} />
                </div>
                <h3 className="font-display text-2xl">{title}</h3>
                <p className="mt-2 max-w-md text-muted">{body}</p>
              </div>
            ))}
          </div>
        </section>

        {/* HOW — blue section, white cards */}
        <section
          className="mt-3 overflow-hidden rounded-3xl px-10 py-14 md:px-14"
          style={{ background: 'var(--color-blue-deep)', color: '#ffffff' }}
        >
          <h2 className="font-display text-5xl leading-tight text-white md:text-6xl">Three steps.</h2>
          <p className="mt-4 max-w-2xl text-sm text-white/70">
            From a folder of files to trusted answers — with permissions checked at every layer.
          </p>

          <div className="mt-14 grid gap-6 md:grid-cols-3">
            {steps.map(([title, body, Icon], i) => (
              <div
                key={title}
                className="rounded-2xl border border-white/15 bg-white/5 p-7 backdrop-blur-sm"
              >
                <div className="flex items-center justify-between">
                  <div className="grid h-11 w-11 place-items-center rounded-xl bg-white text-[color:var(--color-blue-deep)]">
                    <Icon size={19} strokeWidth={1.9} />
                  </div>
                  <span className="font-display text-5xl leading-none text-white/25">0{i + 1}</span>
                </div>
                <h3 className="mt-6 font-display text-3xl text-white">{title}</h3>
                <p className="mt-3 text-sm leading-relaxed text-white/75">{body}</p>
              </div>
            ))}
          </div>
        </section>

        {/* CTA */}
        <section className="mt-3 flex flex-col items-start justify-between gap-8 rounded-3xl border border-white/5 bg-surface/40 p-10 md:flex-row md:items-center md:p-14">
          <h2 className="font-display text-5xl leading-tight md:text-6xl">Start with one collection.</h2>
          <Link
            to="/register"
            className="inline-flex items-center justify-center rounded-full px-7 py-3.5 text-sm font-semibold transition"
            style={{ background: 'var(--color-blue)', color: '#ffffff' }}
          >
            Create an organisation <ArrowRight size={15} className="ml-2" />
          </Link>
        </section>

        {/* FOOTER — blue, white text */}
        <footer
          className="mt-3 overflow-hidden rounded-3xl px-10 py-12 md:px-14"
          style={{ background: 'var(--color-blue-deep)', color: '#ffffff' }}
        >
          <div className="grid gap-10 md:grid-cols-[1.4fr_1fr_1fr]">
            <div>
              <div className="flex items-center gap-3">
                <span className="grid h-9 w-9 place-items-center rounded-xl bg-white text-[color:var(--color-blue-deep)]">
                  <ShieldCheck size={18} strokeWidth={2.1} />
                </span>
                <span className="font-display text-3xl leading-none text-white">Docs Assistant</span>
              </div>
              <p className="mt-5 max-w-sm text-sm leading-relaxed text-white/70">
                Private document Q&amp;A. Answers come only from files each person is allowed to read — with the exact sentence they came from.
              </p>
            </div>

            <div>
              <div className="text-[11px] font-medium uppercase tracking-[0.18em] text-white/50">Product</div>
              <ul className="mt-4 space-y-2.5 text-sm">
                <li><Link to="/register" className="text-white/80 transition hover:text-white">Create an organisation</Link></li>
                <li><Link to="/login"    className="text-white/80 transition hover:text-white">Sign in</Link></li>
                <li><a href="#principles" className="text-white/80 transition hover:text-white">Principles</a></li>
                <li><a href="#how"        className="text-white/80 transition hover:text-white">How it works</a></li>
              </ul>
            </div>

            <div>
              <div className="text-[11px] font-medium uppercase tracking-[0.18em] text-white/50">Trust</div>
              <ul className="mt-4 space-y-2.5 text-sm text-white/80">
                <li className="flex items-center gap-2"><ShieldCheck size={14} strokeWidth={1.9} /> Role-based access</li>
                <li className="flex items-center gap-2"><Lock        size={14} strokeWidth={1.9} /> Guardrails on uploads</li>
                <li className="flex items-center gap-2"><ScrollText  size={14} strokeWidth={1.9} /> Full audit trail</li>
              </ul>
            </div>
          </div>

          <div className="mt-12 flex flex-col items-start justify-between gap-3 border-t border-white/15 pt-6 text-xs text-white/60 md:flex-row md:items-center">
            <span>© {new Date().getFullYear()} Docs Assistant · Private by default</span>
            <span>Built for teams who care who can see what.</span>
          </div>
        </footer>
      </div>

      <GuestChat open={guestOpen} onClose={() => setGuestOpen(false)} question={q} />
    </div>
  );
}

function ProductPreview() {
  return (
    <div className="relative">
      {/* Soft backdrop glow */}
      <div
        aria-hidden
        className="absolute -inset-6 -z-10 rounded-[40px] opacity-40 blur-3xl"
        style={{ background: 'radial-gradient(circle at 30% 30%, var(--color-blue), transparent 60%)' }}
      />

      {/* Main card */}
      <div className="glass relative overflow-hidden rounded-3xl p-6">

        {/* Top row: source + status */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span
              className="grid h-6 w-6 place-items-center rounded-lg"
              style={{
                background: 'color-mix(in srgb, var(--color-blue) 22%, transparent)',
                color: 'var(--color-blue)',
              }}
            >
              <Library size={12} strokeWidth={2} />
            </span>
            <span className="text-xs text-muted">Searching HR Policies</span>
          </div>

          <span
            className="inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px]"
            style={{
              borderColor: 'color-mix(in srgb, var(--color-blue) 40%, transparent)',
              color: 'var(--color-blue)',
            }}
          >
            <Lock size={10} strokeWidth={2} /> restricted
          </span>
        </div>

        {/* Divider */}
        <div className="my-5 h-px bg-border" />

        {/* User question bubble */}
        <div className="flex justify-end">
          <div className="max-w-[85%] rounded-2xl rounded-br-md bg-surface-2 px-4 py-2.5 text-sm">
            How many days of annual leave do I get?
          </div>
        </div>

        {/* Assistant answer */}
        <div className="mt-4 flex items-start gap-3">
          <span
            className="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-lg"
            style={{
              background: 'color-mix(in srgb, var(--color-blue) 22%, transparent)',
              color: 'var(--color-blue)',
            }}
          >
            <Sparkles size={12} strokeWidth={2} />
          </span>
          <p className="text-sm leading-relaxed">
            You get <span className="font-semibold">20 days</span> of paid annual leave per year.
          </p>
        </div>

        {/* Citation with blue spine */}
        <div className="mt-5 flex overflow-hidden rounded-xl border border-border bg-surface/70">
          <div className="w-1 shrink-0" style={{ background: 'var(--color-blue)' }} />
          <div className="flex-1 px-4 py-3">
            <div className="flex items-center gap-2 text-xs">
              <FileText size={12} style={{ color: 'var(--color-blue)' }} />
              <span className="text-muted">policy.pdf</span>
              <span className="text-faint">·</span>
              <span className="text-muted">Page 1</span>
            </div>
            <p className="mt-2 text-sm italic leading-relaxed text-ink/90">
              “Employees are entitled to 20 days of paid annual leave per year.”
            </p>
          </div>
        </div>

        {/* Provenance footer */}
        <div className="mt-5 flex items-center justify-between border-t border-border pt-4 text-[11px] text-faint">
          <div className="flex items-center gap-2">
            <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: 'var(--color-success)' }} />
            Verified against source
          </div>
          <div className="flex items-center gap-1.5">
            <ShieldCheck size={11} /> RBAC enforced
          </div>
        </div>
      </div>
    </div>
  );
}