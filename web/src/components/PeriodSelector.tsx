import { type Messages } from "../i18n";

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

export {
  eachDay,
  endOfWeek,
  localDayKey,
  parseDayKey,
  rangeFor,
  rangeLength,
  startOfWeek,
} from "../dates";
export { type DayRange } from "../dates";

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

  function move(dir: -1 | 1) {
    const index = PERIODS.indexOf(value);
    const next = PERIODS[Math.max(0, Math.min(PERIODS.length - 1, index + dir))];
    if (next && next !== value) onChange(next);
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      move(-1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      move(1);
    } else if (event.key === "Home") {
      event.preventDefault();
      onChange(PERIODS[0]);
    } else if (event.key === "End") {
      event.preventDefault();
      onChange(PERIODS[PERIODS.length - 1]);
    }
  }

  return (
    <div
      className="segmented"
      role="group"
      aria-label={t.periodLabel}
      onKeyDown={onKeyDown}
      style={{ marginBottom: 16 }}
    >
        {PERIODS.map((period) => (
          <button
            type="button"
            key={period}
            aria-pressed={value === period}
            onClick={() => onChange(period)}
          >
            {labels[period]}
          </button>
        ))}
    </div>
  );
}
