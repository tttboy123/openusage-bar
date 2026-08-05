import { useEffect, useState } from "react";
import { fetchCapacity, type CapacityProvider } from "../api";
import { type Messages } from "../i18n";

function ratioWidth(ratio: number | undefined): string {
  if (ratio === undefined) return "0%";
  const clamped = Math.max(0, Math.min(1, ratio)) * 100;
  return `${clamped}%`;
}

export default function CapacityPage({ t }: { t: Messages }) {
  const [items, setItems] = useState<CapacityProvider[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void fetchCapacity()
      .then(setItems)
      .catch((e) => setError(e instanceof Error ? e.message : "failed"));
  }, []);

  return (
    <section className="panel">
      <div className="panel-head">
        <h3>{t.navCapacity}</h3>
        <span>{items.length} scopes</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Provider</th>
              <th scope="col">Quota</th>
              <th scope="col">Used</th>
              <th scope="col">Remaining</th>
              <th scope="col">State</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item, index) => (
              <tr key={`${item.providerId}-${index}`}>
                <th scope="row">{item.providerId ?? "n/a"}</th>
                <td>{item.quotaName ?? "n/a"}</td>
                <td className="mono">
                  {item.used ?? "n/a"}
                  {item.unit === "percent" ? "%" : ""}
                </td>
                <td className="mono">{item.remaining ?? "n/a"}</td>
                <td>
                  <span className={`pill pill-${item.state === "ok" ? "ok" : "warn"}`}>
                    {item.state ?? "unknown"}
                  </span>
                </td>
              </tr>
            ))}
            {items.length === 0 && !error ? (
              <tr>
                <td colSpan={5} className="empty">
                  {t.comingSoon}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      <div className="panel-body" style={{ paddingTop: 0 }}>
        {items.map((item, index) => (
          <div key={`${item.providerId}-${index}`} style={{ marginBottom: 8 }}>
            <div
              style={{
                height: 5,
                borderRadius: 3,
                background: "var(--surface-alt)",
                overflow: "hidden",
              }}
              aria-hidden="true"
            >
              <div
                style={{
                  width: ratioWidth(item.remainingRatio),
                  height: "100%",
                  background: "var(--accent)",
                }}
              />
            </div>
          </div>
        ))}
      </div>
      {error ? <p className="empty">{error}</p> : null}
    </section>
  );
}
