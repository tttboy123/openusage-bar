# macOS Menu Bar Controls Research

Date: 2026-07-14
Status: Research complete, UI direction pending approval

## Research question

How should OpenUsage Bar use the macOS menu bar when it must summarize many heterogeneous AI providers, remain visible around limited menu-bar space, expose actionable usage information, and lead into a data-rich SwiftUI client?

## Executive conclusion

The strongest pattern is a three-level product:

1. One combined menu-bar item for an immediate signal.
2. A compact decision popover for current capacity, today's activity, source health, and quick actions.
3. A separate resizable SwiftUI window for historical analysis, filters, quotas, costs, and data provenance.

This combines iStat Menus' Combined Mode, Stats' module and permission diagnostics, SwiftBar's header/body separation, Ice's treatment of menu-bar space as a hard constraint, and Apple guidance that complex data belongs in a window-style extra or separate window.

## Comparison matrix

| Product | Menu-bar strategy | Popover or menu | Customization | Detail and diagnostics | Most useful lesson |
| --- | --- | --- | --- | --- | --- |
| CodexBar | One item per provider or Merge Icons mode | Dense provider usage rows, reset countdowns, status badges | Provider toggles, icon/label/bar display, ordering, refresh cadence | Provider status, debug attempts, CLI JSON and local serve | Provider breadth needs a combined default and source diagnostics |
| ClaudeBar | Single application item | Dashboard-like quota list with warning colors | Provider toggles, refresh, themes | Settings and provider setup | Clear quota scanning, but fixed red/yellow/green can conflict with model colors |
| Swift OpenUsage | Pins at most a few selected metrics | Provider-grouped compact meters with reset and pace | Pin metrics, show/hide/reorder, density and theme | CLI and local HTTP contract | Only pin high-value metrics; omit unavailable values instead of placeholders |
| OpenCode Bar | Single dashboard item | Pay-as-you-go and quota sections with provider submenus | Plans, sources, provider detection | Auth-source labels, CLI JSON | Separate billing from quota and expose where credentials were found |
| AgentBar | One stacked aggregate usage item | Per-service detail popover | Provider enablement and refresh | Notifications and settings | Aggregate state can be visible without one icon per provider |
| Quotio | Menu-bar entry into a wider command center | Server and quota overview | Providers, accounts, routing, alerts | Full dashboard, logs, fallback, account setup | Account and routing workflows belong outside the compact popover |
| Stats | Independent system modules and widgets | Module popovers with live and historical graphs | Module/widget enablement and refresh cost | Explicit menu-bar permission and energy diagnostics | Sampling cost and menu-bar visibility must be first-class settings |
| iStat Menus | Independent items plus Combined Mode | Rich dropdown sections | Global and per-module fields, order, display mode, rules | Detailed views and threshold rules | Combined mode is the correct default for a notch-constrained laptop |
| SwiftBar/xbar | Script header becomes menu-bar item; body becomes dropdown | Native text menu | Refresh schedule, badges, tooltips, alternate actions | Error item, debug view, stderr logs | Human rendering protocol and machine data protocol must stay separate |
| Ice | Manages hidden, always-hidden, grouped, and overflow items | Management panels | Layout, spacing, search, profiles | Visibility controls | OpenUsage cannot assume its icon is visible, even while the process is healthy |

## Sources

- CodexBar: https://github.com/steipete/CodexBar
- ClaudeBar: https://github.com/tddworks/ClaudeBar
- Swift OpenUsage: https://github.com/robinebers/openusage
- OpenCode Bar: https://github.com/opgginc/opencode-bar
- AgentBar: https://github.com/scari/AgentBar
- Quotio: https://github.com/nguyenphutrong/quotio
- Stats: https://github.com/exelban/stats
- iStat Menus: https://weather.bjango.com/mac/istatmenus/
- SwiftBar: https://github.com/swiftbar/SwiftBar
- Ice: https://github.com/jordanbaird/Ice
- Apple MenuBarExtra: https://developer.apple.com/documentation/swiftui/menubarextra
- Apple popovers: https://developer.apple.com/design/human-interface-guidelines/popovers/

## Patterns worth migrating

### One combined item by default

OpenUsage Bar should not create one icon per provider. A single combined item is the safe default on a MacBook with a notch and on systems that hide overflow items.

Offer four display presets:

1. Icon Only
2. Compact: icon plus the most urgent remaining value
3. Activity: icon plus today's Token total
4. Custom: one primary and at most one secondary short value

Use fixed formatting and monospaced digits so refreshes do not change the item's width abruptly. Provider names and reset sentences do not belong in the persistent menu-bar label.

### Human summary, not a miniature dashboard

The popover should answer:

- What needs attention now?
- Which subscription is closest to exhaustion?
- How much Token activity happened today?
- Is the data current and trustworthy?
- Where can I inspect the full history?

Recommended order:

1. Global strip
2. Attention, only when needed
3. Capacity
4. Today activity and spend
5. Footer actions

Do not place the annual heatmap, 30-day model chart, provider setup forms, raw diagnostics, or routing controls in the popover.

### One primary quota per provider row

Each provider row shows:

- Provider identity and optional plan
- One most decision-relevant quota or balance
- Remaining value
- Reset countdown when authoritative
- Freshness or a source problem

Secondary windows expand inline. Do not open nested popovers. Only one provider row remains expanded at a time.

Allow users to classify Provider metrics as Always Visible or On Demand and reorder them, following the strongest part of Swift OpenUsage's customization model. Limit persistent menu-bar pins to two metrics; usage-trend charts cannot be pinned.

### Last-good first

Opening the popover should immediately show the last-good snapshot and its age. A lightweight freshness check runs in the background. The UI never becomes empty while waiting for remote providers.

The popover distinguishes:

- Confirmed zero
- Unsupported
- Not configured
- Permission blocked
- Temporarily unavailable
- Stale last-good data

### Permission and visibility recovery

Stats documents a macOS 26 failure mode in which a running application is not allowed to display menu-bar items under System Settings > Menu Bar. OpenUsage Bar must include:

- A normal launchable application entry
- First-run visibility check
- A Data Health window reachable without the menu-bar icon
- A `doctor` command
- An `Open System Settings` recovery action

The collector and ledger remain useful even if the status item is hidden.

### Sampling and energy tiers

Provider sources do not need the same refresh cadence:

- Local telemetry: event-driven or short cadence
- Local logs and SQLite: moderate cadence
- Subscription quota APIs: slower cadence
- Billing APIs: slow cadence
- Manual cookie or fragile web sources: conservative cadence

Manual refresh must be debounced and timeout-bounded. The popover should show whether data is live, cached, or stale rather than creating the appearance of real-time accuracy.

## Recommended OpenUsage Bar menu structure

### L0: status item

Default rendering:

```text
[OpenUsage glyph] 42%
```

The value represents the most urgent active capacity window. The accessibility title explains the provider, remaining amount, and reset.

Only global failure uses a warning overlay. A single provider failure does not replace a useful global capacity signal.

### L1: popover

Target width: 380 to 420 points.

Use a fixed header and footer with a middle scrolling region whose maximum height is derived from the current screen's visible frame. ClaudeBar's approximately 400-point width and bounded scrolling area are a useful reference: refresh, settings, and the details entry must never be pushed below the screen.

```text
OpenUsage                                      Refresh
Today 74.2M Token        6 live · 1 stale · 1 issue

Attention
MiniMax              18% left       resets in 2h

Capacity
Codex                68% left       resets Sunday
Kiro                 42% left       resets Jul 31
Cursor               Stale          updated 18m ago

Today
74.2M Token          $5.42 estimated
7-day trend          5 active models

Usage Details…       Data Health       Settings
```

Sections with no relevant content disappear. No decorative success card is shown merely to fill space.

### L2: Usage Details window

The separate SwiftUI client contains:

- Overview
- Capacity
- Activity
- API Spend
- Local Tools
- Providers and Accounts
- Data Health

The approved annual square heatmap and daily model stacked chart remain in Activity.

## Visual system

Recommended design dials:

- Density: 7/10
- Design variance: 4/10
- Motion: 2/10

Rules:

- System font and semantic materials
- One neutral structural palette
- Provider brand color only for identity
- Model colors only for composition
- Risk colors only for state
- Monospaced digits for changing values
- Hairlines and spacing instead of a grid of rounded cards
- No animated cycling of provider names or metrics
- No constant one-second visual updates without a real source event

Risk color should use burn pace when the source exposes a reset window. A high used percentage immediately before reset may be safe, while the same percentage early in a window may predict exhaustion. Fixed percentage thresholds remain a fallback, not the canonical risk model.

## Keyboard and accessibility

Required commands:

- Command-R: refresh
- Command-D: Usage Details
- Command-comma: Settings
- Escape: close or collapse
- Arrow keys: provider navigation
- Return or Space: expand or activate

Requirements:

- Menu-bar item has a meaningful accessibility title and value
- Provider quota reads provider, window, remaining value, and reset
- Risk state is textually named, not expressed by color alone
- Focus order matches visual order
- Increase Contrast, Reduce Motion, Reduce Transparency, light, and dark appearances are tested
- Annual heatmap exposes an aggregate summary and grouped keyboard navigation rather than 365 meaningless stops

## Patterns to reject

- One menu-bar icon per provider as the default
- More than two changing values in the persistent label
- Annual charts or provider setup forms inside the popover
- A red/yellow/green system that also doubles as model identity
- Nested provider popovers
- Hiding last-good data during refresh
- Treating a hidden icon as a dead collector
- Parsing Accessibility text as the machine API
- Arbitrary per-provider layout DSL in the first release

## Recommended decision

Adopt the combined decision-console pattern:

- One combined status item
- One compact 380 to 420 point popover
- One resizable Usage Details window
- One canonical query core powering UI, CLI, and local API
- First-run menu-bar visibility diagnosis
- A small set of display presets before advanced customization
