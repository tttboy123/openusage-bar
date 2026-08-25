const TOKEN_UNITS = ["", "K", "M", "B", "T"] as const;

/** Keep token abbreviations stable across UI languages and OS locales. */
export function formatTokenCompact(value: number): string {
  if (!Number.isFinite(value)) return "—";

  const magnitude = Math.abs(value);
  if (magnitude < 1_000) return String(Math.round(value));

  const unitIndex = Math.min(
    Math.floor(Math.log(magnitude) / Math.log(1_000)),
    TOKEN_UNITS.length - 1,
  );
  const scaled = magnitude / 1_000 ** unitIndex;
  const rounded = Math.round(scaled * 10) / 10;
  const sign = value < 0 ? "-" : "";

  return `${sign}${rounded.toFixed(1).replace(/\.0$/, "")}${TOKEN_UNITS[unitIndex]}`;
}
