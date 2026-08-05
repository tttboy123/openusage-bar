export type Period = "day" | "week" | "month" | "year";
import { type Messages } from "../i18n";

export const PERIODS: Period[] = ["day", "week", "month", "year"];

export function periodDays(period: Period): number {
  switch (period) {
    case "day":
      return 1;
    case "week":
      return 7;
    case "month":
      return 30;
    case "year":
      return 365;
  }
}

export function rangeFor(days: number) {
  const to = new Date();
  const from = new Date();
  from.setDate(from.getDate() - days + 1);
  return {
    from: from.toISOString().slice(0, 10),
    to: to.toISOString().slice(0, 10),
  };
}

export default function PeriodSelector({
  value,
  onChange,
  t,
}: {
  value: Period;
  onChange: (period: Period) => void;
  t: Messages;
}) {
  const labels: Record<Period, string> = {
    day: t.periodDay,
    week: t.periodWeek,
    month: t.periodMonth,
    year: t.periodYear,
  };
  return (
    <div className="toolbar" style={{ marginBottom: 16 }}>
      {PERIODS.map((period) => (
        <button
          type="button"
          key={period}
          className="icon-btn"
          style={{
            background: value === period ? "var(--accent-soft)" : undefined,
            color: value === period ? "var(--accent)" : undefined,
          }}
          onClick={() => onChange(period)}
        >
          {labels[period]}
        </button>
      ))}
    </div>
  );
}
