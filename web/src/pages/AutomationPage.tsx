import { type Messages } from "../i18n";

export default function AutomationPage({ t }: { t: Messages }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h3>{t.navAutomation}</h3>
        <span>v0.8</span>
      </div>
      <div className="panel-body">
        <p className="empty">{t.comingSoon}</p>
      </div>
    </section>
  );
}
