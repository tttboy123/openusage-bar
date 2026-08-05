export type Period = "day" | "week" | "month" | "year";

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
}: {
  value: Period;
  onChange: (period: Period) => void;
}) {
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
          {period}
        </button>
      ))}
    </div>
  );
}
