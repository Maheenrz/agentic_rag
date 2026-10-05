import { Link } from 'react-router-dom';

const btnPrimary =
  'inline-flex items-center justify-center rounded-full bg-accent px-6 py-3 text-sm font-semibold text-canvas transition hover:bg-accent-hover';
const btnGhost =
  'inline-flex items-center justify-center rounded-full border border-border px-6 py-3 text-sm font-medium text-ink transition hover:bg-surface-2';

const principles = [
  ['01', 'Access first',
    'Search only looks inside collections the person is allowed to read. The database filter enforces this, not the language model.'],
  ['02', 'Quoted sources',
    'Every answer names the file and section, and quotes the exact sentence it used, so you can check it yourself.'],
  ['03', 'Guardrails',
    'Card numbers and national IDs are masked or blocked, risky uploads are held back, and answers are checked against their sources.'],
  ['04', 'Audit trail',
    'Logins, uploads, and permission changes are recorded with who made them and when.'],
];

const steps = [
  ['Upload', 'Admins add PDF, Word, Excel, HTML, Markdown, or text files into a shared collection.'],
  ['Share', 'Choose who can read it: everyone in the organisation, or specific people and groups.'],
  ['Ask', 'Members ask questions in plain language and get answers with the source behind each one.'],
];

export default function LandingPage() {
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 border-b border-border/60 bg-canvas/85 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
          <Link to="/" className="font-display text-2xl">Docs Assistant</Link>

          <nav className="hidden gap-8 text-sm text-muted md:flex">
            <a href="#principles" className="transition hover:text-ink">Principles</a>
            <a href="#how" className="transition hover:text-ink">How it works</a>
          </nav>

          <div className="flex items-center gap-2">
            <Link to="/login" className="px-4 py-2 text-sm text-muted transition hover:text-ink">Sign in</Link>
            <Link to="/register" className={`${btnPrimary} px-4 py-2`}>Get started</Link>
          </div>
        </div>
      </header>

      <section className="mx-auto grid max-w-6xl items-center gap-16 px-6 pb-24 pt-20 md:grid-cols-[1.1fr_1fr]">
        <div>
          <p className="mb-6 text-lg text-muted">Private document Q{'&'}A for <span className="italic">Teams</span></p>
          <h1 className="font-display text-6xl leading-[1.02] md:text-7xl">
            Ask your documents.
            <br />
            <em className="text-accent">Trust every answer.</em>
          </h1>
          <p className="mt-8 max-w-md text-lg text-muted">
            Answers come only from the files each person is allowed to read, with the exact sentence they came from.
          </p>
          <div className="mt-10 flex flex-wrap gap-3">
            <Link to="/register" className={btnPrimary}>Create an organisation</Link>
            <Link to="/login" className={btnGhost}>Sign in</Link>
          </div>
        </div>

        <ProductPreview />
      </section>

      <section id="principles" className="border-t border-border">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <h2 className="font-display text-4xl md:text-5xl">Built around who can see what.</h2>
          <div className="mt-14 grid gap-x-12 gap-y-12 md:grid-cols-2">
            {principles.map(([num, title, body]) => (
              <div key={num} className="border-t border-border pt-6">
                <div className="text-sm text-muted">{num}</div>
                <h3 className="mt-2 text-xl font-medium">{title}</h3>
                <p className="mt-2 max-w-md text-muted">{body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section id="how" className="border-t border-border">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <h2 className="font-display text-4xl md:text-5xl">Three steps.</h2>
          <div className="mt-14 grid gap-10 md:grid-cols-3">
            {steps.map(([title, body], i) => (
              <div key={title}>
                <div className="font-display text-5xl text-muted">{i + 1}</div>
                <h3 className="mt-4 text-xl font-medium">{title}</h3>
                <p className="mt-2 text-muted">{body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="border-t border-border">
        <div className="mx-auto flex max-w-6xl flex-col items-start justify-between gap-8 px-6 py-20 md:flex-row md:items-center">
          <h2 className="font-display text-4xl md:text-5xl">Start with one collection.</h2>
          <Link to="/register" className={btnPrimary}>Create an organisation</Link>
        </div>
      </section>

      <footer className="border-t border-border">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-8 text-sm text-muted">
          <span className="font-display text-lg text-ink">Docs Assistant</span>
          <span>Private by default</span>
        </div>
      </footer>
    </div>
  );
}

function ProductPreview() {
  return (
    <div className="rounded-3xl border border-border bg-surface p-6">
      <div className="flex items-center justify-between text-xs text-muted">
        <span>Searching HR Policies</span>
        <span className="rounded-full border border-border px-2.5 py-0.5">restricted</span>
      </div>

      <div className="ml-auto mt-6 w-fit max-w-[80%] rounded-2xl bg-surface-2 px-4 py-2.5 text-sm">
        How many days of annual leave do I get?
      </div>

      <p className="mt-4 leading-relaxed">You get 20 days of paid annual leave per year.</p>

      <div className="mt-5 rounded-xl border border-border px-4 py-3">
        <div className="text-xs text-muted">policy.pdf · Page 1</div>
        <p className="mt-1.5 border-l-2 border-accent pl-3 italic text-ink/90">
          “Employees are entitled to 20 days of paid annual leave per year.”
        </p>
      </div>
    </div>
  );
}