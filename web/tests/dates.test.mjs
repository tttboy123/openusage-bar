import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import {
  eachDay,
  endOfWeek,
  localDayKey,
  parseDayKey,
  rangeFor,
  rangeLength,
  startOfWeek,
} from "../.test-dist/dates.js";

test("localDayKey pads month and day", () => {
  assert.equal(localDayKey(new Date(2026, 0, 1)), "2026-01-01");
  assert.equal(localDayKey(new Date(2026, 11, 31)), "2026-12-31");
});

test("day keys always use a four-digit year", () => {
  assert.equal(localDayKey(parseDayKey("0042-08-24")), "0042-08-24");
});

test("localDayKey uses local calendar date", () => {
  const d = new Date(2026, 6, 7, 23, 30);
  assert.equal(localDayKey(d), "2026-07-07");
});

test("parseDayKey creates local midnight without UTC date drift", () => {
  const date = parseDayKey("2026-08-24");

  assert.deepEqual(
    [
      date.getFullYear(),
      date.getMonth() + 1,
      date.getDate(),
      date.getHours(),
      date.getMinutes(),
    ],
    [2026, 8, 24, 0, 0],
  );
});

test("eachDay includes every local calendar day in the closed range", () => {
  assert.deepEqual(
    eachDay("2026-08-23", "2026-08-25").map(localDayKey),
    ["2026-08-23", "2026-08-24", "2026-08-25"],
  );
});

test("rangeFor is injectable and recomputes across local midnight", () => {
  const beforeMidnight = new Date(2026, 7, 23, 23, 59, 59);
  const afterMidnight = new Date(2026, 7, 24, 0, 0, 1);

  assert.deepEqual(rangeFor(7, beforeMidnight), {
    from: "2026-08-17",
    to: "2026-08-23",
  });
  assert.deepEqual(rangeFor(7, afterMidnight), {
    from: "2026-08-18",
    to: "2026-08-24",
  });
});

test("week boundaries use the local Sunday-through-Saturday calendar week", () => {
  assert.equal(startOfWeek("2026-08-24"), "2026-08-23");
  assert.equal(endOfWeek("2026-08-24"), "2026-08-29");
});

test("rangeLength counts calendar days across daylight-saving changes", () => {
  assert.equal(rangeLength("2026-03-07", "2026-03-09"), 3);
});

test("August 24 and midnight ranges are stable in Los Angeles and Singapore", () => {
  const moduleUrl = new URL("../.test-dist/dates.js", import.meta.url).href;

  function probe(timeZone, beforeMidnight, afterMidnight) {
    const script = `
      import { localDayKey, rangeFor } from ${JSON.stringify(moduleUrl)};
      const instant = new Date("2026-08-24T00:30:00.000Z");
      process.stdout.write(JSON.stringify({
        instantDay: localDayKey(instant),
        before: rangeFor(1, new Date(${JSON.stringify(beforeMidnight)})),
        after: rangeFor(1, new Date(${JSON.stringify(afterMidnight)})),
      }));
    `;

    return JSON.parse(
      execFileSync(process.execPath, ["--input-type=module", "--eval", script], {
        encoding: "utf8",
        env: { ...process.env, TZ: timeZone },
      }),
    );
  }

  assert.deepEqual(
    probe(
      "America/Los_Angeles",
      "2026-08-24T06:59:59.000Z",
      "2026-08-24T07:00:01.000Z",
    ),
    {
      instantDay: "2026-08-23",
      before: { from: "2026-08-23", to: "2026-08-23" },
      after: { from: "2026-08-24", to: "2026-08-24" },
    },
  );
  assert.deepEqual(
    probe(
      "Asia/Singapore",
      "2026-08-23T15:59:59.000Z",
      "2026-08-23T16:00:01.000Z",
    ),
    {
      instantDay: "2026-08-24",
      before: { from: "2026-08-23", to: "2026-08-23" },
      after: { from: "2026-08-24", to: "2026-08-24" },
    },
  );
});
