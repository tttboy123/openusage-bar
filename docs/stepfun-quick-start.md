# StepFun Step Plan quick start

OpenUsage Bar supports two StepFun credential modes:

| Input | What the card can show |
|---|---|
| Full browser Session Cookie | Real Credit-plan balance or legacy 5-hour/week remaining quota, plan name, and reset/expiry times when StepFun supplies them |
| Step API key only | Connection state and available model count; no subscription quota |

It also supports both official StepFun sites:

| Site selection | Platform | Step Plan API |
|---|---|---|
| China (.com) | `https://platform.stepfun.com` | `https://api.stepfun.com/step_plan/v1` |
| International (.ai) | `https://platform.stepfun.ai` | `https://api.stepfun.ai/step_plan/v1` |

Choose the site where the account and credential were created. OpenUsage Bar deliberately does not auto-try the other site because a browser session must never be disclosed across regions.

## Add the web session

1. Sign in to [StepFun China](https://platform.stepfun.com/) or [StepFun International](https://platform.stepfun.ai/) and open the Step Plan usage page.
2. Open browser developer tools, select **Network**, refresh the page, and choose the request named `QueryStepPlanRateLimit`.
3. Under **Request Headers**, copy the complete `Cookie` value. Do not copy it into chat, source code, a note, or a shell command.
4. Click the OpenUsage Bar menu-bar icon, then **+** and **Step Plan**.
5. Select the matching **China (.com)** or **International (.ai)** site, enter an account label, and paste the cookie into **Full Session Cookie or Oasis-Token**. The Step API key is optional.
6. Save and refresh. Credit-based plans show `…% remaining` plus residual/total credits and expiry; legacy plans show `5h …% remaining` and `Weekly …% remaining`.

The input may contain many browser cookies. OpenUsage Bar immediately separates it and retains only:

- `Oasis-Token`: access and refresh session tokens.
- `Oasis-Webid`: the browser/device identifier required by StepFun.

Values such as `__stripe_mid`, `INGRESSCOOKIE`, and `_wafdytokenv1` are discarded. The two retained values are stored as separate macOS Keychain items under service `com.lune.openusage-menubar`; they are never written to `providers.json`. The app does not request or store a StepFun password.

## Refresh and expiration

The app calls only fixed StepFun HTTPS endpoints and does not make a model-generation request. A China account can contact only `platform.stepfun.com` and `api.stepfun.com`; an International account can contact only `platform.stepfun.ai` and `api.stepfun.ai`. When StepFun rejects an expired access token, the app uses the paired refresh token on that same site and replaces only the Oasis token in Keychain. Redirects are disabled so the session header cannot be forwarded to another host.

If the card says **Credential rejected**, first verify the selected site. Then sign out of that StepFun site, sign in again, copy the new request Cookie, and update the matching entry through **+ → Step Plan**. Never send the cookie to another person.

StepFun sometimes returns reset time `0`. That means no reset timestamp is currently supplied; OpenUsage Bar still displays the valid quota instead of reporting an error.

For newer Credit-based plans, the dashboard may return the legacy 5-hour and weekly fields as zero even when the subscription is full. OpenUsage Bar therefore prefers `plan_credit_rate_limit` when it is valid and falls back to the legacy windows only when the Credit block is absent or unusable.
