import { type Messages } from "../i18n";

export default function PlaceholderPage({ t, title }: { t: Messages; title: string }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h3>{title}</h3>
        <span>v0.8</span>
      </div>
      <div className="panel-body">
        <p className="empty">{t.comingSoon}</p>
      </div>
    </section>
  );
}
