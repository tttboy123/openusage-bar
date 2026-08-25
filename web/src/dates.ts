export function localDayKey(d: Date): string {
  const year = String(d.getFullYear()).padStart(4, "0");
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function parseDayKey(dayKey: string): Date {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dayKey);
  if (!match) {
    throw new RangeError(`Invalid day key: ${dayKey}`);
  }

  const [, yearText, monthText, dayText] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const date = new Date(0);
  date.setHours(0, 0, 0, 0);
  date.setFullYear(year, month - 1, day);

  if (
    date.getFullYear() !== year ||
    date.getMonth() !== month - 1 ||
    date.getDate() !== day
  ) {
    throw new RangeError(`Invalid day key: ${dayKey}`);
  }

  return date;
}

export function eachDay(from: string, to: string): Date[] {
  const days: Date[] = [];
  const current = parseDayKey(from);
  const end = parseDayKey(to);

  while (current <= end) {
    days.push(new Date(current));
    current.setDate(current.getDate() + 1);
  }

  return days;
}

export type DayRange = {
  from: string;
  to: string;
};

export function rangeFor(days: number, now: Date = new Date()): DayRange {
  if (!Number.isInteger(days) || days < 1) {
    throw new RangeError("days must be a positive integer");
  }

  const to = new Date(now);
  const from = new Date(now);
  from.setDate(from.getDate() - days + 1);

  return {
    from: localDayKey(from),
    to: localDayKey(to),
  };
}

export function startOfWeek(dayKey: string): string {
  const date = parseDayKey(dayKey);
  date.setDate(date.getDate() - date.getDay());
  return localDayKey(date);
}

export function endOfWeek(dayKey: string): string {
  const date = parseDayKey(dayKey);
  date.setDate(date.getDate() + (6 - date.getDay()));
  return localDayKey(date);
}

export function rangeLength(from: string, to: string): number {
  return eachDay(from, to).length;
}
